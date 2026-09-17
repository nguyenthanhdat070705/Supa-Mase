"""Offline tests for the two-child scoped administration boundary.

No test in this module talks to Docker, Telegram, or the network.  The fake
Docker runner deliberately accepts only the fixed argv surfaces implemented by
``Administration`` so a future broad shell/container-name regression is loud.
"""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import client as parent_client
from administration import Administration
from common import Refusal, canonical
from server import Application


ORIGINAL_PATH_LSTAT = Path.lstat


def secure_test_lstat(path):
    """Model root-created 0600 cidfiles on Windows, which reports mode 0666."""
    info = ORIGINAL_PATH_LSTAT(path)
    if ((path.name.startswith('.helper.') and path.name.endswith('.cid'))
            or (path.name.startswith('.recovery.') and path.name.endswith('.json'))):
        return SimpleNamespace(
            st_mode=info.st_mode & ~0o077, st_uid=0,
            st_nlink=1, st_size=info.st_size,
        )
    return info


DOCKER = '/usr/bin/docker'
GIB = 1024 ** 3
FIN_ID = 'a' * 64
TOAN_ID = 'b' * 64
FIN_IMAGE = 'sha256:' + '1' * 64
TOAN_IMAGE = 'sha256:' + '2' * 64
CONFIG_SECRET = 'broker-secret-value-123456789'

PARENT = {'role': 'parent', 'id': 'firstmate'}
FOREIGN_PARENT = {'role': 'parent', 'id': 'other-parent'}
OPERATOR = {'role': 'operator', 'id': 'captain-operator'}
FIN_CHILD = {'role': 'child', 'id': 'team-sandbox'}
TOAN_CHILD = {'role': 'child', 'id': 'toanmytran-bot'}
THIRD_CHILD = {'role': 'child', 'id': 'third-bot'}


def children():
    return {
        'team-sandbox': {
            'parent_id': 'firstmate', 'container': 'secondmate',
            'home': '/home/nguye/team-sandbox', 'exec_user': '1000:1000',
        },
        'toanmytran-bot': {
            'parent_id': 'firstmate', 'container': 'toanmytran-bot',
            'home': '/home/nguye/toanmytran-bot', 'exec_user': '1000:1000',
        },
        # A registered sibling is intentionally not in the administration
        # allowlist.  This proves registry membership alone grants no power.
        'third-bot': {
            'parent_id': 'firstmate', 'container': 'thirdmate',
            'home': '/home/nguye/third-bot', 'exec_user': '1000:1000',
        },
    }


def expected_environment():
    return {
        'HOME': '/home/nguye',
        'SM_INSTANCE_FILE': '/opt/secondmate/instance.json',
        'SM_CAPTAIN_ID': '123456789',
        'SM_TEAM_GROUP_IDS': '-1001234567890,-1009876543210',
        'SM_FORBIDDEN_BOT_ID': '999999999',
        'SM_BOT_TOKEN_FILE': '/run/secrets/secondmate-bot-token',
        'DEBIAN_FRONTEND': 'noninteractive',
        'TZ': 'Asia/Bangkok',
    }


def expected_networks(child_id):
    network_id = ('c' if child_id == 'team-sandbox' else 'd') * 64
    return {'fleet-' + child_id: network_id}


def manifest(child_id, container, home, image, *, start_allowed=True):
    source_root = '/srv/firstmate/children/' + child_id
    return {
        'container': container,
        'pinned_image_id': image,
        'expected_user': '1000:1000',
        'expected_mounts': [
            {'source': source_root + '/parent-home', 'destination': '/home/nguye',
             'rw': True, 'type': 'bind'},
            {'source': source_root + '/home', 'destination': home,
             'rw': True, 'type': 'bind'},
            {'source': '/srv/firstmate/secrets/' + child_id + '-token',
             'destination': '/run/secrets/secondmate-bot-token',
             'rw': False, 'type': 'bind'},
            {'source': source_root + '/runtime', 'destination': '/opt/secondmate',
             'rw': False, 'type': 'bind'},
        ],
        'expected_labels': {
            'com.firstmate.managed-child': 'true',
            'com.firstmate.child-id': child_id,
        },
        'expected_environment': expected_environment(),
        'expected_networks': expected_networks(child_id),
        'expected_log_config': {'max-size': '10m', 'max-file': '3'},
        'expected_restart_policy': 'unless-stopped' if start_allowed else 'no',
        'start_allowed': start_allowed,
        'resource_profiles': {
            'normal': {'memory_bytes': GIB, 'nano_cpus': 1_000_000_000, 'pids_limit': 256},
            'large': {'memory_bytes': 2 * GIB, 'nano_cpus': 2_000_000_000, 'pids_limit': 512},
        },
        'backup_paths': ['config', 'data', 'state'],
        'max_backup_bytes': 64 * 1024 ** 2,
        'runbooks': ['runtime-health', 'home-usage', 'processes'],
    }


def admin_config():
    return {
        'children': {
            'team-sandbox': manifest(
                'team-sandbox', 'secondmate', '/home/nguye/team-sandbox', FIN_IMAGE),
            'toanmytran-bot': manifest(
                'toanmytran-bot', 'toanmytran-bot', '/home/nguye/toanmytran-bot',
                TOAN_IMAGE, start_allowed=False),
        },
        'max_log_bytes': 16_384,
        'reserve_bytes': 10 * GIB,
        'reserve_percent': 10,
    }


def recovery_marker(child_id, operation_id, container_id, image_id, *,
                    generation='runtime:g1', lease_id='lease-1',
                    stop_completed, restored=False):
    return {
        'schema': 'firstmate-backup-recovery.v1',
        'child_id': child_id,
        'operation_id': operation_id,
        'container_id': container_id,
        'image_id': image_id,
        'runtime_generation': generation,
        'lease_id': lease_id,
        'verified_running': True,
        'stop_intent': True,
        'stop_completed': stop_completed,
        'restored': restored,
    }


def inspect_value(child_id, container, home, image, container_id, *, running=True):
    source_root = '/srv/firstmate/children/' + child_id
    return {
        'Id': container_id,
        'Image': image,
        'Name': '/' + container,
        'Created': '2026-09-17T01:00:00Z',
        'Config': {
            'User': '1000:1000',
            'Entrypoint': ['/bin/bash', '/opt/secondmate/entrypoint.sh'],
            'Env': [f'{name}={value}' for name, value in expected_environment().items()],
            'Labels': {
                'com.firstmate.managed-child': 'true',
                'com.firstmate.child-id': child_id,
            },
        },
        'HostConfig': {
            'Privileged': False,
            'PidMode': '',
            'IpcMode': 'private',
            'UTSMode': '',
            'NetworkMode': 'fleet-' + child_id,
            'CapAdd': None,
            'CapDrop': ['ALL'],
            'SecurityOpt': ['no-new-privileges:true'],
            'PublishAllPorts': False,
            'PortBindings': {},
            'Devices': [],
            'DeviceRequests': [],
            'AutoRemove': False,
            'Init': True,
            'Tmpfs': {'/tmp': 'rw,noexec,nosuid,size=67108864'},
            'Memory': GIB,
            'NanoCpus': 1_000_000_000,
            'PidsLimit': 256,
            'RestartPolicy': {
                'Name': 'no' if child_id == 'toanmytran-bot' else 'unless-stopped',
            },
            'LogConfig': {
                'Type': 'json-file',
                'Config': {'max-size': '10m', 'max-file': '3'},
            },
        },
        'State': {
            'Running': running,
            'Paused': False,
            'Status': 'running' if running else 'exited',
            'StartedAt': '2026-09-17T01:00:01Z' if running else '',
        },
        'Mounts': [
            {'Source': source_root + '/parent-home', 'Destination': '/home/nguye',
             'RW': True, 'Type': 'bind', 'Propagation': 'rprivate'},
            {'Source': source_root + '/home', 'Destination': home,
             'RW': True, 'Type': 'bind', 'Propagation': 'rprivate'},
            {'Source': '/srv/firstmate/secrets/' + child_id + '-token',
             'Destination': '/run/secrets/secondmate-bot-token',
             'RW': False, 'Type': 'bind', 'Propagation': 'rprivate'},
            {'Source': source_root + '/runtime', 'Destination': '/opt/secondmate',
             'RW': False, 'Type': 'bind', 'Propagation': 'rprivate'},
        ],
        'NetworkSettings': {
            'Networks': {
                name: {'NetworkID': network_id}
                for name, network_id in expected_networks(child_id).items()
            },
        },
    }


class FakeDocker:
    """A strict subprocess.run replacement for the administrative catalog."""

    def __init__(self):
        self.calls = []
        self.log_output = b''
        self.tar_output = b'bounded-backup-archive'
        self.log_processes = []
        self.helper_processes = []
        self.helper_values = {}
        self.removed_helpers = []
        self.helper_returncode = 0
        self.helper_timeout = False
        self.helper_mutator = None
        self.block_first_helper = False
        self.first_helper_started = threading.Event()
        self.release_first_helper = threading.Event()
        self.helper_launches = 0
        self.fail_operations = set()
        self.stop_on_failed_stop = False
        self.start_hook = None
        self.inspect_hook = None
        self.network_ids = {
            name: network_id
            for child_id in ('team-sandbox', 'toanmytran-bot')
            for name, network_id in expected_networks(child_id).items()
        }
        self.inspect_values = {
            'secondmate': inspect_value(
                'team-sandbox', 'secondmate', '/home/nguye/team-sandbox', FIN_IMAGE, FIN_ID),
            'toanmytran-bot': inspect_value(
                'toanmytran-bot', 'toanmytran-bot', '/home/nguye/toanmytran-bot',
                TOAN_IMAGE, TOAN_ID),
        }

    def _value_for_target(self, target):
        if target in self.inspect_values:
            return self.inspect_values[target]
        if target in self.helper_values:
            return self.helper_values[target]
        for value in self.inspect_values.values():
            if value['Id'] == target:
                return value
        raise AssertionError('Unexpected container target: ' + repr(target))

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if not argv or argv[0] != DOCKER:
            raise AssertionError('Unexpected executable: ' + repr(argv))
        operation = argv[1]
        if operation == 'inspect':
            if argv[2:4] != ['--format', '{{json .}}']:
                raise AssertionError('Inspect escaped the fixed template: ' + repr(argv))
            value = self._value_for_target(argv[4])
            if self.inspect_hook is not None:
                self.inspect_hook(argv[4], value)
            output = json.dumps(value).encode()
        elif operation == 'stats':
            if argv[2:5] != ['--no-stream', '--format', '{{json .}}']:
                raise AssertionError('Stats escaped the fixed template: ' + repr(argv))
            self._value_for_target(argv[5])
            output = b'{"CPUPerc":"0.10%","MemUsage":"10MiB / 1GiB"}'
        elif operation == 'logs':
            raise AssertionError('Logs must use the bounded streaming popen path.')
        elif operation == 'top':
            self._value_for_target(argv[2])
            if argv[3:] != ['-eo', 'pid,user,comm']:
                raise AssertionError('Top escaped the fixed field list: ' + repr(argv))
            output = b'PID USER COMMAND\n1 1000 python3\n'
        elif operation == 'exec':
            if '/usr/bin/du' not in argv:
                raise AssertionError('Unexpected exec surface: ' + repr(argv))
            marker = argv.index('--one-file-system')
            paths = argv[marker + 1:]
            amount = 4096 if '-sb' in argv else 4
            output = ''.join(f'{amount}\t{path}\n' for path in paths).encode()
        elif operation == 'update':
            value = self._value_for_target(argv[-1])
            expected_prefix = [DOCKER, 'update', '--memory']
            if argv[:3] != expected_prefix:
                raise AssertionError('Update escaped the fixed surface: ' + repr(argv))
            host = value['HostConfig']
            host['Memory'] = int(argv[3])
            host['NanoCpus'] = int(float(argv[5]) * 1_000_000_000)
            host['PidsLimit'] = int(argv[7])
            output = b''
        elif operation == 'stop':
            if argv[2:4] != ['--time', '20'] or len(argv) != 5:
                raise AssertionError('Stop must use --time 20 and an inspected ID.')
            value = self._value_for_target(argv[4])
            failed = operation in self.fail_operations
            if not failed or self.stop_on_failed_stop:
                value['State'].update({'Running': False, 'Status': 'exited', 'Paused': False})
            if failed:
                return subprocess.CompletedProcess(argv, 1, b'', b'withheld')
            output = (argv[4] + '\n').encode()
        elif operation == 'start':
            if len(argv) != 3:
                raise AssertionError('Start accepts only an inspected ID.')
            value = self._value_for_target(argv[2])
            if operation in self.fail_operations:
                return subprocess.CompletedProcess(argv, 1, b'', b'withheld')
            value['State'].update({
                'Running': True, 'Status': 'running', 'Paused': False,
                'StartedAt': '2026-09-17T02:00:00Z',
            })
            if self.start_hook is not None:
                self.start_hook(value)
            output = (argv[2] + '\n').encode()
        elif operation == 'rm':
            if len(argv) != 4 or argv[2] != '-f':
                raise AssertionError('Helper cleanup must be rm -f of an attested ID.')
            helper_id = argv[3]
            if helper_id not in self.helper_values:
                raise AssertionError('Cleanup target was not an attested helper ID.')
            if operation in self.fail_operations:
                return subprocess.CompletedProcess(argv, 1, b'', b'withheld')
            self.removed_helpers.append(helper_id)
            output = (helper_id + '\n').encode()
        elif operation == 'ps':
            if '--format' not in argv or argv[-2:] != ['--format', '{{.ID}}']:
                raise AssertionError('Helper lookup escaped its fixed ID-only format.')
            filters = [argv[index + 1] for index, value in enumerate(argv) if value == '--filter']
            active = []
            for helper_id, helper in self.helper_values.items():
                if helper_id in self.removed_helpers:
                    continue
                labels = helper['Config']['Labels']
                matches = True
                for value in filters:
                    if value.startswith('name=^/'):
                        matches &= helper['Name'] == value.removeprefix('name=^').removesuffix('$')
                    elif value.startswith('label='):
                        name, setting = value.removeprefix('label=').split('=', 1)
                        matches &= labels.get(name) == setting
                if matches:
                    active.append(helper_id)
            output = ''.join(helper_id + '\n' for helper_id in active).encode()
        elif operation == 'network':
            if (len(argv) != 6
                    or argv[2:5] != ['inspect', '--format', '{{.Id}}']
                    or argv[5] not in self.network_ids):
                raise AssertionError(
                    'Network lookup escaped the fixed ID-only format: ' + repr(argv))
            output = (self.network_ids[argv[5]] + '\n').encode()
        else:
            raise AssertionError('Unexpected Docker operation: ' + repr(argv))
        return subprocess.CompletedProcess(argv, 0, output, b'')

    def popen(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[0:2] == [DOCKER, 'logs']:
            self._value_for_target(argv[-1])
            if kwargs.get('stdout') != subprocess.PIPE or kwargs.get('stderr') != subprocess.STDOUT:
                raise AssertionError('Logs must combine output into one bounded stream.')
            process = FakeProcess(self.log_output)
            self.log_processes.append(process)
        elif argv[0:2] == [DOCKER, 'run']:
            if kwargs.get('stdout') != subprocess.PIPE or kwargs.get('stderr') != subprocess.DEVNULL:
                raise AssertionError('Backup helper output surface is not locked down.')
            helper_name = argv[argv.index('--name') + 1]
            cidfile = Path(argv[argv.index('--cidfile') + 1])
            helper_id = f'{len(self.helper_values) + 10:064x}'
            labels = {}
            for index, value in enumerate(argv):
                if value == '--label':
                    name, setting = argv[index + 1].split('=', 1)
                    labels[name] = setting
            image = next(value for value in argv if value in (FIN_IMAGE, TOAN_IMAGE))
            image_index = argv.index(image)
            mount_spec = argv[argv.index('--mount') + 1]
            mount_parts = dict(
                part.split('=', 1) for part in mount_spec.split(',') if '=' in part)
            self.helper_values[helper_id] = {
                'Id': helper_id, 'Name': '/' + helper_name, 'Image': image,
                'Config': {
                    'User': argv[argv.index('--user') + 1],
                    'Entrypoint': [argv[argv.index('--entrypoint') + 1]],
                    'Cmd': argv[image_index + 1:],
                    'Labels': labels,
                },
                'HostConfig': {
                    'NetworkMode': 'none', 'Privileged': False,
                    'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                    'CapAdd': None,
                    'SecurityOpt': ['no-new-privileges:true'],
                    'LogConfig': {'Type': 'none'},
                    'PidsLimit': 64, 'Memory': 268435456,
                    'NanoCpus': 500000000, 'AutoRemove': False,
                    'VolumesFrom': [],
                    'PortBindings': {}, 'Devices': [], 'DeviceRequests': [],
                },
                'Mounts': [{
                    'Source': mount_parts['source'],
                    'Destination': mount_parts['target'],
                    'RW': False, 'Type': 'bind', 'Propagation': 'rprivate',
                }],
            }
            if self.helper_mutator is not None:
                self.helper_mutator(self.helper_values[helper_id])
            self.helper_launches += 1
            if self.block_first_helper and self.helper_launches == 1:
                self.first_helper_started.set()
                if not self.release_first_helper.wait(5):
                    raise AssertionError('Timed out waiting to release first backup helper.')
            cidfile.write_text(helper_id + '\n')
            process = FakeProcess(
                self.tar_output, returncode=self.helper_returncode,
                timeout_once=self.helper_timeout)
            self.helper_processes.append(process)
        else:
            raise AssertionError('Unexpected streamed Docker operation: ' + repr(argv))
        return process

    def argv(self):
        return [call[0] for call in self.calls]


class RecordingBytesIO(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FakeProcess:
    def __init__(self, output, returncode=0, timeout_once=False):
        self.stdout = RecordingBytesIO(output)
        self.returncode = returncode
        self.killed = False
        self.timeout_once = timeout_once

    def wait(self, timeout=None):
        if timeout is not None and self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired('docker run', timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeLifecycle:
    def __init__(self):
        self.generation = 'runtime:g1'
        self.safe = True
        self.identity_verified = True
        self.lease_id = None
        self.change_after_quiesce = False
        self.resume_ok = True
        self.status_calls = []
        self.runtime_calls = []
        self.execute_calls = []

    def status(self, child):
        self.status_calls.append(child['container'])
        value = {
            'generation': self.generation,
            'identity_verified': self.identity_verified,
            'safe_to_stop': self.safe,
            'running': True,
        }
        if self.lease_id:
            value['lease_id'] = self.lease_id
        return value

    def runtime(self, child, *arguments):
        self.runtime_calls.append((child['container'], arguments))
        if arguments[0] == 'control-check':
            if '--quiesce' in arguments:
                self.lease_id = 'lease-1'
            value = {
                'generation': self.generation,
                'identity_verified': self.identity_verified,
                'safe_to_stop': self.safe,
            }
            if self.lease_id:
                value['lease_id'] = (
                    'changed-lease' if self.change_after_quiesce
                    and '--quiesce' not in arguments else self.lease_id)
            return value
        if arguments[0] == 'control-resume':
            if arguments != ('control-resume', '--lease-id', self.lease_id):
                raise AssertionError('Resume did not use the exact lease.')
            if self.resume_ok:
                self.lease_id = None
            return {'resumed': self.resume_ok}
        raise AssertionError('Unexpected runtime call: ' + repr(arguments))

    def execute(self, child, action, expected_generation, recovery=False):
        self.execute_calls.append((child['container'], action, expected_generation, recovery))
        return {'action': action}


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.children = children()
        self.docker = FakeDocker()
        self.lifecycle = FakeLifecycle()
        self.admin = Administration(
            copy.deepcopy(admin_config()), self.children, self.lifecycle,
            docker=DOCKER, runner=self.docker, popen=self.docker.popen,
            secret_values=(CONFIG_SECRET,),
        )
        # Never let unit-test recovery inspect a real production backup tree.
        # This matters on Linux hosts where /var/lib/firstmate-control exists
        # and is intentionally unreadable to the unprivileged test user.
        self.admin.backup_root = self.root / 'backups'
        self.config = {
            'version': 1,
            'database': str(self.root / 'control.sqlite3'),
            'captain_user_id': 123,
            'parents': {'firstmate': {'can_approve': False},
                        'other-parent': {'can_approve': False}},
            'children': self.children,
            'operators': {
                'captain-operator': {
                    'children': ['team-sandbox', 'toanmytran-bot'],
                    'allow_recovery': True,
                },
            },
        }
        self.app = Application(
            self.config, {}, lifecycle=self.lifecycle, administration=self.admin)

    def tearDown(self):
        if self.app is not None:
            self.app.db.close()
        self.temp.cleanup()

    def refusal(self, code, callable_, *args, **kwargs):
        with self.assertRaises(Refusal) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_configuration_is_an_explicit_two_child_allowlist(self):
        self.assertEqual(set(self.admin.manifests), {'team-sandbox', 'toanmytran-bot'})

        for root in (
                '/var/lib/firstmate-control/backups/../../../../etc/escaped',
                '/var/lib/firstmate-control/backups//noncanonical',
                '/var/lib/firstmate-control/backups/'):
            with self.subTest(backup_root=root):
                bad = admin_config()
                bad['backup_root'] = root
                self.refusal(
                    'refused', Administration, bad,
                    self.children, self.lifecycle)

        bad = admin_config()
        bad['children']['missing-child'] = manifest(
            'missing-child', 'missing', '/home/nguye/missing', 'sha256:' + '3' * 64)
        self.refusal('refused', Administration, bad, self.children, self.lifecycle)

        bad = admin_config()
        bad['children']['team-sandbox']['container'] = 'pgadmin-web'
        self.refusal('refused', Administration, bad, self.children, self.lifecycle)

        bad = admin_config()
        bad['children']['team-sandbox']['expected_mounts'].append(
            {'source': '/var/run/docker.sock', 'destination': '/var/run/docker.sock',
             'rw': True, 'type': 'bind'})
        self.refusal('refused', Administration, bad, self.children, self.lifecycle)

        bad = admin_config()
        bad['children']['team-sandbox']['arbitrary_command'] = True
        self.refusal('refused', Administration, bad, self.children, self.lifecycle)

        bad = admin_config()
        bad['max_log_bytes'] = 262_145
        self.refusal('refused', Administration, bad, self.children, self.lifecycle)

    def test_configuration_rejects_option_like_and_control_network_names(self):
        for name in ('--weird', 'fleet\x00team', 'fleet\nteam'):
            with self.subTest(name=repr(name)):
                bad = admin_config()
                bad['children']['team-sandbox']['expected_networks'] = {
                    name: 'c' * 64,
                }
                self.refusal(
                    'refused', Administration, bad,
                    self.children, self.lifecycle)

    def test_backup_home_requires_and_selects_the_exact_nested_bind(self):
        child = self.children['team-sandbox']
        manifest_value = self.admin.manifests['team-sandbox']

        self.assertEqual(child['home'], '/home/nguye/team-sandbox')
        self.assertEqual(
            self.admin._backup_home_mount(child, manifest_value),
            {
                'source': '/srv/firstmate/children/team-sandbox/home',
                'destination': '/home/nguye/team-sandbox',
                'rw': True,
                'type': 'bind',
            },
        )

        identity = self.admin._inspect('team-sandbox', child)
        self.assertIn(
            ('/srv/firstmate/children/team-sandbox/parent-home',
             '/home/nguye', True, 'bind'),
            identity['mounts'],
        )
        self.assertIn(
            ('/srv/firstmate/children/team-sandbox/home',
             '/home/nguye/team-sandbox', True, 'bind'),
            identity['mounts'],
        )
        self.assertEqual(
            self.admin._backup_command(child, manifest_value)[-8:],
            ['-C', '/home/nguye/team-sandbox', '-cf', '-', '--',
             'config', 'data', 'state'],
        )

        ancestor_only = admin_config()
        ancestor_only['children']['team-sandbox']['expected_mounts'] = [
            mount for mount in
            ancestor_only['children']['team-sandbox']['expected_mounts']
            if mount['destination'] != child['home']
        ]
        self.refusal(
            'refused', Administration, ancestor_only,
            self.children, self.lifecycle)

    def test_backup_home_sources_across_children_cannot_overlap(self):
        first_source = '/srv/firstmate/children/team-sandbox/home'
        cases = {
            'equal': first_source,
            'descendant': first_source + '/nested',
            'ancestor': '/srv/firstmate/children/team-sandbox',
        }
        for name, other_source in cases.items():
            with self.subTest(name=name):
                bad = admin_config()
                exact = next(
                    mount for mount in
                    bad['children']['toanmytran-bot']['expected_mounts']
                    if mount['destination'] == self.children['toanmytran-bot']['home']
                )
                exact['source'] = other_source
                self.refusal(
                    'refused', Administration, bad,
                    self.children, self.lifecycle)

    def test_operator_scope_configuration_requires_distinct_registered_child_list(self):
        cases = {
            'string instead of list': {
                'children': 'team-sandbox', 'allow_recovery': True,
            },
            'duplicate child': {
                'children': ['team-sandbox', 'team-sandbox'],
                'allow_recovery': True,
            },
            'unknown child': {
                'children': ['team-sandbox', 'unregistered-bot'],
                'allow_recovery': True,
            },
            'non-string child': {
                'children': ['team-sandbox', 7], 'allow_recovery': True,
            },
            'non-bool recovery authority': {
                'children': ['team-sandbox'], 'allow_recovery': 1,
            },
        }
        for name, operator in cases.items():
            with self.subTest(name=name):
                bad = copy.deepcopy(self.config)
                bad['operators']['captain-operator'] = operator
                self.refusal(
                    'refused', Application, bad, {},
                    lifecycle=FakeLifecycle(), administration=self.admin)

    def test_public_routes_enforce_parent_operator_and_child_scope(self):
        for principal in (PARENT, OPERATOR):
            for child_id in ('team-sandbox', 'toanmytran-bot'):
                result = self.app.handle(
                    'GET', f'/v1/children/{child_id}/admin/diagnostics', principal)
                self.assertEqual(result['child_id'], child_id)

        self.refusal(
            'forbidden', self.app.handle, 'GET',
            '/v1/children/team-sandbox/admin/diagnostics', FIN_CHILD)
        self.refusal(
            'forbidden', self.app.handle, 'GET',
            '/v1/children/team-sandbox/admin/diagnostics', TOAN_CHILD)
        self.refusal(
            'forbidden', self.app.handle, 'GET',
            '/v1/children/team-sandbox/admin/diagnostics', FOREIGN_PARENT)
        self.refusal(
            'forbidden', self.app.handle, 'GET',
            '/v1/children/third-bot/admin/diagnostics', PARENT)
        self.refusal(
            'forbidden', self.app.handle, 'GET',
            '/v1/children/third-bot/admin/diagnostics', THIRD_CHILD)
        self.refusal(
            'forbidden', self.app.handle, 'GET',
            '/v1/children/pgadmin-web/admin/diagnostics', PARENT)

    def test_lifecycle_is_unavailable_outside_administrative_allowlist(self):
        # A broader legacy operator scope must not bypass the stricter root-owned
        # administration allowlist when the administration surface is enabled.
        self.app.operators['captain-operator']['children'].append('third-bot')
        payload = {
            'operation_id': str(uuid.uuid4()),
            'action': 'start',
            'expected_generation': self.lifecycle.generation,
        }
        for principal in (PARENT, OPERATOR):
            with self.subTest(role=principal['role']):
                self.refusal(
                    'forbidden', self.app.handle, 'POST',
                    '/v1/children/third-bot/control', principal, payload)
        self.assertEqual(self.lifecycle.execute_calls, [])

    def test_scoped_operator_cannot_read_child_brain_but_child_and_parent_can(self):
        brain = {'fixture': 'parent-curated-brain'}
        with self.app.db:
            self.app.db.execute(
                'INSERT INTO brains VALUES (?,?,?,?)',
                ('team-sandbox', 'fixture-revision', json.dumps(brain), time.time()),
            )
        route = '/v1/children/team-sandbox/brain'
        self.assertEqual(self.app.handle('GET', route, PARENT), brain)
        self.assertEqual(self.app.handle('GET', route, FIN_CHILD), brain)
        refusal = self.refusal(
            'forbidden', self.app.handle, 'GET', route, OPERATOR)
        self.assertEqual(refusal.status, 403)

    def test_attestation_rejects_image_mount_drift_and_docker_socket(self):
        value = self.docker.inspect_values['secondmate']
        value['Image'] = 'sha256:' + 'f' * 64
        self.refusal(
            'identity_mismatch', self.admin.diagnostics,
            PARENT, 'team-sandbox', self.children['team-sandbox'])
        self.assertEqual([call[1] for call in self.docker.argv()], ['inspect'])

        self.docker = FakeDocker()
        self.admin.runner = self.docker
        self.docker.inspect_values['secondmate']['Mounts'].append(
            {'Source': '/var/run/docker.sock', 'Destination': '/var/run/docker.sock',
             'RW': True, 'Type': 'bind'})
        self.refusal(
            'identity_mismatch', self.admin.diagnostics,
            PARENT, 'team-sandbox', self.children['team-sandbox'])
        self.assertEqual([call[1] for call in self.docker.argv()], ['inspect'])

        for field, changed in (
                ('Source', '/srv/firstmate/children/other/home'),
                ('Type', 'volume'),
                # A socket hidden at an otherwise expected destination must
                # fail source attestation, not merely destination screening.
                ('Source', '/var/run/docker.sock')):
            with self.subTest(field=field, changed=changed):
                self.docker = FakeDocker()
                self.admin.runner = self.docker
                self.docker.inspect_values['secondmate']['Mounts'][0][field] = changed
                self.refusal(
                    'identity_mismatch', self.admin.diagnostics,
                    PARENT, 'team-sandbox', self.children['team-sandbox'])
                self.assertEqual([call[1] for call in self.docker.argv()], ['inspect'])

    def test_attestation_rejects_capability_and_host_network_drift(self):
        for field, changed in (
                ('CapDrop', []), ('NetworkMode', 'host'), ('AutoRemove', True),
                ('RestartPolicy', {'Name': 'no'})):
            with self.subTest(field=field):
                docker = FakeDocker()
                self.admin.runner = docker
                docker.inspect_values['secondmate']['HostConfig'][field] = changed
                self.refusal(
                    'identity_mismatch', self.admin.diagnostics,
                    PARENT, 'team-sandbox', self.children['team-sandbox'])
                self.assertEqual(
                    docker.argv(),
                    [[DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate']],
                )

    def test_attestation_rejects_shared_or_slave_bind_propagation(self):
        for propagation in ('rshared', 'rslave'):
            with self.subTest(propagation=propagation):
                docker = FakeDocker()
                self.admin.runner = docker
                docker.inspect_values['secondmate']['Mounts'][1][
                    'Propagation'] = propagation
                self.refusal(
                    'identity_mismatch', self.admin.diagnostics,
                    PARENT, 'team-sandbox', self.children['team-sandbox'])
                self.assertEqual(
                    docker.argv(),
                    [[DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate']],
                )

    def test_attestation_rejects_environment_secrets_duplicates_and_extra_networks(self):
        mutations = (
            ('authority environment drift', lambda value: value['Config']['Env'].__setitem__(
                2, 'SM_CAPTAIN_ID=987654321')),
            ('inline token', lambda value: value['Config']['Env'].append(
                'OPENAI_API_KEY=sk-inline-secret-must-never-exist')),
            ('duplicate environment key', lambda value: value['Config']['Env'].append(
                'SM_CAPTAIN_ID=123456789')),
            ('extra network', lambda value: value['NetworkSettings']['Networks'].__setitem__(
                'unapproved-net', {'NetworkID': 'e' * 64})),
            ('unbounded log rotation', lambda value: value['HostConfig']['LogConfig'][
                'Config'].__setitem__('max-size', '100m')),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                docker = FakeDocker()
                self.admin.runner = docker
                mutate(docker.inspect_values['secondmate'])
                self.refusal(
                    'identity_mismatch', self.admin.diagnostics,
                    PARENT, 'team-sandbox', self.children['team-sandbox'])
                self.assertEqual(
                    docker.argv(),
                    [[DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate']],
                )

    def test_dormant_child_resolves_empty_network_id_with_exact_pinned_lookup(self):
        network = 'fleet-team-sandbox'
        inspect_argv = [DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate']
        network_argv = [
            DOCKER, 'network', 'inspect', '--format', '{{.Id}}', network,
        ]

        docker = FakeDocker()
        docker.inspect_values['secondmate']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'created', 'StartedAt': '',
        })
        docker.inspect_values['secondmate']['NetworkSettings']['Networks'][network][
            'NetworkID'] = ''
        self.admin.runner = docker
        result = self.admin.diagnostics(
            PARENT, 'team-sandbox', self.children['team-sandbox'])
        self.assertFalse(result['container']['running'])
        self.assertEqual(docker.argv(), [inspect_argv, network_argv])

        docker = FakeDocker()
        docker.inspect_values['secondmate']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'created', 'StartedAt': '',
        })
        docker.inspect_values['secondmate']['NetworkSettings']['Networks'][network][
            'NetworkID'] = ''
        docker.network_ids[network] = 'e' * 64
        self.admin.runner = docker
        self.refusal(
            'identity_mismatch', self.admin.diagnostics,
            PARENT, 'team-sandbox', self.children['team-sandbox'])
        self.assertEqual(docker.argv(), [inspect_argv, network_argv])

    def test_logs_are_bounded_use_inspected_id_and_redact_secrets(self):
        telegram = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd1234'
        api = 'Bearer abcdefghijklmnopqrstuvwxyz_123456'
        jwt = '.'.join(('a' * 22, 'b' * 22, 'c' * 12))
        sensitive = f'\x01{CONFIG_SECRET} {telegram} {api} {jwt}\n'.encode()
        self.docker.log_output = b'x' * self.admin.max_log_bytes + sensitive

        result = self.app.handle(
            'GET',
            '/v1/children/team-sandbox/admin/logs?tail=7&since_seconds=9',
            PARENT,
        )
        self.assertTrue(result['truncated'])
        self.assertNotIn(CONFIG_SECRET, result['content'])
        self.assertNotIn(telegram, result['content'])
        self.assertNotIn('Bearer ', result['content'])
        self.assertNotIn(jwt, result['content'])
        self.assertNotIn('\x01', result['content'])
        self.assertGreaterEqual(result['content'].count('[REDACTED]'), 4)
        self.assertEqual(
            self.docker.argv()[-1],
            [DOCKER, 'logs', '--tail', '7', '--since', '9s', '--timestamps', FIN_ID],
        )

        before = len(self.docker.calls)
        self.refusal(
            'refused', self.app.handle, 'GET',
            '/v1/children/team-sandbox/admin/logs?tail=501', PARENT)
        self.refusal(
            'refused', self.app.handle, 'GET',
            '/v1/children/team-sandbox/admin/logs?follow=true', PARENT)
        self.assertEqual(len(self.docker.calls), before)

    def test_logs_redact_before_truncation_and_cap_encoded_bytes(self):
        # With the unsafe order (raw byte truncation, then redaction), this
        # cutoff begins five bytes into CONFIG_SECRET and leaks its suffix.
        # Invalid bytes also expand to three-byte U+FFFD during decoding, so
        # this simultaneously verifies the final response is byte bounded.
        suffix_bytes = self.admin.max_log_bytes + 5 - len(CONFIG_SECRET)
        self.assertGreater(suffix_bytes, 0)
        self.docker.log_output = (
            b'x' * 50 + CONFIG_SECRET.encode() + b'\xff' * suffix_bytes)

        result = self.app.handle(
            'GET', '/v1/children/team-sandbox/admin/logs?tail=500', PARENT)
        self.assertTrue(result['truncated'])
        self.assertNotIn(CONFIG_SECRET, result['content'])
        self.assertNotIn(CONFIG_SECRET[5:], result['content'])
        self.assertLessEqual(
            len(result['content'].encode('utf-8')), self.admin.max_log_bytes)
        self.assertEqual(
            set(self.docker.log_processes[-1].stdout.read_sizes), {65_536})

    def test_logs_streaming_ring_drops_partial_line_and_normalizes_controls(self):
        retain = self.admin.max_log_bytes + 8192
        self.docker.log_output = (
            b'x' * (retain + 100) + CONFIG_SECRET.encode()
            + b' remains-in-cut-line\ncomplete\x01safe-line\n')

        result = self.app.handle(
            'GET', '/v1/children/team-sandbox/admin/logs?tail=500', PARENT)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['content'], 'complete safe-line\n')
        self.assertNotIn(CONFIG_SECRET, result['content'])
        self.assertNotIn('remains-in-cut-line', result['content'])
        self.assertLessEqual(
            len(result['content'].encode('utf-8')), self.admin.max_log_bytes)
        self.assertFalse(self.docker.log_processes[-1].killed)

    def test_start_allowed_gate_blocks_dormant_bot_before_lifecycle(self):
        payload = {
            'operation_id': str(uuid.uuid4()),
            'action': 'start',
            'expected_generation': self.lifecycle.generation,
        }
        self.refusal(
            'activation_required', self.app.handle, 'POST',
            '/v1/children/toanmytran-bot/control', PARENT, payload)
        self.assertEqual(self.lifecycle.execute_calls, [])
        self.assertEqual(
            self.docker.argv(),
            [[DOCKER, 'inspect', '--format', '{{json .}}', 'toanmytran-bot']],
        )

    def test_diagnostics_uses_fixed_inspect_and_stats_argv(self):
        result = self.admin.diagnostics(
            PARENT, 'team-sandbox', self.children['team-sandbox'])
        self.assertEqual(result['resources']['active_profile'], 'normal')
        self.assertFalse(result['isolation']['docker_socket'])
        self.assertEqual(
            self.docker.argv(),
            [
                [DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate'],
                [DOCKER, 'stats', '--no-stream', '--format', '{{json .}}', FIN_ID],
            ],
        )

    def test_runbooks_have_exact_non_shell_argv(self):
        child = self.children['team-sandbox']
        processes = self.admin.runbook(PARENT, 'team-sandbox', child, 'processes')
        self.assertIn('python3', processes['result'])
        self.assertEqual(
            self.docker.argv()[-1],
            [DOCKER, 'top', FIN_ID, '-eo', 'pid,user,comm'],
        )

        self.docker.calls.clear()
        usage = self.admin.runbook(PARENT, 'team-sandbox', child, 'home-usage')
        self.assertEqual([row['path'] for row in usage['result']], ['config', 'data', 'state'])
        self.assertEqual(
            self.docker.argv()[-1],
            [
                DOCKER, 'exec', '-i', '--user', '1000:1000',
                '--env', 'FM_HOME=/home/nguye/team-sandbox', FIN_ID,
                '/usr/bin/du', '-sk', '--one-file-system',
                '/home/nguye/team-sandbox/config',
                '/home/nguye/team-sandbox/data',
                '/home/nguye/team-sandbox/state',
            ],
        )

        self.docker.calls.clear()
        health = self.admin.runbook(PARENT, 'team-sandbox', child, 'runtime-health')
        self.assertEqual(health['result']['generation'], self.lifecycle.generation)
        self.assertEqual(
            self.docker.argv(),
            [[DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate']],
        )
        self.refusal(
            'forbidden', self.admin.runbook,
            PARENT, 'team-sandbox', child, 'shell')

    def operation(self, operation_id=None, *, profile='large', generation=None):
        return {
            'operation_id': operation_id or str(uuid.uuid4()),
            'action': 'resource-profile',
            'expected_generation': generation or self.lifecycle.generation,
            'parameters': {'profile': profile},
        }

    def backup_operation(self, operation_id=None, generation=None):
        return {
            'operation_id': operation_id or str(uuid.uuid4()),
            'action': 'backup',
            'expected_generation': generation or self.lifecycle.generation,
            'parameters': {},
        }

    def backup_admin(self, name, docker=None, lifecycle=None):
        docker = docker or FakeDocker()
        lifecycle = lifecycle or FakeLifecycle()
        admin = Administration(
            copy.deepcopy(admin_config()), self.children, lifecycle,
            docker=DOCKER, runner=docker, popen=docker.popen,
            disk_usage=lambda _path: SimpleNamespace(
                total=100 * GIB, used=10 * GIB, free=90 * GIB),
        )
        admin.backup_root = self.root / name
        self.app.administration = admin
        return admin, docker, lifecycle

    def test_resource_profile_is_idempotent_conflict_safe_and_uses_exact_id(self):
        operation_id = str(uuid.uuid4())
        payload = self.operation(operation_id)
        first = self.app.handle(
            'POST', '/v1/children/team-sandbox/admin/operations', PARENT, payload)
        self.assertEqual(first['state'], 'complete')
        self.assertEqual(first['result']['profile'], 'large')
        update = [argv for argv in self.docker.argv() if argv[1] == 'update']
        self.assertEqual(
            update,
            [[DOCKER, 'update', '--memory', str(2 * GIB), '--cpus', '2',
              '--pids-limit', '512', FIN_ID]],
        )
        self.assertNotIn('secondmate', update[0])

        call_count = len(self.docker.calls)
        duplicate = self.app.handle(
            'POST', '/v1/children/team-sandbox/admin/operations', PARENT, payload)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(len(self.docker.calls), call_count)

        conflict = self.operation(operation_id, profile='normal')
        self.refusal(
            'conflict', self.app.handle, 'POST',
            '/v1/children/team-sandbox/admin/operations', PARENT, conflict)
        self.assertEqual(len(self.docker.calls), call_count)

    def test_resource_profile_rejects_stale_and_active_work_before_update(self):
        stale = self.operation(generation='runtime:stale')
        self.refusal(
            'stale_generation', self.app.handle, 'POST',
            '/v1/children/team-sandbox/admin/operations', PARENT, stale)
        self.assertFalse(any(argv[1] == 'update' for argv in self.docker.argv()))

        self.docker.calls.clear()
        self.lifecycle.safe = False
        active = self.operation()
        self.refusal(
            'active_work', self.app.handle, 'POST',
            '/v1/children/team-sandbox/admin/operations', PARENT, active)
        self.assertFalse(any(argv[1] == 'update' for argv in self.docker.argv()))
        self.assertEqual(self.lifecycle.runtime_calls, [])

        self.lifecycle.safe = True
        outside = self.operation(profile='unlimited')
        self.refusal(
            'forbidden', self.app.handle, 'POST',
            '/v1/children/team-sandbox/admin/operations', PARENT, outside)
        self.assertFalse(any(argv[1] == 'update' for argv in self.docker.argv()))

    def test_post_quiesce_lease_change_resumes_before_refusal(self):
        self.lifecycle.change_after_quiesce = True
        operation_id = str(uuid.uuid4())
        self.refusal(
            'lease_changed', self.app.handle, 'POST',
            '/v1/children/team-sandbox/admin/operations', PARENT,
            self.operation(operation_id))
        self.assertEqual(
            self.lifecycle.runtime_calls,
            [
                ('secondmate', ('control-check', '--quiesce',
                                '--expected-generation', self.lifecycle.generation)),
                ('secondmate', ('control-check',)),
                ('secondmate', ('control-resume', '--lease-id', 'lease-1')),
            ],
        )
        status = self.app.handle(
            'GET', f'/v1/children/team-sandbox/admin/operations/{operation_id}',
            PARENT)
        self.assertEqual(status['state'], 'refused')
        self.assertEqual(status['result'], {'error': 'lease_changed'})

    def test_resume_failure_marks_operation_unknown(self):
        self.lifecycle.resume_ok = False
        operation_id = str(uuid.uuid4())
        self.refusal(
            'admin_unknown', self.app.handle, 'POST',
            '/v1/children/team-sandbox/admin/operations', PARENT,
            self.operation(operation_id))
        status = self.app.handle(
            'GET', f'/v1/children/team-sandbox/admin/operations/{operation_id}',
            PARENT)
        self.assertEqual(status['state'], 'unknown')
        self.assertEqual(status['result'], {'error': 'admin_unknown'})
        self.assertEqual(
            self.lifecycle.runtime_calls[-1],
            ('secondmate', ('control-resume', '--lease-id', 'lease-1')),
        )

    def test_backup_stops_runs_attested_home_only_helper_and_restarts(self):
        admin, docker, lifecycle = self.backup_admin('backup-success')
        operation_id = str(uuid.uuid4())
        helper_name = 'fm-backup-team-sandbox-' + operation_id
        helper_id = f'{10:064x}'
        cidfile = admin.backup_root / 'team-sandbox' / ('.helper.' + operation_id + '.cid')
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': lifecycle.generation, 'parameters': {},
        }
        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            result = self.app.handle(
                'POST', '/v1/children/team-sandbox/admin/operations', PARENT, payload)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['result']['container_id'], FIN_ID)
        self.assertEqual(
            docker.argv(),
            [
                [DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate'],
                [DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate'],
                [DOCKER, 'exec', '-i', '--user', '1000:1000',
                 '--env', 'FM_HOME=/home/nguye/team-sandbox', FIN_ID,
                 '/usr/bin/du', '-sb', '--one-file-system',
                 '/home/nguye/team-sandbox/config',
                 '/home/nguye/team-sandbox/data',
                 '/home/nguye/team-sandbox/state'],
                [DOCKER, 'stop', '--time', '20', FIN_ID],
                [DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate'],
                [DOCKER, 'run', '--name', helper_name, '--cidfile', str(cidfile),
                 '--log-driver', 'none', '--network', 'none', '--read-only',
                 '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                 '--pids-limit', '64', '--memory', '268435456', '--cpus', '0.5',
                 '--user', '1000:1000', '--entrypoint', '/usr/bin/tar',
                 '--mount',
                 'type=bind,source=/srv/firstmate/children/team-sandbox/home,'
                 'target=/home/nguye/team-sandbox,readonly,bind-propagation=rprivate',
                 '--label', 'io.maycha.fleet.operation=state-backup',
                 '--label', 'io.maycha.fleet.child-id=team-sandbox',
                 '--label', 'io.maycha.fleet.operation-id=' + operation_id,
                 FIN_IMAGE,
                 '--format=pax', '--numeric-owner', '--one-file-system',
                 '--exclude=.codex', '--exclude=auth.json', '--exclude=*token*',
                 '--exclude=*secret*', '--exclude=*.pem', '--exclude=*.key',
                 '-C', '/home/nguye/team-sandbox', '-cf', '-', '--',
                 'config', 'data', 'state'],
                [DOCKER, 'ps', '-a', '--no-trunc', '--filter',
                 'name=^/' + helper_name + '$', '--filter',
                 'label=io.maycha.fleet.operation=state-backup', '--filter',
                 'label=io.maycha.fleet.child-id=team-sandbox', '--filter',
                 'label=io.maycha.fleet.operation-id=' + operation_id,
                 '--format', '{{.ID}}'],
                [DOCKER, 'inspect', '--format', '{{json .}}', helper_id],
                [DOCKER, 'rm', '-f', helper_id],
                [DOCKER, 'start', FIN_ID],
                [DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate'],
                [DOCKER, 'inspect', '--format', '{{json .}}', 'secondmate'],
            ],
        )
        self.assertEqual(
            lifecycle.runtime_calls[-1],
            ('secondmate', ('control-resume', '--lease-id', 'lease-1')),
        )
        self.assertTrue(docker.inspect_values['secondmate']['State']['Running'])
        self.assertEqual(docker.removed_helpers, [helper_id])
        helper = docker.helper_values[helper_id]
        self.assertEqual(helper['Mounts'], [{
            'Source': '/srv/firstmate/children/team-sandbox/home',
            'Destination': '/home/nguye/team-sandbox',
            'RW': False, 'Type': 'bind', 'Propagation': 'rprivate',
        }])
        helper_sources = {mount['Source'] for mount in helper['Mounts']}
        self.assertNotIn(
            '/srv/firstmate/children/team-sandbox/parent-home', helper_sources)
        self.assertNotIn(
            '/srv/firstmate/children/team-sandbox/runtime', helper_sources)
        self.assertNotIn(
            '/srv/firstmate/secrets/team-sandbox-token', helper_sources)
        self.assertEqual(helper['HostConfig']['VolumesFrom'], [])
        self.assertEqual(helper['Config']['User'], '1000:1000')
        self.assertEqual(helper['Config']['Entrypoint'], ['/usr/bin/tar'])
        self.assertEqual(helper['HostConfig']['PidsLimit'], 64)
        self.assertEqual(helper['HostConfig']['Memory'], 268435456)
        self.assertEqual(helper['HostConfig']['NanoCpus'], 500000000)
        self.assertEqual(
            (admin.backup_root / 'team-sandbox' / (operation_id + '.tar')).read_bytes(),
            docker.tar_output,
        )
        self.assertFalse(any(
            path.name.startswith('.partial.')
            for path in (admin.backup_root / 'team-sandbox').iterdir()))

    def test_backup_transitions_stop_completed_only_after_verified_stop(self):
        admin, docker, lifecycle = self.backup_admin('backup-stop-transition')
        operation_id = str(uuid.uuid4())
        child_root = admin.backup_root / 'team-sandbox'
        recovery_record = child_root / ('.recovery.' + operation_id + '.json')
        stop_returned = False
        stopped_inspected = False
        observations = []
        transitions = []
        original_replace = os.replace
        original_fsync_directory = admin._fsync_directory

        def observing_runner(argv, **kwargs):
            nonlocal stop_returned, stopped_inspected
            if argv[1] == 'stop':
                marker = json.loads(recovery_record.read_text())
                observations.append(('at-stop', marker['stop_completed']))
            result = docker(argv, **kwargs)
            if argv[1] == 'stop':
                stop_returned = True
            elif (argv[1] == 'inspect' and stop_returned
                  and argv[-1] == 'secondmate' and not stopped_inspected):
                self.assertFalse(json.loads(result.stdout)['State']['Running'])
                stopped_inspected = True
            return result

        def observing_popen(argv, **kwargs):
            marker = json.loads(recovery_record.read_text())
            observations.append(('at-helper', marker['stop_completed']))
            return docker.popen(argv, **kwargs)

        def observing_replace(source, destination):
            if Path(destination) == recovery_record:
                value = json.loads(Path(source).read_text())
                if value['stop_completed'] and not value['restored']:
                    self.assertTrue(stopped_inspected)
                transitions.append({
                    'stop_completed': value['stop_completed'],
                    'restored': value['restored'],
                    'synced': False,
                })
            return original_replace(source, destination)

        def observing_fsync_directory(path):
            result = original_fsync_directory(path)
            if Path(path) == child_root and transitions:
                transitions[-1]['synced'] = True
            return result

        admin.runner = observing_runner
        admin.popen = observing_popen
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': lifecycle.generation, 'parameters': {},
        }
        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat),
              patch('administration.os.replace', side_effect=observing_replace),
              patch.object(admin, '_fsync_directory', side_effect=observing_fsync_directory)):
            result = self.app.handle(
                'POST', '/v1/children/team-sandbox/admin/operations', PARENT, payload)

        self.assertEqual(result['state'], 'complete')
        self.assertEqual(observations, [('at-stop', False), ('at-helper', True)])
        self.assertEqual(
            transitions,
            [
                {'stop_completed': True, 'restored': False, 'synced': True},
                {'stop_completed': True, 'restored': True, 'synced': True},
            ],
        )

    def test_backup_helper_timeout_and_failure_cleanup_then_restart(self):
        cases = ('timeout', 'failure')
        for failure in cases:
            with self.subTest(failure=failure):
                docker = FakeDocker()
                docker.helper_timeout = failure == 'timeout'
                docker.helper_returncode = 17 if failure == 'failure' else 0
                lifecycle = FakeLifecycle()
                admin, docker, lifecycle = self.backup_admin(
                    'backup-failure-' + failure, docker, lifecycle)
                operation_id = str(uuid.uuid4())
                payload = {
                    'operation_id': operation_id, 'action': 'backup',
                    'expected_generation': lifecycle.generation, 'parameters': {},
                }
                with (patch('administration._protected_directory', side_effect=lambda path: path),
                      patch.object(Path, 'lstat', secure_test_lstat)):
                    self.refusal(
                        'admin_unknown', self.app.handle, 'POST',
                        '/v1/children/team-sandbox/admin/operations', PARENT, payload)
                status = self.app.handle(
                    'GET',
                    f'/v1/children/team-sandbox/admin/operations/{operation_id}',
                    PARENT)
                self.assertEqual(status['state'], 'unknown')
                self.assertEqual(status['result'], {'error': 'admin_unknown'})
                self.assertEqual(len(docker.helper_processes), 1)
                self.assertTrue(any(
                    argv[1:3] == ['rm', '-f'] for argv in docker.argv()))
                self.assertTrue(any(
                    argv == [DOCKER, 'start', FIN_ID] for argv in docker.argv()))
                self.assertTrue(docker.inspect_values['secondmate']['State']['Running'])
                self.assertEqual(
                    lifecycle.runtime_calls[-1],
                    ('secondmate', ('control-resume', '--lease-id', 'lease-1')),
                )
                partials = list((admin.backup_root / 'team-sandbox').glob('.partial.*'))
                self.assertEqual(partials, [])

    def test_backup_completes_after_bridge_drops_old_lease_and_changes_generation(self):
        admin, docker, lifecycle = self.backup_admin('backup-new-runtime-generation')

        def launch_new_bridge(_container):
            lifecycle.lease_id = None
            lifecycle.generation = 'runtime:g2'

        docker.start_hook = launch_new_bridge
        operation_id = str(uuid.uuid4())
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': 'runtime:g1', 'parameters': {},
        }
        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            result = self.app.handle(
                'POST', '/v1/children/team-sandbox/admin/operations', PARENT, payload)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(lifecycle.generation, 'runtime:g2')
        self.assertEqual(lifecycle.status_calls, ['secondmate', 'secondmate'])
        self.assertFalse(any(
            arguments[0] == 'control-resume'
            for _container, arguments in lifecycle.runtime_calls))
        self.assertTrue(docker.inspect_values['secondmate']['State']['Running'])
        self.assertFalse(
            (admin.backup_root / 'team-sandbox' /
             ('.recovery.' + operation_id + '.json')).exists())

    def test_backup_refuses_surviving_foreign_lease_as_unknown(self):
        admin, docker, lifecycle = self.backup_admin('backup-foreign-lease')

        def launch_with_foreign_lease(_container):
            lifecycle.lease_id = 'foreign-lease'
            lifecycle.generation = 'runtime:g2'

        docker.start_hook = launch_with_foreign_lease
        operation_id = str(uuid.uuid4())
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': 'runtime:g1', 'parameters': {},
        }
        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat),
              patch('administration.time.sleep')):
            self.refusal(
                'admin_unknown', self.app.handle, 'POST',
                '/v1/children/team-sandbox/admin/operations', PARENT, payload)
        status = self.app.handle(
            'GET', f'/v1/children/team-sandbox/admin/operations/{operation_id}', PARENT)
        self.assertEqual(status['state'], 'unknown')
        self.assertEqual(status['result'], {'error': 'admin_unknown'})
        self.assertEqual(lifecycle.status_calls.count('secondmate'), 31)
        self.assertFalse(any(
            arguments[0] == 'control-resume'
            for _container, arguments in lifecycle.runtime_calls))
        self.assertTrue(docker.inspect_values['secondmate']['State']['Running'])
        self.assertTrue(
            (admin.backup_root / 'team-sandbox' /
             ('.recovery.' + operation_id + '.json')).exists())

    def test_backup_restart_race_keeps_unrestored_marker_when_runtime_recheck_is_stopped(self):
        admin, docker, lifecycle = self.backup_admin('backup-restart-race')
        operation_id = str(uuid.uuid4())
        started = False
        post_start_inspects = 0

        def observe_start(_container):
            nonlocal started
            started = True

        def stop_on_runtime_recheck(target, value):
            nonlocal post_start_inspects
            if started and target == 'secondmate':
                post_start_inspects += 1
                if post_start_inspects == 2:
                    value['State'].update({
                        'Running': False, 'Paused': False,
                        'Status': 'exited', 'StartedAt': '',
                    })

        docker.start_hook = observe_start
        docker.inspect_hook = stop_on_runtime_recheck
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': lifecycle.generation, 'parameters': {},
        }
        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat),
              patch('administration.time.sleep')):
            self.refusal(
                'admin_unknown', self.app.handle, 'POST',
                '/v1/children/team-sandbox/admin/operations', PARENT, payload)

        status = self.app.handle(
            'GET', f'/v1/children/team-sandbox/admin/operations/{operation_id}', PARENT)
        recovery_record = (
            admin.backup_root / 'team-sandbox' /
            ('.recovery.' + operation_id + '.json'))
        self.assertEqual(status['state'], 'unknown')
        self.assertEqual(post_start_inspects, 2)
        self.assertFalse(docker.inspect_values['secondmate']['State']['Running'])
        self.assertTrue(recovery_record.exists())
        marker = json.loads(recovery_record.read_text())
        self.assertTrue(marker['stop_completed'])
        self.assertFalse(marker['restored'])
        self.assertFalse(any(
            arguments[0] == 'control-resume'
            for _container, arguments in lifecycle.runtime_calls))

    def test_backup_helper_attestation_rejects_config_host_and_mount_drift(self):
        mutations = (
            ('command', lambda helper: helper['Config']['Cmd'].append('secrets')),
            ('volumes-from', lambda helper: helper['HostConfig'].__setitem__(
                'VolumesFrom', [FIN_ID])),
            ('resource envelope', lambda helper: helper['HostConfig'].__setitem__(
                'PidsLimit', 65)),
            ('security options', lambda helper: helper['HostConfig']['SecurityOpt'].append(
                'seccomp=unconfined')),
            ('mount propagation', lambda helper: helper['Mounts'][0].__setitem__(
                'Propagation', 'rshared')),
            ('extra mount', lambda helper: helper['Mounts'].append({
                'Source': '/run/secrets', 'Destination': '/run/secrets',
                'RW': False, 'Type': 'bind', 'Propagation': 'rprivate',
            })),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                docker = FakeDocker()
                docker.helper_mutator = mutate
                lifecycle = FakeLifecycle()
                admin, docker, lifecycle = self.backup_admin(
                    'backup-helper-drift-' + label.replace(' ', '-'), docker, lifecycle)
                operation_id = str(uuid.uuid4())
                payload = {
                    'operation_id': operation_id, 'action': 'backup',
                    'expected_generation': lifecycle.generation, 'parameters': {},
                }
                with (patch('administration._protected_directory', side_effect=lambda path: path),
                      patch.object(Path, 'lstat', secure_test_lstat)):
                    self.refusal(
                        'admin_unknown', self.app.handle, 'POST',
                        '/v1/children/team-sandbox/admin/operations', PARENT, payload)
                status = self.app.handle(
                    'GET',
                    f'/v1/children/team-sandbox/admin/operations/{operation_id}',
                    PARENT)
                self.assertEqual(status['state'], 'unknown')
                self.assertEqual(docker.removed_helpers, [])
                self.assertTrue(any(
                    argv == [DOCKER, 'start', FIN_ID] for argv in docker.argv()))
                self.assertTrue(docker.inspect_values['secondmate']['State']['Running'])

    def test_post_stop_size_refusal_is_unknown_and_restarts_child(self):
        admin, docker, lifecycle = self.backup_admin('backup-post-side-effect')
        admin.manifests['team-sandbox']['max_backup_bytes'] = 1_048_576
        docker.tar_output = b'x' * (1_048_576 + 1)
        operation_id = str(uuid.uuid4())
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': lifecycle.generation, 'parameters': {},
        }
        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            self.refusal(
                'admin_unknown', self.app.handle, 'POST',
                '/v1/children/team-sandbox/admin/operations', PARENT, payload)
        status = self.app.handle(
            'GET', f'/v1/children/team-sandbox/admin/operations/{operation_id}', PARENT)
        self.assertEqual(status['state'], 'unknown')
        self.assertEqual(status['result'], {'error': 'admin_unknown'})
        self.assertTrue(any(argv == [DOCKER, 'start', FIN_ID] for argv in docker.argv()))
        self.assertTrue(docker.inspect_values['secondmate']['State']['Running'])
        self.assertEqual(
            lifecycle.runtime_calls[-1],
            ('secondmate', ('control-resume', '--lease-id', 'lease-1')),
        )

    def test_backups_for_two_children_are_globally_serialized(self):
        docker = FakeDocker()
        docker.block_first_helper = True
        lifecycle = FakeLifecycle()
        _admin, docker, lifecycle = self.backup_admin(
            'backup-global-serialization', docker, lifecycle)
        results, errors = {}, []

        def execute(child_id):
            try:
                results[child_id] = self.app.handle(
                    'POST', f'/v1/children/{child_id}/admin/operations', PARENT,
                    {
                        'operation_id': str(uuid.uuid4()), 'action': 'backup',
                        'expected_generation': lifecycle.generation, 'parameters': {},
                    })
            except Exception as error:  # captured for an assertion in the main test thread
                errors.append(error)

        first = threading.Thread(target=execute, args=('team-sandbox',))
        second = threading.Thread(target=execute, args=('toanmytran-bot',))
        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            first.start()
            self.assertTrue(docker.first_helper_started.wait(2))
            second.start()
            time.sleep(0.15)
            self.assertTrue(second.is_alive())
            self.assertFalse(any(
                TOAN_ID in argv or 'toanmytran-bot' in argv for argv in docker.argv()))
            docker.release_first_helper.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {'team-sandbox', 'toanmytran-bot'})
        self.assertTrue(all(value['state'] == 'complete' for value in results.values()))
        self.assertEqual(docker.helper_launches, 2)

    def test_service_recovery_marks_interrupted_admin_operation_unknown(self):
        operation_id = str(uuid.uuid4())
        self.lifecycle.lease_id = 'lease-1'
        with self.app.db:
            self.app.db.execute(
                'INSERT INTO admin_operations VALUES (?,?,?,?,?,NULL)',
                (operation_id, 'team-sandbox', 'digest', json.dumps({
                    'operation_id': operation_id,
                    'action': 'resource-profile',
                    'expected_generation': self.lifecycle.generation,
                    'parameters': {'profile': 'large'},
                    'principal': PARENT,
                    'child_id': 'team-sandbox',
                }), 'executing'),
            )
        self.app.db.close()
        self.app = None

        recovered = Application(
            self.config, {}, lifecycle=self.lifecycle,
            administration=self.admin, recover_interrupted=True)
        try:
            result = recovered.handle(
                'GET',
                f'/v1/children/team-sandbox/admin/operations/{operation_id}',
                PARENT,
            )
            self.assertEqual(result, {
                'operation_id': operation_id, 'state': 'unknown', 'result': None,
            })
            self.assertEqual(self.lifecycle.lease_id, 'lease-1')
            self.assertFalse(any(
                arguments and arguments[0] == 'control-resume'
                for _container, arguments in self.lifecycle.runtime_calls))
            self.assertFalse(any(
                argv == [DOCKER, 'start', FIN_ID] for argv in self.docker.argv()))
        finally:
            recovered.db.close()

    def test_service_recovery_does_not_start_stopped_resource_operation(self):
        operation_id = str(uuid.uuid4())
        self.docker.inspect_values['secondmate']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'exited', 'StartedAt': '',
        })
        with self.app.db:
            self.app.db.execute(
                'INSERT INTO admin_operations VALUES (?,?,?,?,?,NULL)',
                (operation_id, 'team-sandbox', 'digest', json.dumps({
                    'operation_id': operation_id,
                    'action': 'resource-profile',
                    'expected_generation': self.lifecycle.generation,
                    'parameters': {'profile': 'large'},
                    'principal': PARENT,
                    'child_id': 'team-sandbox',
                }), 'executing'),
            )
        self.app.db.close()
        self.app = None

        recovered = Application(
            self.config, {}, lifecycle=self.lifecycle,
            administration=self.admin, recover_interrupted=True)
        try:
            status = recovered.handle(
                'GET',
                f'/v1/children/team-sandbox/admin/operations/{operation_id}',
                PARENT,
            )
            self.assertEqual(status['state'], 'unknown')
            self.assertFalse(self.docker.inspect_values['secondmate']['State']['Running'])
            self.assertFalse(any(
                argv == [DOCKER, 'start', FIN_ID] for argv in self.docker.argv()))
        finally:
            recovered.db.close()

    def test_interrupted_backup_recovery_removes_orphan_restarts_and_resumes(self):
        docker = FakeDocker()
        lifecycle = FakeLifecycle()
        lifecycle.lease_id = 'lease-1'
        admin, docker, lifecycle = self.backup_admin(
            'backup-recovery', docker, lifecycle)
        operation_id = str(uuid.uuid4())
        helper_id = 'e' * 64
        helper_name = 'fm-backup-team-sandbox-' + operation_id
        command = admin._backup_command(
            self.children['team-sandbox'], admin.manifests['team-sandbox'])
        docker.helper_values[helper_id] = {
            'Id': helper_id, 'Name': '/' + helper_name, 'Image': FIN_IMAGE,
            'Config': {
                'User': '1000:1000', 'Entrypoint': ['/usr/bin/tar'],
                'Cmd': command,
                'Labels': {
                    'io.maycha.fleet.operation': 'state-backup',
                    'io.maycha.fleet.child-id': 'team-sandbox',
                    'io.maycha.fleet.operation-id': operation_id,
                },
            },
            'HostConfig': {
                'NetworkMode': 'none', 'Privileged': False,
                'ReadonlyRootfs': True, 'CapDrop': ['ALL'], 'CapAdd': None,
                'SecurityOpt': ['no-new-privileges:true'],
                'LogConfig': {'Type': 'none'}, 'PidsLimit': 64,
                'Memory': 268435456, 'NanoCpus': 500000000,
                'AutoRemove': False, 'VolumesFrom': [], 'PortBindings': {},
                'Devices': [], 'DeviceRequests': [],
            },
            'Mounts': [{
                'Source': '/srv/firstmate/children/team-sandbox/home',
                'Destination': '/home/nguye/team-sandbox',
                'RW': False, 'Type': 'bind', 'Propagation': 'rprivate',
            }],
        }
        docker.inspect_values['secondmate']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'exited', 'StartedAt': '',
        })
        child_root = admin.backup_root / 'team-sandbox'
        child_root.mkdir(parents=True)
        (child_root / ('.partial.' + operation_id + '.tar')).write_bytes(b'partial')
        (child_root / ('.partial.' + operation_id + '.json')).write_text('{}')
        recovery_record = child_root / ('.recovery.' + operation_id + '.json')
        recovery_record.write_text(json.dumps(recovery_marker(
            'team-sandbox', operation_id, FIN_ID, FIN_IMAGE,
            generation=lifecycle.generation, stop_completed=True,
        )))
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': lifecycle.generation, 'parameters': {},
            'principal': PARENT, 'child_id': 'team-sandbox',
        }
        with self.app.db:
            self.app.db.execute(
                'INSERT INTO admin_operations VALUES (?,?,?,?,?,NULL)',
                (operation_id, 'team-sandbox', 'digest', json.dumps(payload), 'executing'),
            )
        self.app.db.close()
        self.app = None

        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            recovered = Application(
                self.config, {}, lifecycle=lifecycle,
                administration=admin, recover_interrupted=True)
        try:
            status = recovered.handle(
                'GET',
                f'/v1/children/team-sandbox/admin/operations/{operation_id}',
                PARENT)
            self.assertEqual(status['state'], 'unknown')
            self.assertEqual(docker.removed_helpers, [helper_id])
            self.assertTrue(docker.inspect_values['secondmate']['State']['Running'])
            self.assertFalse(any(child_root.glob('.partial.*')))
            self.assertFalse(recovery_record.exists())
            self.assertEqual(
                lifecycle.runtime_calls[-1],
                ('secondmate', ('control-resume', '--lease-id', 'lease-1')),
            )
        finally:
            recovered.db.close()

    def test_crash_before_stop_marker_cannot_start_child_stopped_by_operator(self):
        docker = FakeDocker()
        lifecycle = FakeLifecycle()
        admin, docker, lifecycle = self.backup_admin(
            'backup-recovery-stop-not-completed', docker, lifecycle)
        docker.inspect_values['secondmate']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'exited', 'StartedAt': '',
        })
        operation_id = str(uuid.uuid4())
        child_root = admin.backup_root / 'team-sandbox'
        child_root.mkdir(parents=True)
        recovery_record = child_root / ('.recovery.' + operation_id + '.json')
        recovery_record.write_text(json.dumps(recovery_marker(
            'team-sandbox', operation_id, FIN_ID, FIN_IMAGE,
            generation=lifecycle.generation, stop_completed=False,
        )))
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': lifecycle.generation, 'parameters': {},
            'principal': PARENT, 'child_id': 'team-sandbox',
        }
        rows = [{'child_id': 'team-sandbox', 'payload': json.dumps(payload)}]

        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            self.refusal('admin_unknown', admin.recover_interrupted, rows)

        self.assertFalse(docker.inspect_values['secondmate']['State']['Running'])
        self.assertFalse(any(
            argv == [DOCKER, 'start', FIN_ID] for argv in docker.argv()))
        self.assertFalse(json.loads(recovery_record.read_text())['restored'])

    def test_stale_completed_stop_marker_without_executing_row_cannot_start_child(self):
        docker = FakeDocker()
        lifecycle = FakeLifecycle()
        admin, docker, lifecycle = self.backup_admin(
            'backup-recovery-stale-marker', docker, lifecycle)
        docker.inspect_values['secondmate']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'exited', 'StartedAt': '',
        })
        operation_id = str(uuid.uuid4())
        child_root = admin.backup_root / 'team-sandbox'
        child_root.mkdir(parents=True)
        recovery_record = child_root / ('.recovery.' + operation_id + '.json')
        recovery_record.write_text(json.dumps(recovery_marker(
            'team-sandbox', operation_id, FIN_ID, FIN_IMAGE,
            generation=lifecycle.generation, stop_completed=True,
        )))

        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            self.refusal('admin_unknown', admin.recover_interrupted, [])

        self.assertFalse(docker.inspect_values['secondmate']['State']['Running'])
        self.assertFalse(any(
            argv == [DOCKER, 'start', FIN_ID] for argv in docker.argv()))
        self.assertFalse(json.loads(recovery_record.read_text())['restored'])

    def test_executing_backup_without_marker_does_not_start_dormant_toan(self):
        docker = FakeDocker()
        lifecycle = FakeLifecycle()
        admin, docker, lifecycle = self.backup_admin(
            'backup-no-recovery-marker', docker, lifecycle)
        docker.inspect_values['toanmytran-bot']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'created', 'StartedAt': '',
        })
        operation_id = str(uuid.uuid4())
        payload = {
            'operation_id': operation_id, 'action': 'backup',
            'expected_generation': lifecycle.generation, 'parameters': {},
            'principal': PARENT, 'child_id': 'toanmytran-bot',
        }
        with self.app.db:
            self.app.db.execute(
                'INSERT INTO admin_operations VALUES (?,?,?,?,?,NULL)',
                (operation_id, 'toanmytran-bot', 'digest', json.dumps(payload), 'executing'),
            )
        self.app.db.close()
        self.app = None

        recovered = Application(
            self.config, {}, lifecycle=lifecycle,
            administration=admin, recover_interrupted=True)
        try:
            status = recovered.handle(
                'GET',
                f'/v1/children/toanmytran-bot/admin/operations/{operation_id}',
                PARENT)
            self.assertEqual(status['state'], 'unknown')
            self.assertFalse(docker.inspect_values['toanmytran-bot']['State']['Running'])
            self.assertFalse(any(
                argv == [DOCKER, 'start', TOAN_ID] for argv in docker.argv()))
        finally:
            recovered.db.close()

    def test_orphan_helper_cleanup_does_not_start_dormant_child(self):
        docker = FakeDocker()
        lifecycle = FakeLifecycle()
        admin, docker, lifecycle = self.backup_admin(
            'backup-orphan-helper', docker, lifecycle)
        docker.inspect_values['toanmytran-bot']['State'].update({
            'Running': False, 'Paused': False, 'Status': 'created', 'StartedAt': '',
        })
        operation_id = str(uuid.uuid4())
        child_root = admin.backup_root / 'toanmytran-bot'
        child_root.mkdir(parents=True)
        cidfile = child_root / ('.helper.' + operation_id + '.cid')
        helper_name = 'fm-backup-toanmytran-bot-' + operation_id
        mount_spec = (
            'type=bind,source=/srv/firstmate/children/toanmytran-bot/home,'
            'target=/home/nguye/toanmytran-bot,readonly,bind-propagation=rprivate')
        command = admin._backup_command(
            self.children['toanmytran-bot'], admin.manifests['toanmytran-bot'])
        docker.popen(
            [DOCKER, 'run', '--name', helper_name, '--cidfile', str(cidfile),
             '--log-driver', 'none', '--network', 'none', '--read-only',
             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
             '--pids-limit', '64', '--memory', '268435456', '--cpus', '0.5',
             '--user', '1000:1000', '--entrypoint', '/usr/bin/tar',
             '--mount', mount_spec,
             '--label', 'io.maycha.fleet.operation=state-backup',
             '--label', 'io.maycha.fleet.child-id=toanmytran-bot',
             '--label', 'io.maycha.fleet.operation-id=' + operation_id,
             TOAN_IMAGE, *command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={})
        docker.calls.clear()

        with (patch('administration._protected_directory', side_effect=lambda path: path),
              patch.object(Path, 'lstat', secure_test_lstat)):
            admin.recover_interrupted([])
        self.assertFalse(cidfile.exists())
        self.assertEqual(len(docker.removed_helpers), 1)
        self.assertFalse(docker.inspect_values['toanmytran-bot']['State']['Running'])
        self.assertFalse(any(
            argv == [DOCKER, 'start', TOAN_ID] for argv in docker.argv()))

    def test_storage_guard_runs_before_backup_directory_or_partial_file(self):
        docker = FakeDocker()
        lifecycle = FakeLifecycle()
        admin = Administration(
            copy.deepcopy(admin_config()), self.children, lifecycle,
            docker=DOCKER, runner=docker,
            disk_usage=lambda _path: SimpleNamespace(
                total=100 * GIB, used=90 * GIB, free=10 * GIB),
            popen=lambda *_args, **_kwargs: self.fail('tar process must not start'),
        )
        admin.backup_root = self.root / 'storage-guard-backups'
        operation_id = str(uuid.uuid4())
        with patch.object(Path, 'mkdir') as mkdir, patch('administration.os.open') as os_open:
            self.refusal(
                'storage_blocked', admin._backup,
                'team-sandbox', self.children['team-sandbox'],
                lifecycle.generation, operation_id, {})
        mkdir.assert_not_called()
        os_open.assert_not_called()
        self.assertTrue(any('/usr/bin/du' in argv for argv in docker.argv()))
        self.assertFalse(any('/usr/bin/tar' in argv for argv in docker.argv()))
        self.assertEqual(
            lifecycle.runtime_calls[-1],
            ('secondmate', ('control-resume', '--lease-id', 'lease-1')),
        )


class ControlClientDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'firstmate-home'
        self.home.mkdir()
        self.config_path = self.root / 'client.json'
        self.config_path.write_text('{}')

    def tearDown(self):
        self.temp.cleanup()

    def control_argv(self, operation_id, action='restart', generation='runtime:g1',
                     *, include_home=True):
        argv = ['client.py', '--config', str(self.config_path)]
        if include_home:
            argv.extend(['--home', str(self.home)])
        argv.extend([
            'control', 'team-sandbox', action,
            '--expected-generation', generation,
            '--operation-id', operation_id,
        ])
        return argv

    @staticmethod
    def run_main(argv, fake_client):
        with (patch.object(parent_client, 'Client', return_value=fake_client),
              patch.object(parent_client.sys, 'argv', argv),
              patch('builtins.print')):
            return parent_client.main()

    def test_control_persists_exact_uuid_and_payload_before_post(self):
        operation_id = '11111111-2222-4333-8444-555555555555'
        payload = {
            'operation_id': operation_id,
            'action': 'restart',
            'expected_generation': 'runtime:g1',
        }
        record = (
            self.home / 'state' / 'parent-control' / 'control-operations' /
            (operation_id + '.json'))
        test = self

        class InspectingClient:
            parent_id = 'firstmate'

            def __init__(self):
                self.calls = []

            def call(self, method, path, sent_payload=None, *, timeout=45):
                test.assertTrue(record.exists(), 'journal must exist before HTTP POST')
                test.assertEqual(record.read_bytes(), canonical(payload) + b'\n')
                test.assertEqual(sent_payload, payload)
                self.calls.append((method, path, sent_payload, timeout))
                return {'operation_id': operation_id, 'state': 'complete'}

        fake = InspectingClient()
        self.run_main(self.control_argv(operation_id), fake)
        self.assertEqual(
            fake.calls,
            [('POST', '/v1/children/team-sandbox/control', payload, 90)],
        )

    def test_control_requires_home_and_rejects_same_uuid_with_different_payload(self):
        operation_id = '22222222-3333-4444-8555-666666666666'

        class SuccessfulClient:
            parent_id = 'firstmate'

            def __init__(self):
                self.calls = []

            def call(self, method, path, payload=None, *, timeout=45):
                self.calls.append((method, path, payload, timeout))
                return {'operation_id': operation_id, 'state': 'complete'}

        fake = SuccessfulClient()
        with patch.dict(parent_client.os.environ, {'FM_HOME': ''}):
            with self.assertRaises(Refusal) as missing_home:
                self.run_main(
                    self.control_argv(operation_id, include_home=False), fake)
        self.assertEqual(missing_home.exception.code, 'refused')
        self.assertIn('FM_HOME', str(missing_home.exception))
        self.assertEqual(fake.calls, [])

        self.run_main(self.control_argv(operation_id, action='restart'), fake)
        self.assertEqual(len(fake.calls), 1)
        with self.assertRaises(Refusal) as collision:
            self.run_main(self.control_argv(operation_id, action='stop'), fake)
        self.assertEqual(collision.exception.code, 'refused')
        self.assertIn('different content', str(collision.exception))
        self.assertEqual(len(fake.calls), 1)

    def test_control_transport_unknown_reports_durable_uuid_and_path(self):
        operation_id = '33333333-4444-4555-8666-777777777777'
        record = (
            self.home / 'state' / 'parent-control' / 'control-operations' /
            (operation_id + '.json'))

        class TimeoutClient:
            parent_id = 'firstmate'

            def call(self, method, path, payload=None, *, timeout=45):
                if not record.exists():
                    raise AssertionError('timeout happened before durable journaling')
                raise Refusal(
                    'Control request completion is unknown; retain its exact request ID.',
                    503, 'transport_unknown')

        with self.assertRaises(Refusal) as caught:
            self.run_main(self.control_argv(operation_id), TimeoutClient())
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, 'transport_unknown')
        self.assertIn(operation_id, str(caught.exception))
        self.assertIn(str(record), str(caught.exception))
        self.assertTrue(record.exists())


if __name__ == '__main__':
    unittest.main()
