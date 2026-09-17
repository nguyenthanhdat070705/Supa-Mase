"""Offline tests for the exact child-home bind preparation helper."""

from __future__ import annotations

import errno
import importlib.util
import json
from pathlib import Path, PurePosixPath
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent.parent / 'prepare-home-bind.py'
SPEC = importlib.util.spec_from_file_location('prepare_home_bind', SCRIPT)
prepare_home_bind = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_home_bind)


FAKE_O_DIRECTORY = 1 << 20
FAKE_O_NOFOLLOW = 1 << 21
FAKE_O_CLOEXEC = 1 << 22
DEPLOYMENT_ROOT = '/srv/firstmate/children/team-sandbox'
CHILD_ID = 'team-sandbox'
CONTAINER = 'secondmate'


class FakeNoFollowFilesystem:
    """Small fd-relative directory model used on POSIX and Windows alike."""

    def __init__(self, *, final_mode=0o700, final_uid=1000, final_gid=1000,
                 final_present=True, symlink=None):
        self.nodes = {}
        self.descriptors = {}
        self.calls = []
        self.stat_calls = []
        self.synced = []
        self.next_descriptor = 10
        components = ('srv', 'firstmate', 'children', 'team-sandbox', 'home')
        current = PurePosixPath('/')
        self.nodes[current] = self.directory(0o755, 0, 0, 1)
        for index, component in enumerate(components, start=2):
            current /= component
            self.nodes[current] = self.directory(0o755, 0, 0, index)
        final = current / CHILD_ID
        if final_present:
            self.nodes[final] = self.directory(
                final_mode, final_uid, final_gid, len(components) + 2)
        if symlink is not None:
            target = PurePosixPath(symlink)
            self.nodes[target] = {'kind': 'symlink'}

    @staticmethod
    def directory(mode, uid, gid, inode):
        return {
            'kind': 'directory', 'mode': stat.S_IFDIR | mode,
            'uid': uid, 'gid': gid, 'device': 91, 'inode': inode,
        }

    def open(self, name, flags, mode=0o777, *, dir_fd=None):
        del mode
        if dir_fd is None:
            target = PurePosixPath(name)
        else:
            if dir_fd not in self.descriptors:
                raise OSError(errno.EBADF, 'bad fake descriptor')
            target = self.descriptors[dir_fd] / name
        self.calls.append((target, flags, dir_fd))
        node = self.nodes.get(target)
        if node is None:
            raise FileNotFoundError(errno.ENOENT, 'missing fake path', target.as_posix())
        if node['kind'] == 'symlink':
            if flags & FAKE_O_NOFOLLOW:
                raise OSError(errno.ELOOP, 'refused symlink', target.as_posix())
            raise AssertionError('test helper was opened without O_NOFOLLOW')
        descriptor = self.next_descriptor
        self.next_descriptor += 1
        self.descriptors[descriptor] = target
        return descriptor

    def close(self, descriptor):
        if descriptor not in self.descriptors:
            raise OSError(errno.EBADF, 'double close in fake filesystem')
        del self.descriptors[descriptor]

    def fstat(self, descriptor):
        path = self.descriptors[descriptor]
        node = self.nodes[path]
        return SimpleNamespace(
            st_mode=node['mode'], st_uid=node['uid'], st_gid=node['gid'],
            st_dev=node['device'], st_ino=node['inode'],
        )

    def stat(self, name, *, dir_fd=None, follow_symlinks=True):
        if dir_fd not in self.descriptors:
            raise OSError(errno.EBADF, 'bad fake descriptor')
        target = self.descriptors[dir_fd] / name
        self.stat_calls.append((target, dir_fd, follow_symlinks))
        node = self.nodes.get(target)
        if node is None:
            raise FileNotFoundError(errno.ENOENT, 'missing fake path', target.as_posix())
        if node['kind'] == 'symlink':
            if follow_symlinks:
                raise FileNotFoundError(errno.ENOENT, 'dangling fake symlink', target.as_posix())
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        return SimpleNamespace(
            st_mode=node['mode'], st_uid=node['uid'], st_gid=node['gid'],
            st_dev=node['device'], st_ino=node['inode'],
        )

    def fsync(self, descriptor):
        self.synced.append(self.descriptors[descriptor])


class PrepareFilesystemTests(unittest.TestCase):
    @staticmethod
    def prepare(filesystem, uid=1000, gid=1000):
        with (patch.object(prepare_home_bind.os, 'O_DIRECTORY', FAKE_O_DIRECTORY,
                           create=True),
              patch.object(prepare_home_bind.os, 'O_NOFOLLOW', FAKE_O_NOFOLLOW,
                           create=True),
              patch.object(prepare_home_bind.os, 'O_CLOEXEC', FAKE_O_CLOEXEC,
                           create=True),
              patch.object(prepare_home_bind.os, 'open', filesystem.open),
              patch.object(prepare_home_bind.os, 'close', filesystem.close),
              patch.object(prepare_home_bind.os, 'fstat', filesystem.fstat),
              patch.object(prepare_home_bind.os, 'fsync', filesystem.fsync)):
            return prepare_home_bind.prepare(
                DEPLOYMENT_ROOT, CHILD_ID, uid, gid)

    @staticmethod
    def require_absent(filesystem):
        with (patch.object(prepare_home_bind.os, 'O_DIRECTORY', FAKE_O_DIRECTORY,
                           create=True),
              patch.object(prepare_home_bind.os, 'O_NOFOLLOW', FAKE_O_NOFOLLOW,
                           create=True),
              patch.object(prepare_home_bind.os, 'O_CLOEXEC', FAKE_O_CLOEXEC,
                           create=True),
              patch.object(prepare_home_bind.os, 'open', filesystem.open),
              patch.object(prepare_home_bind.os, 'close', filesystem.close),
              patch.object(prepare_home_bind.os, 'stat', filesystem.stat),
              patch.object(prepare_home_bind.os, 'fsync', filesystem.fsync)):
            return prepare_home_bind.require_absent(DEPLOYMENT_ROOT, CHILD_ID)

    def test_exact_mode_owner_and_fd_relative_no_follow_walk_are_accepted(self):
        filesystem = FakeNoFollowFilesystem()
        result = self.prepare(filesystem)

        self.assertEqual(result, {
            'schema': 'fm-exact-home-bind.v1',
            'source': DEPLOYMENT_ROOT + '/home/' + CHILD_ID,
            'destination': '/home/nguye/' + CHILD_ID,
            'uid': 1000,
            'gid': 1000,
            'mode': '0700',
            'device': 91,
            'inode': 7,
        })
        self.assertEqual(
            filesystem.synced,
            [
                PurePosixPath(DEPLOYMENT_ROOT + '/home/' + CHILD_ID),
                PurePosixPath(DEPLOYMENT_ROOT + '/home'),
            ],
        )
        self.assertEqual(filesystem.descriptors, {})
        self.assertTrue(filesystem.calls)
        for _path, flags, _parent in filesystem.calls:
            self.assertTrue(flags & FAKE_O_DIRECTORY)
            self.assertTrue(flags & FAKE_O_NOFOLLOW)
            self.assertTrue(flags & FAKE_O_CLOEXEC)

    def test_intermediate_and_final_symlinks_are_rejected_without_fd_leaks(self):
        for label, symlink in (
                ('intermediate', '/srv/firstmate/children'),
                ('final', DEPLOYMENT_ROOT + '/home/' + CHILD_ID)):
            with self.subTest(label=label):
                filesystem = FakeNoFollowFilesystem(symlink=symlink)
                with self.assertRaises(OSError) as caught:
                    self.prepare(filesystem)
                self.assertEqual(caught.exception.errno, errno.ELOOP)
                self.assertEqual(filesystem.descriptors, {})

    def test_mode_0600_and_wrong_owner_are_rejected(self):
        cases = (
            ('mode', {'final_mode': 0o600}, 1000, 1000),
            ('uid', {'final_uid': 1001}, 1000, 1000),
            ('gid', {'final_gid': 1001}, 1000, 1000),
        )
        for label, options, uid, gid in cases:
            with self.subTest(label=label):
                filesystem = FakeNoFollowFilesystem(**options)
                with self.assertRaisesRegex(RuntimeError, 'mode 0700.*UID:GID'):
                    self.prepare(filesystem, uid, gid)
                self.assertEqual(filesystem.descriptors, {})
                self.assertEqual(filesystem.synced, [])

    def test_absent_exact_source_is_rejected(self):
        filesystem = FakeNoFollowFilesystem(final_present=False)
        with self.assertRaisesRegex(RuntimeError, 'Seed the exact child-home'):
            self.prepare(filesystem)
        self.assertEqual(filesystem.descriptors, {})

    def test_require_absent_accepts_only_enoent_and_fsyncs_parent(self):
        filesystem = FakeNoFollowFilesystem(final_present=False)
        result = self.require_absent(filesystem)

        self.assertEqual(result, {
            'schema': 'fm-absent-child-home.v1',
            'source': DEPLOYMENT_ROOT + '/home/' + CHILD_ID,
            'absent': True,
        })
        self.assertEqual(len(filesystem.stat_calls), 1)
        target, _parent_fd, follow_symlinks = filesystem.stat_calls[0]
        self.assertEqual(
            target, PurePosixPath(DEPLOYMENT_ROOT + '/home/' + CHILD_ID))
        self.assertFalse(follow_symlinks)
        self.assertEqual(
            filesystem.synced,
            [PurePosixPath(DEPLOYMENT_ROOT + '/home')],
        )
        self.assertEqual(filesystem.descriptors, {})
        for _path, flags, _parent in filesystem.calls:
            self.assertTrue(flags & FAKE_O_NOFOLLOW)

    def test_require_absent_refuses_every_existing_entry_including_dangling_symlink(self):
        final = PurePosixPath(DEPLOYMENT_ROOT + '/home/' + CHILD_ID)
        cases = []
        directory = FakeNoFollowFilesystem()
        cases.append(('directory', directory))
        regular = FakeNoFollowFilesystem()
        regular.nodes[final] = {
            'kind': 'file', 'mode': stat.S_IFREG | 0o600,
            'uid': 1000, 'gid': 1000, 'device': 91, 'inode': 88,
        }
        cases.append(('regular file', regular))
        cases.append((
            'dangling symlink',
            FakeNoFollowFilesystem(symlink=final.as_posix()),
        ))

        for label, filesystem in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(RuntimeError, 'entry already exists'):
                    self.require_absent(filesystem)
                self.assertEqual(filesystem.stat_calls[0][2], False)
                self.assertEqual(filesystem.synced, [])
                self.assertEqual(filesystem.descriptors, {})

    def test_require_absent_rejects_intermediate_symlink_during_no_follow_walk(self):
        filesystem = FakeNoFollowFilesystem(
            final_present=False, symlink='/srv/firstmate/children')
        with self.assertRaises(OSError) as caught:
            self.require_absent(filesystem)
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertEqual(filesystem.stat_calls, [])
        self.assertEqual(filesystem.descriptors, {})

    def test_deployment_root_must_be_canonical_and_scoped(self):
        for value in ('relative/root', '/', '/srv//firstmate',
                      '/srv/firstmate/../other', '/srv/firstmate,bad'):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    prepare_home_bind.prepare(value, CHILD_ID, 1000, 1000)


class PrepareCliTests(unittest.TestCase):
    @staticmethod
    def argv(*, allow_absent):
        value = [
            'prepare-home-bind.py',
            '--deployment-root', DEPLOYMENT_ROOT,
            '--child-id', CHILD_ID,
            '--container', CONTAINER,
        ]
        if allow_absent:
            value.append('--allow-absent')
        value.append('--require-absent')
        return value

    def invoke(self, *, allow_absent, status):
        absent_result = {
            'schema': 'fm-absent-child-home.v1',
            'source': DEPLOYMENT_ROOT + '/home/' + CHILD_ID,
            'absent': True,
        }
        with (patch.object(prepare_home_bind.sys, 'argv', self.argv(
                    allow_absent=allow_absent)),
              patch.object(prepare_home_bind.os, 'geteuid', return_value=0,
                           create=True),
              patch.object(prepare_home_bind, 'container_is_offline',
                           return_value=status) as offline,
              patch.object(prepare_home_bind, 'require_absent',
                           return_value=absent_result) as require,
              patch.object(prepare_home_bind, 'prepare') as prepare,
              patch('builtins.print') as output):
            if allow_absent and status == 'absent':
                prepare_home_bind.main()
                error = None
            else:
                with self.assertRaisesRegex(RuntimeError, 'proven first install') as caught:
                    prepare_home_bind.main()
                error = caught.exception
        return error, offline, require, prepare, output

    def test_require_absent_requires_flag_and_proven_absent_container(self):
        for label, allow_absent, status in (
                ('missing allow flag', False, 'absent'),
                ('stopped existing container', True, 'exited')):
            with self.subTest(label=label):
                error, offline, require, prepare, output = self.invoke(
                    allow_absent=allow_absent, status=status)
                self.assertIsNotNone(error)
                offline.assert_called_once_with(CONTAINER, allow_absent)
                require.assert_not_called()
                prepare.assert_not_called()
                output.assert_not_called()

    def test_require_absent_mode_runs_only_after_proven_first_install(self):
        error, offline, require, prepare, output = self.invoke(
            allow_absent=True, status='absent')
        self.assertIsNone(error)
        offline.assert_called_once_with(CONTAINER, True)
        require.assert_called_once_with(DEPLOYMENT_ROOT, CHILD_ID)
        prepare.assert_not_called()
        rendered = json.loads(output.call_args.args[0])
        self.assertEqual(rendered['schema'], 'fm-absent-child-home.v1')
        self.assertTrue(rendered['absent'])
        self.assertEqual(rendered['container'], CONTAINER)
        self.assertEqual(rendered['container_state'], 'absent')

    def test_cli_accepts_long_docker_name_but_rejects_invalid_container_names(self):
        long_name = 'c' * 255
        argv = [
            'prepare-home-bind.py',
            '--deployment-root', DEPLOYMENT_ROOT,
            '--child-id', CHILD_ID,
            '--container', long_name,
        ]
        prepared = {
            'schema': 'fm-exact-home-bind.v1',
            'source': DEPLOYMENT_ROOT + '/home/' + CHILD_ID,
            'destination': '/home/nguye/' + CHILD_ID,
            'uid': 1000,
            'gid': 1000,
            'mode': '0700',
            'device': 91,
            'inode': 7,
        }
        with (patch.object(prepare_home_bind.sys, 'argv', argv),
              patch.object(prepare_home_bind.os, 'geteuid', return_value=0,
                           create=True),
              patch.object(prepare_home_bind, 'container_is_offline',
                           return_value='exited') as offline,
              patch.object(prepare_home_bind, 'prepare',
                           return_value=prepared) as prepare,
              patch('builtins.print') as output):
            prepare_home_bind.main()
        self.assertEqual(len(long_name), 255)
        offline.assert_called_once_with(long_name, False)
        prepare.assert_called_once_with(DEPLOYMENT_ROOT, CHILD_ID, 1000, 1000)
        rendered = json.loads(output.call_args.args[0])
        self.assertEqual(rendered['container'], long_name)
        self.assertEqual(rendered['container_state'], 'exited')

        for invalid in ('--weird', 'bad/name', 'bad\x01name', 'x' * 256):
            with self.subTest(container=repr(invalid)), \
                    patch.object(prepare_home_bind.sys, 'argv', [
                        *argv[:-2], '--container=' + invalid,
                    ]), \
                    patch.object(prepare_home_bind.os, 'geteuid',
                                 return_value=0, create=True), \
                    patch.object(prepare_home_bind,
                                 'container_is_offline') as rejected_offline:
                with self.assertRaisesRegex(RuntimeError, 'Docker container name'):
                    prepare_home_bind.main()
                rejected_offline.assert_not_called()

    def test_cli_enforces_child_id_80_boundary_separately_from_docker_name(self):
        valid_child_id = 'c' * 80
        argv = [
            'prepare-home-bind.py',
            '--deployment-root', DEPLOYMENT_ROOT,
            '--child-id', valid_child_id,
            '--container', CONTAINER,
        ]
        prepared = {'schema': 'fm-exact-home-bind.v1'}
        with (patch.object(prepare_home_bind.sys, 'argv', argv),
              patch.object(prepare_home_bind.os, 'geteuid', return_value=0,
                           create=True),
              patch.object(prepare_home_bind, 'container_is_offline',
                           return_value='exited') as offline,
              patch.object(prepare_home_bind, 'prepare',
                           return_value=prepared) as prepare,
              patch('builtins.print')):
            prepare_home_bind.main()
        offline.assert_called_once_with(CONTAINER, False)
        prepare.assert_called_once_with(
            DEPLOYMENT_ROOT, valid_child_id, 1000, 1000)

        with (patch.object(prepare_home_bind.sys, 'argv', [
                    *argv[:4], 'c' * 81, *argv[5:],
                ]),
              patch.object(prepare_home_bind.os, 'geteuid', return_value=0,
                           create=True),
              patch.object(prepare_home_bind,
                           'container_is_offline') as rejected_offline):
            with self.assertRaisesRegex(RuntimeError, 'Child ID'):
                prepare_home_bind.main()
            rejected_offline.assert_not_called()


class PrepareContainerStateTests(unittest.TestCase):
    environment = {
        'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8',
    }

    @staticmethod
    def completed(argv, returncode=0, stdout=b''):
        return subprocess.CompletedProcess(argv, returncode, stdout, b'')

    def inspect_argv(self, name=CONTAINER):
        return [
            prepare_home_bind.DOCKER, 'inspect', '--type', 'container',
            '--format', '{{json .State}}', name,
        ]

    def listing_argv(self):
        return [
            prepare_home_bind.DOCKER, 'container', 'ls', '--all', '--no-trunc',
            '--format', '{{.Names}}',
        ]

    def assert_fixed_call(self, call, argv):
        self.assertEqual(call.args, (argv,))
        self.assertEqual(call.kwargs, {
            'stdin': subprocess.DEVNULL,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.DEVNULL,
            'check': False,
            'timeout': 20,
            'env': self.environment,
        })

    def test_created_and_exited_container_are_offline(self):
        for status in ('created', 'exited'):
            with self.subTest(status=status):
                state = {
                    'Running': False, 'Paused': False,
                    'Restarting': False, 'Status': status,
                }
                argv = self.inspect_argv()
                with patch.object(
                        prepare_home_bind.subprocess, 'run',
                        return_value=self.completed(argv, stdout=json.dumps(state).encode())) as run:
                    self.assertEqual(
                        prepare_home_bind.container_is_offline(CONTAINER, False), status)
                self.assertEqual(run.call_count, 1)
                self.assert_fixed_call(run.call_args, argv)

    def test_running_paused_and_restarting_container_are_refused(self):
        cases = {
            'running': {
                'Running': True, 'Paused': False,
                'Restarting': False, 'Status': 'running',
            },
            'paused': {
                'Running': False, 'Paused': True,
                'Restarting': False, 'Status': 'exited',
            },
            'restarting': {
                'Running': False, 'Paused': False,
                'Restarting': True, 'Status': 'exited',
            },
        }
        for label, state in cases.items():
            with self.subTest(label=label):
                argv = self.inspect_argv()
                with patch.object(
                        prepare_home_bind.subprocess, 'run',
                        return_value=self.completed(
                            argv, stdout=json.dumps(state).encode())) as run:
                    with self.assertRaisesRegex(RuntimeError, 'fully stopped'):
                        prepare_home_bind.container_is_offline(CONTAINER, False)
                self.assertEqual(run.call_count, 1)
                self.assert_fixed_call(run.call_args, argv)

    def test_failed_inspect_is_absent_only_with_flag_and_fixed_exact_name_listing(self):
        inspect_argv = self.inspect_argv('bot.1')
        listing_argv = self.listing_argv()
        inspect_failure = self.completed(inspect_argv, returncode=1)
        cases = (
            ('no allow-absent', False, self.completed(listing_argv), False),
            ('listing failed', True,
             self.completed(listing_argv, returncode=1), False),
            ('exact name present', True,
             self.completed(listing_argv, stdout=b'bot.1\n'), False),
            ('empty listing', True,
             self.completed(listing_argv, stdout=b''), True),
            ('other valid names', True,
             self.completed(listing_argv, stdout=b'other-bot\nbot.10\n'), True),
            ('unrelated valid long Docker name', True,
             self.completed(listing_argv, stdout=(b'x' * 200) + b'\n'), True),
            ('duplicate names', True,
             self.completed(listing_argv, stdout=b'other-bot\nother-bot\n'), False),
            ('non-ascii listing', True,
             self.completed(listing_argv, stdout=b'other\xff\n'), False),
            ('invalid listed name', True,
             self.completed(listing_argv, stdout=b'--weird\n'), False),
            ('control in listed name', True,
             self.completed(listing_argv, stdout=b'good\x01bad\n'), False),
            ('oversized listing', True,
             self.completed(listing_argv, stdout=b'x' * 1_048_577), False),
        )
        for label, allow_absent, listing, accepted in cases:
            with self.subTest(label=label):
                responses = [inspect_failure]
                if allow_absent:
                    responses.append(listing)
                with patch.object(
                        prepare_home_bind.subprocess, 'run',
                        side_effect=responses) as run:
                    if accepted:
                        self.assertEqual(
                            prepare_home_bind.container_is_offline(
                                'bot.1', allow_absent),
                            'absent')
                    else:
                        with self.assertRaises(RuntimeError):
                            prepare_home_bind.container_is_offline(
                                'bot.1', allow_absent)
                self.assert_fixed_call(run.call_args_list[0], inspect_argv)
                if allow_absent:
                    self.assertEqual(run.call_count, 2)
                    self.assert_fixed_call(run.call_args_list[1], listing_argv)
                else:
                    self.assertEqual(run.call_count, 1)

    def test_inspect_transport_failure_never_proves_absence(self):
        with patch.object(
                prepare_home_bind.subprocess, 'run',
                side_effect=OSError('docker unavailable')) as run:
            with self.assertRaisesRegex(RuntimeError, 'state is unknown'):
                prepare_home_bind.container_is_offline(CONTAINER, True)
        self.assertEqual(run.call_count, 1)
        self.assert_fixed_call(run.call_args, self.inspect_argv())


if __name__ == '__main__':
    unittest.main()
