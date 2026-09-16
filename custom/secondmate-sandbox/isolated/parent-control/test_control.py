"""Offline unit + real loopback HTTP roundtrip tests. No Docker/Telegram/DB network."""
import copy
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import uuid

from common import Refusal, canonical, digest, validate_brain
from server import Application, Handler, DockerExecutor
from client import Client
from lifecycle import Lifecycle
from local_ops import apply_approval, export_brain, intake_reports

PARENT = {'role': 'parent', 'id': 'firstmate'}
CHILD = {'role': 'child', 'id': 'team-sandbox'}
OTHER_CHILD = {'role': 'child', 'id': 'other'}
OPERATOR = {'role': 'operator', 'id': 'captain-operator'}


def proposal():
    value = {'schema': 'knowledge-proposal.v1', 'proposal_id': str(uuid.uuid4()), 'version': 1,
             'manifest': {'claims': [{'id': 'c1', 'text': 'Reviewed first claim.', 'evidence_ids': ['e1']},
                                     {'id': 'c2', 'text': 'Unselected claim stays in child.', 'evidence_ids': ['e1']}],
                          'evidence': [{'id': 'e1', 'description': 'Observed example.', 'source': 'data/evidence.md'}],
                          'scope': {'domain': 'finance'}}, 'content': 'Full child notes are review data only.'}
    value['sha256'] = digest(value)
    return value


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.seen = {}
        self.fail = False

    def __call__(self, child, operation, payload=None):
        self.calls.append((child['container'], operation, payload))
        if self.fail:
            raise Refusal('Unknown runtime.', 503, 'runtime_unknown')
        if operation == 'status':
            return {'counts': {}, 'ready': True}
        key = payload['request_id']
        if key in self.seen:
            if self.seen[key] != payload:
                raise Refusal('Conflicting runtime request.')
            return {'request_id': key, 'status': 'duplicate'}
        self.seen[key] = copy.deepcopy(payload)
        return {'request_id': key, 'status': 'queued'}


class FakeLifecycle:
    def __init__(self):
        self.calls = []
    def status(self, child):
        return {'generation': 'g1', 'identity_verified': True, 'safe_to_stop': True}
    def execute(self, child, action, expected_generation, recovery=False):
        if expected_generation != 'g1':
            raise Refusal('Stale generation.', 409, 'stale_generation')
        self.calls.append((child['container'], action, recovery))
        return {'action': action}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = FakeRuntime()
        self.lifecycle = FakeLifecycle()
        self.config = {'version': 1, 'database': str(self.root / 'control.sqlite3'), 'captain_user_id': 123,
                       'parents': {'firstmate': {'can_approve': False}, 'another-parent': {'can_approve': False}},
                       'children': {'team-sandbox': {'parent_id': 'firstmate', 'container': 'secondmate', 'home': '/home/nguye/team-sandbox'},
                                    'other': {'parent_id': 'another-parent', 'container': 'othermate', 'home': '/home/nguye/other'}},
                       'operators': {'captain-operator': {'children': ['team-sandbox'], 'allow_recovery': True}}}
        self.tokens = {'p' * 48: PARENT, 'c' * 48: CHILD, 'o' * 48: OTHER_CHILD, 'a' * 48: OPERATOR}
        self.app = Application(self.config, self.tokens, self.runtime, lifecycle=self.lifecycle)

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def request(self):
        return {'request_id': str(uuid.uuid4()), 'correlation': '0123456789abcdef', 'body': 'Inspect an example.', 'scope': {'domain': 'finance'}}

    def call(self, method, suffix, who=PARENT, value=None):
        return self.app.handle(method, '/v1/children/team-sandbox/' + suffix, who, value)

    def test_parent_ownership_and_no_child_host_authority(self):
        listing = self.app.handle('GET', '/v1/children', PARENT)
        self.assertEqual([x['child_id'] for x in listing['children']], ['team-sandbox'])
        for action in ['requests', 'control', 'approvals']:
            with self.assertRaises(Refusal):
                self.call('POST', action, CHILD, {})
        with self.assertRaises(Refusal):
            self.app.handle('GET', '/v1/children/other/status', PARENT)
        with self.assertRaises(Refusal):
            self.app.handle('GET', '/v1/reports', CHILD)

    def test_request_correlation_roundtrip_and_event_pull_idempotency(self):
        request = self.request()
        result = self.call('POST', 'requests', value=request)
        self.assertEqual(result['delivery'], 'delivered')
        self.assertEqual(self.runtime.seen[request['request_id']]['authority'], {'role': 'parent', 'approval': False})
        self.call('POST', 'requests', value=request)
        self.assertEqual(len(self.runtime.calls), 1)
        event = {'event_id': str(uuid.uuid4()), 'kind': 'done', 'text': 'Completed with evidence.',
                 'request_id': request['request_id'], 'correlation': request['correlation']}
        first = self.call('POST', 'reports', CHILD, event)
        second = self.call('POST', 'reports', CHILD, event)
        self.assertEqual(first['sequence'], second['sequence'])
        batch = self.app.handle('GET', '/v1/reports?after=0', PARENT)
        self.assertEqual(len(batch['events']), 1)
        self.assertEqual(batch['events'][0]['request_id'], request['request_id'])
        received = intake_reports(self.root, batch)
        self.assertEqual(len(received['stored']), 1)
        self.assertEqual(intake_reports(self.root, batch)['stored'], [])
        self.assertFalse((self.root / 'data/learnings.md').exists())
        self.assertEqual(self.app.handle('GET', '/v1/reports?after=' + str(batch['next_cursor']), PARENT)['events'], [])

    def test_unknown_delivery_retry_preserves_exact_id(self):
        request = self.request()
        self.runtime.fail = True
        with self.assertRaises(Refusal):
            self.call('POST', 'requests', value=request)
        self.runtime.fail = False
        before = len(self.runtime.calls)
        self.assertEqual(self.call('POST', 'requests', value=request)['delivery'], 'unknown')
        self.assertEqual(len(self.runtime.calls), before)
        self.assertEqual(self.call('POST', 'requests/retry', value=request)['delivery'], 'delivered')
        changed = {**request, 'body': 'Different.'}
        with self.assertRaises(Refusal):
            self.call('POST', 'requests/retry', value=changed)

    def test_reports_cannot_claim_foreign_requests_or_change_event(self):
        request = self.request()
        self.call('POST', 'requests', value=request)
        event = {'event_id': str(uuid.uuid4()), 'kind': 'done', 'text': 'Outcome', 'request_id': request['request_id'], 'correlation': 'f' * 16}
        with self.assertRaises(Refusal):
            self.call('POST', 'reports', CHILD, event)
        event['correlation'] = request['correlation']
        self.call('POST', 'reports', CHILD, event)
        event['text'] = 'Changed'
        with self.assertRaises(Refusal):
            self.call('POST', 'reports', CHILD, event)

    def test_proposal_immutable_and_only_exact_selected_claims_promote(self):
        value = proposal()
        response = self.call('POST', 'proposals', CHILD, value)
        self.assertTrue(response['approval_required'])
        events = self.app.handle('GET', '/v1/reports', PARENT)['events']
        self.assertTrue(events[0]['requires_mac_notification'])
        changed = copy.deepcopy(value); changed['content'] = 'Changed'; changed['sha256'] = digest({k: v for k, v in changed.items() if k != 'sha256'})
        with self.assertRaises(Refusal):
            self.call('POST', 'proposals', CHILD, changed)
        approval = {'approval_id': str(uuid.uuid4()), 'proposal_id': value['proposal_id'], 'version': 1,
                    'sha256': value['sha256'], 'selected_claims': ['c1'], 'destination': 'data/learnings.md',
                    'approval_reference': {'captain_user_id': 123, 'source': 'telegram', 'reference': 'chat:123/message:99',
                                           'quoted_approval': 'Import c1 of this exact proposal into firstmate learnings.'}}
        with self.assertRaises(Refusal):
            self.call('POST', 'approvals', PARENT, approval)
        accepted = self.call('POST', 'approvals', OPERATOR, approval)['approval']
        with self.assertRaises(Refusal):
            self.app.handle('GET', '/v1/approvals/' + approval['approval_id'], CHILD)
        self.assertFalse((self.root / 'data/learnings.md').exists())
        receipt = apply_approval(self.root, accepted, 'firstmate')
        self.assertEqual(receipt, apply_approval(self.root, accepted, 'firstmate'))
        body = (self.root / 'data/learnings.md').read_text()
        self.assertIn('Reviewed first claim.', body)
        self.assertNotIn('Unselected claim', body)
        self.assertNotIn('Full child notes', body)
        self.assertEqual(body.count('knowledge-approval:'), 1)
        self.app.handle('POST', '/v1/approvals/' + approval['approval_id'] + '/receipt', PARENT, receipt)
        bad = copy.deepcopy(approval); bad['approval_id'] = str(uuid.uuid4()); bad['sha256'] = '0' * 64
        with self.assertRaises(Refusal):
            self.call('POST', 'approvals', OPERATOR, bad)

    def test_policy_export_never_copies_whole_private_memory(self):
        (self.root / 'config').mkdir()
        (self.root / 'config/crew-harness').write_text('codex\n')
        (self.root / 'config/backend').write_text('tmux\n')
        (self.root / 'data').mkdir(); (self.root / 'data/captain.md').write_text('Private memory.')
        (self.root / '.env').write_text('PRIVATE=do-not-export')
        skill = self.root / '.agents/skills/review'; skill.mkdir(parents=True); (skill / 'SKILL.md').write_text('Curated review skill.')
        brain = export_brain(self.root, ['review'], model='gpt-5.6-sol', effort='xhigh')
        paths = {x['path'] for x in brain['files']}
        self.assertEqual(paths, {'config/crew-harness', 'config/inherited-runtime.json', '.agents/skills/review/SKILL.md'})
        self.call('POST', 'brain', value=brain)
        self.assertEqual(self.call('GET', 'brain', CHILD), brain)
        with self.assertRaises(Refusal):
            self.call('POST', 'brain', CHILD, brain)
        tampered = copy.deepcopy(brain); tampered['files'][0]['path'] = 'data/captain.md'
        tampered['revision'] = digest(tampered['files'])
        with self.assertRaises(Refusal):
            validate_brain(tampered)

    def test_control_operation_id_prevents_duplicate_restart(self):
        command = {'operation_id': str(uuid.uuid4()), 'action': 'restart', 'expected_generation': 'g1'}
        first = self.call('POST', 'control', value=command)
        self.assertEqual(first['state'], 'complete')
        self.call('POST', 'control', value=command)
        self.assertEqual(len(self.lifecycle.calls), 1)
        stale = {**command, 'operation_id': str(uuid.uuid4()), 'expected_generation': 'g0'}
        with self.assertRaises(Refusal):
            self.call('POST', 'control', value=stale)
        recovery = {**command, 'operation_id': str(uuid.uuid4()), 'operator_recovery_reference': 'Mac recovery approval ref 99'}
        with self.assertRaises(Refusal):
            self.call('POST', 'control', value=recovery)
        self.call('POST', 'control', OPERATOR, recovery)

    def test_parent_config_cannot_enable_self_approval(self):
        self.config['parents']['firstmate']['can_approve'] = True
        with self.assertRaises(Refusal):
            self.call('POST', 'approvals', PARENT, {})

    def test_data_scope_comes_from_authentication(self):
        calls = []
        class Data:
            def execute(self, child_id, payload):
                calls.append((child_id, payload)); return {'rows': []}
            schema = execute
        self.app.data_service = Data()
        self.app.handle('POST', '/v1/data/query', CHILD, {'table': 'orders'})
        self.assertEqual(calls, [('team-sandbox', {'table': 'orders'})])
        with self.assertRaises(Refusal):
            self.app.handle('POST', '/v1/data/query', CHILD, {'child_id': 'other', 'table': 'orders'})
        with self.assertRaises(Refusal):
            self.app.handle('POST', '/v1/data/query', PARENT, {'child_id': 'other', 'table': 'orders'})
        self.app.handle('POST', '/v1/data/schema', PARENT, {'child_id': 'team-sandbox'})
        self.assertEqual(calls[-1], ('team-sandbox', {}))

    def test_http_parent_child_roundtrip_real_socket(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.application = self.app
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        def client(token):
            path = self.root / (token[0] + '.token'); path.write_text(token); path.chmod(0o600)
            return Client({'parent_id': 'firstmate', 'base_url': 'http://127.0.0.1:' + str(http.server_port), 'token_file': str(path)})
        try:
            parent, child = client('p' * 48), client('c' * 48)
            request = self.request()
            parent.call('POST', '/v1/children/team-sandbox/requests', request)
            child.call('POST', '/v1/children/team-sandbox/reports', {'event_id': str(uuid.uuid4()), 'kind': 'done',
                       'text': 'HTTP roundtrip outcome', 'request_id': request['request_id'], 'correlation': request['correlation']})
            events = parent.call('GET', '/v1/reports?after=0')['events']
            self.assertEqual(events[0]['text'], 'HTTP roundtrip outcome')
            with self.assertRaises(Refusal):
                child.call('POST', '/v1/children/team-sandbox/requests', self.request())
        finally:
            http.shutdown(); http.server_close(); thread.join()


class LifecycleTests(unittest.TestCase):
    def test_fixed_commands_and_active_work_gate(self):
        calls = []
        state = {'safe': False, 'lease': False, 'running': True, 'started': 't1'}
        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[1] == 'inspect':
                body = {'id': 'a' * 64, 'running': state['running'], 'started_at': state['started'], 'status': 'running' if state['running'] else 'exited'}
            elif argv[1] == 'exec':
                if '--quiesce' in argv:
                    state['lease'] = True
                body = {'generation': 'nonce1', 'identity_verified': True, 'safe_to_stop': state['safe'],
                        'active_requests': 0 if state['safe'] else 1}
                if state['lease']:
                    body['lease_id'] = 'lease1'
            elif argv[1] == 'restart':
                state['started'] = 't2'; body = {}
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, canonical(body), b'')
        lifecycle = Lifecycle(runner=runner)
        child = {'container': 'secondmate', 'home': '/home/nguye/team-sandbox'}
        with self.assertRaises(Refusal):
            lifecycle.execute(child, 'restart', 'nonce1')
        self.assertFalse(any(x[1] == 'restart' for x in calls))
        state['safe'] = True
        result = lifecycle.execute(child, 'restart', 'nonce1')
        self.assertEqual(result['agent_readiness'], 'not_yet_verified')
        self.assertTrue(any('--quiesce' in x for x in calls))
        self.assertEqual([x for x in calls if x[1] == 'restart'], [['/usr/bin/docker', 'restart', '--time', '20', 'a' * 64]])
        self.assertFalse(any('shell' in x or 'run' in x or 'create' in x for x in calls))


if __name__ == '__main__':
    unittest.main()
