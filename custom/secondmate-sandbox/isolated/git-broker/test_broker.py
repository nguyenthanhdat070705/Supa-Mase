"""Offline tests: local temporary repositories plus simulated GitHub subprocesses."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import broker


class ForcedCommandTests(unittest.TestCase):
    def test_exact_transport_forms(self):
        self.assertEqual(broker.transport_command("git-upload-pack 'team-sandbox-repo'"), 'upload-pack')
        self.assertEqual(broker.transport_command('git-receive-pack team-sandbox-repo'), 'receive-pack')

    def test_reject_injection_and_other_operations(self):
        for command in ('', 'sh', 'git-upload-pack /etc', 'git-upload-pack ../team-sandbox-repo',
                        'git-upload-pack team-sandbox-repo; id', 'git-upload-pack team-sandbox-repo && id',
                        'git-upload-pack "$(id)"', 'X=y git-upload-pack team-sandbox-repo',
                        'git-receive-pack --help team-sandbox-repo', 'publish refs/heads/sandbox/x',
                        "git-upload-pack 'team-sandbox-repo", 'git-upload-pack team-sandbox-repo\nid'):
            with self.subTest(command=command), self.assertRaises(broker.BrokerError):
                broker.transport_command(command)

    def test_environment_drops_untrusted_git_settings_and_tokens(self):
        config = broker.Config('/fixed/repo.git', '/fixed/state', ('/fixed/token',))
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'DO_NOT_INHERIT', 'GIT_CONFIG_COUNT': '99',
                                         'GIT_SSH_COMMAND': 'malicious', 'PYTHONPATH': 'malicious',
                                         'GIT_OBJECT_DIRECTORY': '/quarantine'}, clear=False):
            env = broker.environment(config)
            for key in ('GH_TOKEN', 'GIT_SSH_COMMAND', 'PYTHONPATH', 'GIT_OBJECT_DIRECTORY'):
                self.assertNotIn(key, env)
            self.assertEqual(broker.environment(config, quarantine=True)['GIT_OBJECT_DIRECTORY'], '/quarantine')


class RealGitValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.work = root / 'work'
        cls.bare = root / 'bare.git'
        cls.git_binary = shutil.which('git')
        if not cls.git_binary:
            raise unittest.SkipTest('git executable unavailable')
        cls.fixture_env = {key: os.environ[key] for key in ('PATH', 'SystemRoot', 'TEMP', 'TMP') if key in os.environ}
        cls.fixture_env.update({'HOME': str(root), 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                                'GIT_AUTHOR_NAME': 'Broker test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                                'GIT_COMMITTER_NAME': 'Broker test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'})

        def command(*args):
            return subprocess.run([cls.git_binary, *args], env=cls.fixture_env, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
        cls.command = staticmethod(command)
        command('init', '--bare', str(cls.bare))
        command('init', '-b', 'main', str(cls.work))
        command('-C', str(cls.work), 'commit', '--allow-empty', '-m', 'base')
        cls.base = command('-C', str(cls.work), 'rev-parse', 'HEAD')
        command('-C', str(cls.work), 'commit', '--allow-empty', '-m', 'advance')
        cls.advance = command('-C', str(cls.work), 'rev-parse', 'HEAD')
        command('-C', str(cls.work), 'checkout', '-b', 'divergent', cls.base)
        command('-C', str(cls.work), 'commit', '--allow-empty', '-m', 'other history')
        cls.divergent = command('-C', str(cls.work), 'rev-parse', 'HEAD')
        command('--git-dir=' + str(cls.bare), 'fetch', str(cls.work), '+refs/heads/*:refs/heads/fixture/*')
        cls.config = broker.Config(str(cls.bare), str(root), ('/unused/token',), git=cls.git_binary)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.env_patch = mock.patch.object(broker, 'environment', return_value=self.fixture_env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_creation_and_fast_forward(self):
        broker.validate_updates(self.config, [('0' * 40, self.base, 'refs/heads/sandbox/team/task')])
        broker.validate_updates(self.config, [(self.base, self.advance, 'refs/heads/sandbox/team/task')])

    def test_main_tags_arbitrary_and_invalid_names_denied(self):
        for ref in ('refs/heads/main', 'refs/tags/sandbox/x', 'refs/replace/abc', 'refs/heads/sandbox/',
                    'refs/heads/sandbox/a..b', 'refs/heads/sandbox/a.lock', 'refs/heads/sandbox/x//y',
                    'refs/heads/sandbox/-option', 'refs/heads/sandbox/x\nmain'):
            with self.subTest(ref=ref), self.assertRaises(broker.BrokerError):
                broker.validate_updates(self.config, [('0' * 40, self.base, ref)])

    def test_delete_rewind_and_divergence_denied(self):
        for old, new in ((self.base, '0' * 40), (self.advance, self.base), (self.advance, self.divergent)):
            with self.subTest(old=old, new=new), self.assertRaises(broker.BrokerError):
                broker.validate_updates(self.config, [(old, new, 'refs/heads/sandbox/x')])

    def test_tree_object_denied(self):
        tree = self.command('--git-dir=' + str(self.bare), 'rev-parse', self.base + '^{tree}')
        with self.assertRaises(broker.BrokerError):
            broker.validate_updates(self.config, [('0' * 40, tree, 'refs/heads/sandbox/x')])

    def test_atomic_pre_receive_validation_rejects_mixed_batch(self):
        with self.assertRaises(broker.BrokerError):
            broker.validate_updates(self.config, [('0' * 40, self.base, 'refs/heads/sandbox/x'),
                                                  ('0' * 40, self.base, 'refs/heads/main')])


class PublishTests(unittest.TestCase):
    TOKEN = 'FAKE_TEST_TOKEN_NEVER_REAL'
    URL = 'https://github.com/DemandPlanningMC/demand-planning-maycha/pull/42'
    COMMIT = 'a' * 40

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = broker.Config('/fixed/bare.git', self.temp.name, ('/fixed/token',))
        self.calls = []
        self.lists = [[], [{'url': self.URL}]]
        self.push_returncode = 0
        self.lookup_returncode = 0
        self.create_returncode = 0
        self.stderr = io.StringIO()
        for patch in (mock.patch.object(broker, 'run', side_effect=self.fake_run),
                      mock.patch.object(broker, 'publish_lock', return_value=contextlib.nullcontext()),
                      contextlib.redirect_stderr(self.stderr)):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)

    def fake_run(self, argv, env, timeout=120):
        self.calls.append((argv, env))
        code, out = 0, ''
        if argv == ['/fixed/token']:
            out = self.TOKEN + '\n'
        elif 'rev-parse' in argv:
            out = self.COMMIT + '\n'
        elif 'push' in argv:
            code = self.push_returncode
        elif argv[1:3] == ['pr', 'list']:
            code = self.lookup_returncode
            out = json.dumps(self.lists.pop(0) if len(self.lists) > 1 else self.lists[0])
        elif argv[1:3] == ['pr', 'create']:
            code = self.create_returncode
            out = self.URL + '\n'
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr='Sensitive error ' + self.TOKEN)

    def status(self):
        return json.loads(next(Path(self.temp.name).glob('*.json')).read_text())

    def test_push_exact_ref_and_create_pr_without_token_in_argv_or_status(self):
        result = broker.publish(self.config, 'refs/heads/sandbox/team/task')
        self.assertEqual(result['stage'], 'pr_ready')
        pushes = [argv for argv, env in self.calls if 'push' in argv]
        self.assertEqual(pushes[0][-2:], [broker.UPSTREAM, self.COMMIT + ':refs/heads/sandbox/team/task'])
        self.assertNotIn('--force', pushes[0])
        self.assertNotIn(self.TOKEN, json.dumps([argv for argv, env in self.calls]))
        self.assertNotIn(self.TOKEN, json.dumps(self.status()) + self.stderr.getvalue())
        self.assertEqual([env['GH_TOKEN'] for argv, env in self.calls if argv[1:3] == ['pr', 'create']], [self.TOKEN])

    def test_retry_reuses_open_pr(self):
        self.lists = [[{'url': self.URL}]]
        broker.publish(self.config, 'refs/heads/sandbox/x')
        broker.publish(self.config, 'refs/heads/sandbox/x')
        self.assertFalse(any(argv[1:3] == ['pr', 'create'] for argv, env in self.calls))

    def test_push_failure_does_not_call_github_or_claim_success(self):
        self.push_returncode = 1
        with self.assertRaises(broker.BrokerError):
            broker.publish(self.config, 'refs/heads/sandbox/x')
        self.assertEqual(self.status()['stage'], 'received')
        self.assertFalse(any(argv[0] == self.config.gh for argv, env in self.calls))
        self.assertNotIn(self.TOKEN, json.dumps(self.status()) + self.stderr.getvalue())

    def test_pr_failure_preserves_pushed_stage(self):
        self.lookup_returncode = 1
        with self.assertRaises(broker.BrokerError):
            broker.publish(self.config, 'refs/heads/sandbox/x')
        self.assertEqual(self.status()['stage'], 'upstream_pushed')

    def test_create_race_reuses_pr_after_nonzero_create(self):
        self.create_returncode = 1
        self.assertEqual(broker.publish(self.config, 'refs/heads/sandbox/x')['pr_url'], self.URL)

    def test_untrusted_pr_url_rejected(self):
        self.lists = [[{'url': 'https://example.invalid/' + self.TOKEN}]]
        with self.assertRaises(broker.BrokerError):
            broker.publish(self.config, 'refs/heads/sandbox/x')
        self.assertNotIn(self.TOKEN, json.dumps(self.status()) + self.stderr.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
