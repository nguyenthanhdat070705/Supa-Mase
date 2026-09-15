"""Real loopback HTTP across parent and child modules; no external services."""
from dataclasses import replace
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'parent-control'), str(ROOT / 'runtime')]
from server import Application, Handler
from client import Client
from common import Refusal as ParentRefusal
from local_ops import apply_approval, export_brain, intake_reports
from bridge import Queue, Settings, atomic_json
from control_client import ControlClient, sync_brain
from errors import Refusal as ChildRefusal
from knowledge import propose


class ComponentContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.parent_home = self.root / 'parent'
        self.child_home = self.root / 'child'
        self.parent_home.mkdir()
        self.child_home.mkdir()
        paths = {}
        for identity, letter in [('parent', 'p'), ('child', 'c'), ('operator', 'o')]:
            path = self.root / (identity + '.token')
            path.write_text(letter * 48)
            path.chmod(0o600)
            paths[identity] = path
        self.cfg = Settings(self.child_home, 42, frozenset(),
                            control_token_file=paths['child'])
        self.config = {
            'version': 1, 'database': str(self.root / 'control.sqlite3'),
            'captain_user_id': 42,
            'parents': {'firstmate': {}},
            'children': {'team-sandbox': {'parent_id': 'firstmate',
                'container': 'fixture-child', 'home': str(self.child_home)}},
            'operators': {'fixture-operator': {'children': ['team-sandbox']}}}
        self.app = Application(self.config, {
            'p' * 48: {'role': 'parent', 'id': 'firstmate'},
            'c' * 48: {'role': 'child', 'id': 'team-sandbox'},
            'o' * 48: {'role': 'operator', 'id': 'fixture-operator'}},
            executor=self.runtime)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.application = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.cfg = replace(self.cfg, parent_url=url)
        self.parent = Client({'parent_id': 'firstmate', 'base_url': url,
                              'token_file': str(paths['parent'])})
        self.operator = Client({'parent_id': 'firstmate', 'base_url': url,
                                'token_file': str(paths['operator'])})
        self.child = ControlClient(self.cfg)
        queue = Queue(self.cfg)
        queue.db.close()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.db.close()
        for path in self.root.rglob('*'):
            if path.is_file():
                path.chmod(0o600)
        self.temp.cleanup()

    def runtime(self, child, operation, payload=None):
        self.assertEqual(operation, 'enqueue')
        self.assertEqual(child['container'], 'fixture-child')
        queue = Queue(self.cfg)
        try:
            return queue.enqueue_parent(payload)
        finally:
            queue.db.close()

    def test_parent_request_real_queue_child_report_and_parent_review_intake(self):
        request = {'request_id': str(uuid.uuid4()), 'correlation': '0123456789abcdef',
                   'body': 'Inspect synthetic fixture only.', 'scope': {'domain': 'test'}}
        endpoint = '/v1/children/team-sandbox/requests'
        result = self.parent.call('POST', endpoint, request)
        self.assertEqual(result['result']['status'], 'queued')
        uid = result['result']['update_id']
        self.assertLess(uid, 0)
        queue = Queue(self.cfg)
        try:
            self.assertEqual(queue.row(uid)['status'], 'pending')
            envelope = json.loads(queue.row(uid)['envelope'])
            self.assertEqual(envelope['parent_request']['body'], request['body'])
            self.assertFalse(envelope['routing']['approval'])
        finally:
            queue.db.close()
        again = self.parent.call('POST', endpoint, request)
        self.assertTrue(again['duplicate'])
        event_id = str(uuid.uuid4())
        self.child.report('Synthetic outcome.', 'done', request['request_id'],
                          request['correlation'], event_id)
        self.child.report('Synthetic outcome.', 'done', request['request_id'],
                          request['correlation'], event_id)
        batch = self.parent.call('GET', '/v1/reports?after=0')
        self.assertEqual(len(batch['events']), 1)
        receipt = intake_reports(self.parent_home, batch)
        self.assertEqual(len(receipt['stored']), 1)
        self.assertEqual(len(intake_reports(self.parent_home, batch)['stored']), 0)
        self.assertFalse((self.parent_home / 'data/learnings.md').exists())

    def test_child_proposal_operator_approval_and_selected_claim_promotion(self):
        source = self.child_home / 'proposal.json'
        source.write_text(json.dumps({'manifest': {
            'claims': [{'id': 'keep', 'text': 'Approved fixture fact.', 'evidence_ids': ['e1']},
                       {'id': 'leave', 'text': 'Unselected fixture fact.', 'evidence_ids': ['e1']}],
            'evidence': [{'id': 'e1', 'description': 'Synthetic fixture.', 'source': 'fixture://record'}],
            'scope': {'domain': 'test'}}, 'content': 'Private full notes stay in child.'}))
        notifications = []
        class Notifier:
            def send(self, chat, text):
                notifications.append((chat, text))
                return {'message_id': 1}
        submitted = propose(self.cfg, self.child, source, atomic_json, Notifier())
        propose(self.cfg, self.child, source, atomic_json, Notifier())
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][0], 42)
        self.assertIn(submitted['sha256'], notifications[0][1])
        endpoint = '/v1/children/team-sandbox/approvals'
        approval = {k: submitted[k] for k in ('proposal_id', 'version', 'sha256')}
        approval.update(approval_id=str(uuid.uuid4()), selected_claims=['keep'],
            destination='data/learnings.md', approval_reference={
                'captain_user_id': 42, 'source': 'synthetic-test-only',
                'reference': 'fixture-approval', 'quoted_approval': 'Approve selected fixture claim.'})
        with self.assertRaises(ChildRefusal):
            self.child.request('POST', endpoint, approval)
        with self.assertRaises(ParentRefusal):
            self.parent.call('POST', endpoint, approval)
        self.assertFalse((self.parent_home / 'data/learnings.md').exists())
        self.operator.call('POST', endpoint, approval)
        saved = self.parent.call('GET', '/v1/approvals/' + approval['approval_id'])['approval']
        receipt = apply_approval(self.parent_home, saved, 'firstmate')
        self.parent.call('POST', '/v1/approvals/' + approval['approval_id'] + '/receipt', receipt)
        self.assertEqual(apply_approval(self.parent_home, saved, 'firstmate'), receipt)
        learned = (self.parent_home / 'data/learnings.md').read_text()
        self.assertEqual(learned.count('Approved fixture fact.'), 1)
        self.assertNotIn('Unselected fixture fact.', learned)
        self.assertNotIn('Private full notes', learned)

    def test_parent_snapshot_is_consumed_by_real_child_sync(self):
        skill = self.parent_home / '.agents/skills/fixture-skill'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('Use only synthetic fixture records.\n')
        snapshot = export_brain(self.parent_home, ['fixture-skill'],
                                model='gpt-5.6-sol', effort='xhigh')
        self.parent.call('POST', '/v1/children/team-sandbox/brain', snapshot)
        receipt = sync_brain(self.cfg, self.child, atomic_json)
        self.assertEqual(receipt['revision'], snapshot['revision'])
        self.assertEqual(json.loads((self.child_home / 'config/inherited-runtime.json').read_text()),
                         {'model': 'gpt-5.6-sol', 'reasoning_effort': 'xhigh'})
        self.assertEqual((self.child_home / '.agents/skills/fixture-skill/SKILL.md').read_text(),
                         'Use only synthetic fixture records.\n')


if __name__ == '__main__':
    unittest.main()
