"""Offline binding tests; optional real stock helper integration on Linux."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from binding import bind
from common import Refusal


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / 'child'; self.home.mkdir()
        (self.home / '.fm-secondmate-home').write_text('team-sandbox\n')
        (self.home / '.fm-secondmate-parent').write_bytes(b'schema=fm-secondmate-parent.v1\nroute=local\nparent_home=/home/nguye/provisioner\n')
        if os.name != 'posix':
            self.mock = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *args: None)})
            self.mock.start()

    def tearDown(self):
        if hasattr(self, 'mock'):
            self.mock.stop()
        self.temp.cleanup()

    def test_true_remote_filesystem_binding_without_fake_metadata(self):
        result = bind(self.home, 'firstmate', 'team-sandbox', 'http://host.docker.internal:8787')
        self.assertEqual((self.home / '.fm-secondmate-parent').read_text(), 'schema=fm-secondmate-parent.v1\nroute=remote\n')
        manifest = json.loads((self.home / 'data/parent-control-binding.json').read_text())
        self.assertEqual(manifest['parent_id'], 'firstmate')
        self.assertEqual(manifest['child_id'], 'team-sandbox')
        self.assertEqual(result['parent_report_path'], 'state/parent-replies.status')
        self.assertFalse(result['native_remote_backend'])
        self.assertFalse(list(self.home.glob('state/*.meta')))
        self.assertNotIn('parent_home', (self.home / '.fm-secondmate-parent').read_text())
        self.assertEqual(bind(self.home, 'firstmate', 'team-sandbox', 'http://host.docker.internal:8787'), result)
        with self.assertRaises(Refusal):
            bind(self.home, 'other-parent', 'team-sandbox', 'http://host.docker.internal:8787')

    @unittest.skipUnless(os.name == 'posix', 'OS file-lock integration requires Linux/POSIX')
    def test_live_service_lock_refuses_migration(self):
        import fcntl
        lock_path = self.home / 'state/secondmate-telegram/service.lock'
        lock_path.parent.mkdir(parents=True)
        with lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Refusal):
                bind(self.home, 'firstmate', 'team-sandbox', 'http://host.docker.internal:8787')
        self.assertIn('route=local', (self.home / '.fm-secondmate-parent').read_text())

    @unittest.skipUnless(os.name == 'posix' and os.environ.get('FM_TEST_FRAMEWORK_ROOT'), 'Set FM_TEST_FRAMEWORK_ROOT to the pinned framework checkout on Linux')
    def test_stock_root_and_parent_publisher_use_child_without_primary_mount(self):
        framework = Path(os.environ['FM_TEST_FRAMEWORK_ROOT']).resolve()
        for name in ['fm-wake-lib.sh', 'fm-parent-channel-lib.sh', 'fm-teardown.sh']:
            self.assertTrue((framework / 'bin' / name).is_file())
        bind(self.home, 'firstmate', 'team-sandbox', 'http://host.docker.internal:8787')
        script = '''set -eu
export FM_HOME="$1"
. "$2/bin/fm-wake-lib.sh"
. "$2/bin/fm-parent-channel-lib.sh"
test "$(fm_firstmate_root_home "$FM_HOME")" = "$FM_HOME"
test "$(fm_parent_channel_destination "$FM_HOME" "$FM_HOME/state")" = "$FM_HOME/state/parent-replies.status"
fm_parent_channel_report "$FM_HOME" "$FM_HOME/state" 'done: stock publisher reached child outbox'
fm_secondmate_parent_record_parse "$FM_HOME/.fm-secondmate-parent"
test "$FM_SECONDMATE_PARENT_ROUTE" = remote
test -z "$FM_SECONDMATE_PARENT_HOME"
'''
        result = subprocess.run(['bash', '-c', script, 'stock-binding-test', str(self.home), str(framework)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.home / 'state/parent-replies.status').read_text(), 'done: stock publisher reached child outbox\n')
        self.assertFalse(list(self.home.glob('state/*.meta')))


if __name__ == '__main__':
    unittest.main()
