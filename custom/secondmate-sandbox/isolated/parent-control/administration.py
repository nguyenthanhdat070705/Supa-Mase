"""Scoped host administration for explicitly registered child containers.

The parent receives useful operational controls, never a Docker socket, shell,
container name, host path, image tag, mount, environment or argv surface.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import threading
import time

from common import MAX_BODY, Refusal, canonical, digest, identifier, text, uuid_id


IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
NETWORK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
SAFE_BACKUP_ROOT = PurePosixPath("/var/lib/firstmate-control/backups")
TELEGRAM_TOKEN = re.compile(r"(?<![A-Za-z0-9_])\d{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
BEARER = re.compile(r"(?i)(?<![A-Za-z0-9_])bearer[ \t]+[A-Za-z0-9._~+/-]+={0,}")
API_SECRET = re.compile(r"(?i)(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9._~+/-]{16,}={0,}")
JWT = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
SHA256 = re.compile(r"[0-9a-f]{64}")
UUID_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _safe_relative(value):
    if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value or '\x00' in value:
        raise Refusal('Backup path is outside the fixed child-home allowlist.')
    path = PurePosixPath(value)
    if str(path) != value or any(part in ('', '.', '..') for part in path.parts):
        raise Refusal('Backup path is outside the fixed child-home allowlist.')
    if path.parts[0] not in ('config', 'data', 'state'):
        raise Refusal('Backup path is outside the fixed child-home allowlist.')
    if any(part in ('.codex', 'secrets') for part in path.parts):
        raise Refusal('Credential paths cannot be included in a child backup.')
    return value


def _protected_directory(path):
    raw_path = os.fspath(path)
    path = Path(raw_path)
    pure_path = PurePosixPath(raw_path) if isinstance(raw_path, str) else None
    if (pure_path is None or not path.is_absolute()
            or pure_path.as_posix() != raw_path or '..' in pure_path.parts
            or pure_path.is_relative_to(SAFE_BACKUP_ROOT) is False):
        raise Refusal('Backup root must remain under the protected broker state directory.')
    for item in [path, *path.parents]:
        if item == Path('/'):
            break
        try:
            info = item.lstat()
        except FileNotFoundError:
            if item == path:
                continue
            raise Refusal('Backup parent directory is unavailable.') from None
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refusal('Backup directory ownership or permissions are unsafe.')
        if not stat.S_ISDIR(info.st_mode):
            raise Refusal('Backup path is not a protected directory.')
    return path


class Administration:
    """Fixed-scope Docker operations for parent and separately scoped operator."""

    def __init__(self, config, children, lifecycle, *, docker='/usr/bin/docker', runner=None,
                 popen=None, disk_usage=None, secret_values=()):
        self.config = config or None
        self.children = children
        self.lifecycle = lifecycle
        self.docker = docker
        self.runner = runner or subprocess.run
        self.popen = popen or subprocess.Popen
        self.disk_usage = disk_usage or shutil.disk_usage
        self.secret_values = tuple(value for value in secret_values if isinstance(value, str) and len(value) >= 16)
        self.backup_lock = threading.Lock()
        self.manifests = {}
        self.max_log_bytes = 262_144
        self.backup_root = None
        self.reserve_bytes = 10 * 1024**3
        self.reserve_percent = 10
        if self.config is not None:
            self._validate_config()

    @property
    def enabled(self):
        return self.config is not None

    def _validate_config(self):
        allowed = {'children', 'backup_root', 'reserve_bytes', 'reserve_percent', 'max_log_bytes'}
        if not isinstance(self.config, dict) or set(self.config) - allowed or 'children' not in self.config:
            raise Refusal('Invalid scoped administration configuration.')
        entries = self.config['children']
        if not isinstance(entries, dict) or not 1 <= len(entries) <= 16:
            raise Refusal('Scoped administration requires an explicit child allowlist.')
        bind_sources = {}
        for child_id, manifest in entries.items():
            identifier(child_id)
            child = self.children.get(child_id)
            if child is None:
                raise Refusal('Administrative child must exist in the broker registry.')
            keys = {'container', 'pinned_image_id', 'expected_user', 'expected_mounts',
                    'expected_labels', 'expected_environment', 'expected_networks',
                    'expected_log_config', 'expected_restart_policy',
                    'start_allowed', 'resource_profiles',
                    'backup_paths', 'max_backup_bytes', 'runbooks'}
            if not isinstance(manifest, dict) or set(manifest) != keys:
                raise Refusal('Administrative child manifest is incomplete.')
            if manifest['container'] != child['container'] or manifest['expected_user'] != child.get('exec_user', '1000:1000'):
                raise Refusal('Administrative identity must match the fixed child registry.')
            if not isinstance(manifest['pinned_image_id'], str) or not IMAGE_ID.fullmatch(manifest['pinned_image_id']):
                raise Refusal('Administrative image identity must be a pinned image ID.')
            if type(manifest['start_allowed']) is not bool:
                raise Refusal('Administrative start gate must be explicit.')
            if (manifest['expected_restart_policy'] not in ('no', 'unless-stopped')
                    or (manifest['start_allowed'] is False
                        and manifest['expected_restart_policy'] != 'no')):
                raise Refusal('Administrative restart policy must be exact and preserve the start gate.')
            mounts = manifest['expected_mounts']
            if not isinstance(mounts, list) or not mounts:
                raise Refusal('Administrative mount identity must be explicit.')
            normalized_mounts = []
            for mount in mounts:
                if (not isinstance(mount, dict) or set(mount) != {'source', 'destination', 'rw', 'type'}
                        or not isinstance(mount['source'], str) or '\x00' in mount['source']
                        or any(character == ',' or ord(character) < 32 for character in mount['source'])
                        or (mount['type'] != 'tmpfs' and not mount['source'].startswith('/'))
                        or '..' in mount['source'].split('/') or not isinstance(mount['destination'], str)
                        or '\x00' in mount['destination']
                        or any(character == ',' or ord(character) < 32 for character in mount['destination'])
                        or not mount['destination'].startswith('/') or '..' in mount['destination'].split('/')
                        or type(mount['rw']) is not bool or mount['type'] not in ('bind', 'volume', 'tmpfs')):
                    raise Refusal('Invalid expected child mount.')
                if (PurePosixPath(mount['destination']).as_posix() != mount['destination']
                        or (mount['type'] != 'tmpfs'
                            and PurePosixPath(mount['source']).as_posix() != mount['source'])):
                    raise Refusal('Expected child mounts must use canonical POSIX paths.')
                if (mount['destination'] in ('/var/run/docker.sock', '/run/docker.sock')
                        or mount['source'] in ('/var/run/docker.sock', '/run/docker.sock', '/')):
                    raise Refusal('Docker sockets cannot be part of an administrative child manifest.')
                normalized_mounts.append((mount['source'], mount['destination'], mount['rw'], mount['type']))
            if len(normalized_mounts) != len(set(normalized_mounts)):
                raise Refusal('Expected child mounts must be distinct.')
            bind_sources[child_id] = [PurePosixPath(mount['source']) for mount in mounts
                                      if mount['type'] == 'bind']
            child_home = PurePosixPath(child['home'])
            home_mounts = [mount for mount in mounts if mount['type'] == 'bind'
                           and PurePosixPath(mount['destination']) == child_home]
            if len(home_mounts) != 1 or home_mounts[0]['rw'] is not True:
                raise Refusal('Administrative backup requires one exact writable bind for child home.')
            labels = manifest['expected_labels']
            if (not isinstance(labels, dict) or not labels or any(not isinstance(k, str) or not isinstance(v, str)
                                                    or not k or len(k) > 200 or len(v) > 500
                                                    for k, v in labels.items())):
                raise Refusal('Invalid expected child labels.')
            expected_environment = manifest['expected_environment']
            environment_keys = {'HOME', 'SM_INSTANCE_FILE', 'SM_CAPTAIN_ID', 'SM_TEAM_GROUP_IDS',
                                'SM_FORBIDDEN_BOT_ID', 'SM_BOT_TOKEN_FILE', 'TZ'}
            if (not isinstance(expected_environment, dict) or not environment_keys <= set(expected_environment)
                    or len(expected_environment) > 100
                    or any(not isinstance(name, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,99}', name)
                           or not isinstance(setting, str) or '\x00' in setting or len(setting) > 4096
                           for name, setting in expected_environment.items())
                    or any(re.search(r'(?i)(?:^|_)(?:TOKEN|SECRET|PASSWORD|API_KEY)(?:_|$)', name)
                           and name != 'SM_BOT_TOKEN_FILE' for name in expected_environment)
                    or expected_environment['HOME'] != '/home/nguye'
                    or expected_environment['SM_INSTANCE_FILE'] != '/opt/secondmate/instance.json'
                    or expected_environment['SM_BOT_TOKEN_FILE'] != '/run/secrets/secondmate-bot-token'
                    or expected_environment['TZ'] not in ('Asia/Ho_Chi_Minh', 'Asia/Bangkok')
                    or not re.fullmatch(r'[1-9][0-9]{0,19}', expected_environment['SM_CAPTAIN_ID'])
                    or not re.fullmatch(r'[1-9][0-9]{0,19}', expected_environment['SM_FORBIDDEN_BOT_ID'])
                    or not re.fullmatch(r'(?:-[1-9][0-9]{0,19}(?:,-[1-9][0-9]{0,19})*)?',
                                        expected_environment['SM_TEAM_GROUP_IDS'])):
                raise Refusal('Invalid expected child authority environment.')
            networks = manifest['expected_networks']
            if (not isinstance(networks, dict) or not 1 <= len(networks) <= 4
                    or any(not isinstance(name, str) or not NETWORK_NAME.fullmatch(name)
                           or not isinstance(network_id, str) or not CONTAINER_ID.fullmatch(network_id)
                           for name, network_id in networks.items())):
                raise Refusal('Invalid expected child network identity.')
            log_config = manifest['expected_log_config']
            if (not isinstance(log_config, dict) or set(log_config) != {'max-size', 'max-file'}
                    or not isinstance(log_config['max-size'], str)
                    or not isinstance(log_config['max-file'], str)
                    or not re.fullmatch(r'[1-9][0-9]?m', log_config['max-size'])
                    or not re.fullmatch(r'[1-9]|10', log_config['max-file'])):
                raise Refusal('Managed child logs require a bounded rotation policy.')
            profiles = manifest['resource_profiles']
            if not isinstance(profiles, dict) or not profiles:
                raise Refusal('At least one fixed resource profile is required.')
            for name, profile in profiles.items():
                if not isinstance(name, str) or not PROFILE.fullmatch(name) or not isinstance(profile, dict) or set(profile) != {'memory_bytes', 'nano_cpus', 'pids_limit'}:
                    raise Refusal('Invalid resource profile.')
                if (type(profile['memory_bytes']) is not int or not 256 * 1024**2 <= profile['memory_bytes'] <= 64 * 1024**3
                        or type(profile['nano_cpus']) is not int or not 100_000_000 <= profile['nano_cpus'] <= 32_000_000_000
                        or type(profile['pids_limit']) is not int or not 32 <= profile['pids_limit'] <= 8192):
                    raise Refusal('Resource profile exceeds the bounded child envelope.')
            paths = manifest['backup_paths']
            if not isinstance(paths, list) or not paths or len(paths) > 32:
                raise Refusal('Backup paths must be a small explicit list.')
            manifest['backup_paths'] = [_safe_relative(value) for value in paths]
            if len(manifest['backup_paths']) != len(set(manifest['backup_paths'])):
                raise Refusal('Backup paths must be distinct.')
            if type(manifest['max_backup_bytes']) is not int or not 1_048_576 <= manifest['max_backup_bytes'] <= 8 * 1024**3:
                raise Refusal('Backup byte limit is invalid.')
            runbooks = manifest['runbooks']
            supported = {'runtime-health', 'home-usage', 'processes'}
            if not isinstance(runbooks, list) or not set(runbooks) <= supported or len(runbooks) != len(set(runbooks)):
                raise Refusal('Administrative runbook allowlist is invalid.')
            self.manifests[child_id] = manifest
        source_items = list(bind_sources.items())
        for index, (child_id, sources) in enumerate(source_items):
            for other_id, other_sources in source_items[index + 1:]:
                for source in sources:
                    for other_source in other_sources:
                        if (source.is_relative_to(other_source)
                                or other_source.is_relative_to(source)):
                            raise Refusal(
                                'Administrative child bind sources must be isolated and non-overlapping: '
                                + child_id + ' and ' + other_id + '.')
        root = self.config.get('backup_root', str(SAFE_BACKUP_ROOT))
        pure_root = PurePosixPath(root) if isinstance(root, str) else None
        if (pure_root is None or not pure_root.is_absolute()
                or pure_root.as_posix() != root or '..' in pure_root.parts
                or not pure_root.is_relative_to(SAFE_BACKUP_ROOT)):
            raise Refusal('Backup root must remain under the protected broker state directory.')
        # The rollout creates and protects this directory. Filesystem checks are
        # repeated immediately before every read/write, keeping offline config
        # validation portable and avoiding a check/use gap.
        self.backup_root = Path(root)
        reserve = self.config.get('reserve_bytes', self.reserve_bytes)
        percent = self.config.get('reserve_percent', self.reserve_percent)
        log_bytes = self.config.get('max_log_bytes', self.max_log_bytes)
        if type(reserve) is not int or reserve < 1024**3 or type(percent) is not int or not 1 <= percent <= 50:
            raise Refusal('Administrative storage reserve is invalid.')
        if type(log_bytes) is not int or not 16_384 <= log_bytes <= 262_144:
            raise Refusal('Administrative log response bound is invalid.')
        self.reserve_bytes, self.reserve_percent, self.max_log_bytes = reserve, percent, log_bytes

    def manifest(self, child_id):
        value = self.manifests.get(child_id)
        if value is None:
            raise Refusal('Child is outside the administrative allowlist.', 403, 'forbidden')
        return value

    def authorize(self, principal, child_id, child):
        self.manifest(child_id)
        if principal['role'] == 'parent' and principal['id'] == child['parent_id']:
            return
        if principal['role'] == 'operator':
            return
        raise Refusal('Administrative access requires the owning parent or scoped operator.', 403, 'forbidden')

    def _run(self, arguments, *, timeout=30, maximum=MAX_BODY):
        try:
            result = self.runner([self.docker, *arguments], input=b'', stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=timeout, check=False,
                                 env={'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'})
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal('Administrative Docker result is unknown; inspect before retry.', 503, 'admin_unknown') from None
        if result.returncode or len(result.stdout) > maximum:
            raise Refusal('Administrative Docker operation failed; diagnostics were withheld.', 503, 'admin_unknown')
        return result.stdout

    def _inspect(self, child_id, child):
        manifest = self.manifest(child_id)
        try:
            value = json.loads(self._run(['inspect', '--format', '{{json .}}', manifest['container']]))
        except (ValueError, UnicodeError):
            raise Refusal('Container identity response is invalid.', 503, 'admin_unknown') from None
        if not isinstance(value, dict) or not CONTAINER_ID.fullmatch(value.get('Id', '')):
            raise Refusal('Container identity is unverified.', 503, 'admin_unknown')
        config, host, state = value.get('Config', {}), value.get('HostConfig', {}), value.get('State', {})
        network_mode = host.get('NetworkMode', '')
        cap_drop = host.get('CapDrop') or []
        tmpfs = host.get('Tmpfs') or {}
        log_config = host.get('LogConfig') or {}
        environment = {}
        for item in config.get('Env') or []:
            if not isinstance(item, str) or '=' not in item:
                raise Refusal('Container environment is invalid.', 409, 'identity_mismatch')
            name, setting = item.split('=', 1)
            if name in environment:
                raise Refusal('Container environment contains duplicate keys.', 409, 'identity_mismatch')
            environment[name] = setting
        forbidden_environment = [name for name in environment
                                 if re.search(r'(?i)(?:^|_)(?:TOKEN|SECRET|PASSWORD|API_KEY)(?:_|$)', name)
                                 and name != 'SM_BOT_TOKEN_FILE']
        networks = value.get('NetworkSettings', {}).get('Networks') or {}
        actual_networks = {}
        for name, entry in networks.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            network_id = entry.get('NetworkID')
            if network_id == '' and state.get('Running') is False:
                try:
                    network_id = self._run(
                        ['network', 'inspect', '--format', '{{.Id}}', name], maximum=256
                    ).decode('ascii', 'strict').strip()
                except UnicodeError:
                    raise Refusal('Dormant child network identity is invalid.',
                                  503, 'admin_unknown') from None
            actual_networks[name] = network_id
        if (value.get('Name') != '/' + manifest['container']
                or value.get('Image') != manifest['pinned_image_id']
                or config.get('User') != manifest['expected_user']
                or config.get('Entrypoint') != ['/bin/bash', '/opt/secondmate/entrypoint.sh']
                or host.get('Privileged') is not False
                or host.get('PidMode') not in ('', 'private')
                or host.get('IpcMode') not in ('', 'private')
                or host.get('UTSMode') not in ('', 'private')
                or network_mode in ('host', 'none') or str(network_mode).startswith('container:')
                or host.get('CapAdd') not in (None, [])
                or 'ALL' not in cap_drop
                or host.get('SecurityOpt') != ['no-new-privileges:true']
                or host.get('PublishAllPorts') is not False
                or host.get('PortBindings') not in (None, {})
                or host.get('Devices') not in (None, [])
                or host.get('DeviceRequests') not in (None, [])
                or host.get('AutoRemove') is not False
                or host.get('Init') is not True
                or set(tmpfs) - {'/tmp'}
                or (host.get('RestartPolicy') or {}).get('Name') != manifest['expected_restart_policy']
                or log_config.get('Type') != 'json-file'
                or log_config.get('Config') != manifest['expected_log_config']
                or forbidden_environment
                or environment != manifest['expected_environment']
                or network_mode not in manifest['expected_networks']
                or actual_networks != manifest['expected_networks']
                or type(state.get('Running')) is not bool):
            raise Refusal('Container no longer matches its root-owned administrative identity.', 409, 'identity_mismatch')
        labels = config.get('Labels') or {}
        if any(labels.get(key) != expected for key, expected in manifest['expected_labels'].items()):
            raise Refusal('Container deployment labels no longer match.', 409, 'identity_mismatch')
        actual_mounts = []
        for mount in value.get('Mounts') or []:
            destination = mount.get('Destination')
            if destination in ('/var/run/docker.sock', '/run/docker.sock'):
                raise Refusal('Docker socket detected in a managed child.', 409, 'identity_mismatch')
            if mount.get('Type') == 'bind' and mount.get('Propagation') != 'rprivate':
                raise Refusal('Managed child bind propagation is not private.', 409, 'identity_mismatch')
            actual_mounts.append((mount.get('Source'), destination, mount.get('RW'), mount.get('Type')))
        expected_mounts = [(item['source'], item['destination'], item['rw'], item['type'])
                           for item in manifest['expected_mounts']]
        if sorted(actual_mounts) != sorted(expected_mounts):
            raise Refusal('Container mounts no longer match the administrative manifest.', 409, 'identity_mismatch')
        identity = {
            'container_id': value['Id'], 'image_id': value['Image'], 'created': value.get('Created'),
            'started_at': state.get('StartedAt'), 'running': state['Running'], 'paused': state.get('Paused') is True,
            'status': state.get('Status'),
            'user': config.get('User'), 'mounts': sorted(actual_mounts),
            'labels': {key: labels.get(key) for key in sorted(manifest['expected_labels'])},
            'environment': {key: environment.get(key) for key in sorted(manifest['expected_environment'])},
            'networks': actual_networks,
            'privileged': host.get('Privileged'), 'pid_mode': host.get('PidMode'),
            'network_mode': network_mode, 'cap_add': host.get('CapAdd'),
            'cap_drop': host.get('CapDrop'), 'security_opt': host.get('SecurityOpt'),
            'memory_bytes': host.get('Memory'), 'nano_cpus': host.get('NanoCpus'),
            'pids_limit': host.get('PidsLimit'), 'restart_policy': (host.get('RestartPolicy') or {}).get('Name'),
            'log_driver': (host.get('LogConfig') or {}).get('Type'),
            'log_config': (host.get('LogConfig') or {}).get('Config'),
        }
        identity['identity_generation'] = 'admin:' + digest(identity)
        return identity

    def lifecycle_gate(self, child_id, child, action):
        manifest = self.manifest(child_id)
        self._inspect(child_id, child)
        if action == 'start' and manifest['start_allowed'] is not True:
            raise Refusal('Child activation is not yet authorized.', 409, 'activation_required')

    def _active_profile(self, manifest, identity):
        for name, profile in manifest['resource_profiles'].items():
            if all(identity[field] == profile[field] for field in ('memory_bytes', 'nano_cpus', 'pids_limit')):
                return name
        return None

    def diagnostics(self, principal, child_id, child):
        self.authorize(principal, child_id, child)
        manifest = self.manifest(child_id)
        identity = self._inspect(child_id, child)
        try:
            runtime = self.lifecycle.status(child)
        except Refusal:
            runtime = {'reachability': 'unknown'}
        stats = None
        if identity['running']:
            try:
                raw = self._run(['stats', '--no-stream', '--format', '{{json .}}', identity['container_id']], maximum=65_536)
                stats = json.loads(raw)
            except (Refusal, ValueError, UnicodeError):
                stats = {'reachability': 'unknown'}
        return {
            'child_id': child_id,
            'container': {key: identity[key] for key in ('container_id', 'image_id', 'created', 'started_at', 'running', 'paused', 'status', 'user', 'restart_policy', 'log_driver', 'identity_generation')},
            'isolation': {'privileged': identity['privileged'], 'pid_mode': identity['pid_mode'],
                          'network_mode': identity['network_mode'], 'cap_add': identity['cap_add'],
                          'cap_drop': identity['cap_drop'], 'security_opt': identity['security_opt'],
                          'mounts': [{'destination': mount[1], 'rw': mount[2], 'type': mount[3]}
                                     for mount in identity['mounts']],
                          'docker_socket': False},
            'resources': {'memory_bytes': identity['memory_bytes'], 'nano_cpus': identity['nano_cpus'],
                          'pids_limit': identity['pids_limit'], 'active_profile': self._active_profile(manifest, identity),
                          'available_profiles': sorted(manifest['resource_profiles'])},
            'runtime': runtime, 'stats': stats, 'start_allowed': manifest['start_allowed'],
            'runbooks': list(manifest['runbooks']),
        }

    def _redact(self, raw, boundary_truncated=False):
        value = CONTROL.sub(' ', raw.decode('utf-8', 'replace'))
        if boundary_truncated:
            newline = value.find('\n')
            value = value[newline + 1:] if newline >= 0 else '[TRUNCATED OVERSIZED LOG LINE]'
        for secret in self.secret_values:
            value = value.replace(secret, '[REDACTED]')
        value = TELEGRAM_TOKEN.sub('[REDACTED]', value)
        value = BEARER.sub('[REDACTED]', value)
        value = API_SECRET.sub('[REDACTED]', value)
        value = JWT.sub('[REDACTED]', value)
        return value

    def logs(self, principal, child_id, child, query):
        self.authorize(principal, child_id, child)
        if set(query) - {'tail', 'since_seconds'} or any(len(values) != 1 for values in query.values()):
            raise Refusal('Logs accept only one bounded tail and since_seconds value.')
        try:
            tail = int(query.get('tail', ['100'])[0])
            since = int(query.get('since_seconds', ['3600'])[0])
        except (ValueError, TypeError):
            raise Refusal('Invalid log bounds.') from None
        if not 1 <= tail <= 500 or not 0 <= since <= 86_400:
            raise Refusal('Log bounds exceed the administrative limit.')
        identity = self._inspect(child_id, child)
        process = None
        timer = None
        timed_out = threading.Event()
        try:
            process = self.popen([self.docker, 'logs', '--tail', str(tail), '--since', str(since) + 's',
                                  '--timestamps', identity['container_id']], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env={'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'})
            def expire():
                timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass
            timer = threading.Timer(30, expire)
            timer.daemon = True
            timer.start()
            retain = self.max_log_bytes + 8192
            raw, total = bytearray(), 0
            while True:
                chunk = process.stdout.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                raw.extend(chunk)
                if len(raw) > retain:
                    del raw[:len(raw) - retain]
            process.wait()
        except OSError:
            raise Refusal('Log result is unknown.', 503, 'admin_unknown') from None
        finally:
            if timer is not None:
                timer.cancel()
            if process is not None and process.poll() is None:
                process.kill(); process.wait()
        if timed_out.is_set():
            raise Refusal('Log result is unknown.', 503, 'admin_unknown')
        if process.returncode:
            raise Refusal('Logs are unavailable; Docker diagnostics were withheld.', 503, 'admin_unknown')
        boundary_truncated = total > len(raw)
        sanitized = self._redact(bytes(raw), boundary_truncated).encode('utf-8')
        truncated = boundary_truncated or len(sanitized) > self.max_log_bytes
        if truncated:
            sanitized = sanitized[-self.max_log_bytes:]
        content = sanitized.decode('utf-8', 'ignore')
        return {'child_id': child_id, 'tail': tail, 'since_seconds': since,
                'truncated': truncated, 'content': content}

    def runbook(self, principal, child_id, child, name):
        self.authorize(principal, child_id, child)
        manifest = self.manifest(child_id)
        if name not in manifest['runbooks']:
            raise Refusal('Runbook is outside the fixed administrative catalog.', 403, 'forbidden')
        identity = self._inspect(child_id, child)
        if name == 'runtime-health':
            return {'child_id': child_id, 'runbook': name, 'result': self.lifecycle.status(child)}
        if not identity['running']:
            raise Refusal('This runbook requires a running child.', 409, 'not_running')
        if name == 'processes':
            raw = self._run(['top', identity['container_id'], '-eo', 'pid,user,comm'], maximum=65_536)
            return {'child_id': child_id, 'runbook': name, 'result': self._redact(raw)}
        paths = [child['home'].rstrip('/') + '/' + value for value in manifest['backup_paths']]
        raw = self._run(['exec', '-i', '--user', manifest['expected_user'], '--env', 'FM_HOME=' + child['home'],
                         identity['container_id'], '/usr/bin/du', '-sk', '--one-file-system', *paths], maximum=65_536)
        rows = []
        for line in raw.decode('utf-8', 'replace').splitlines():
            match = re.fullmatch(r'(\d+)\s+(.+)', line)
            if not match or match[2] not in paths:
                raise Refusal('Home usage runbook returned an invalid response.', 503, 'admin_unknown')
            rows.append({'path': match[2].removeprefix(child['home'].rstrip('/') + '/'), 'kib': int(match[1])})
        return {'child_id': child_id, 'runbook': name, 'result': rows}

    def _prepare_mutation(self, child_id, child, expected_generation):
        before = self.lifecycle.status(child)
        if before.get('generation') != expected_generation:
            raise Refusal('Runtime generation changed; obtain fresh diagnostics.', 409, 'stale_generation')
        identity = self._inspect(child_id, child)
        lease_id = None
        try:
            if identity['running']:
                if before.get('identity_verified') is not True or before.get('safe_to_stop') is not True:
                    raise Refusal('Active or uncertain work prevents administration.', 409, 'active_work')
                lease = self.lifecycle.runtime(child, 'control-check', '--quiesce', '--expected-generation', expected_generation)
                if lease.get('generation') != expected_generation or not lease.get('lease_id') or lease.get('safe_to_stop') is not True:
                    raise Refusal('Runtime refused the administrative quiesce lease.', 409, 'lease_refused')
                lease_id = lease['lease_id']
                check = self.lifecycle.runtime(child, 'control-check')
                if check.get('generation') != expected_generation or check.get('lease_id') != lease_id or check.get('safe_to_stop') is not True:
                    raise Refusal('Runtime changed after quiesce; inspect before proceeding.', 409, 'lease_changed')
            latest = self._inspect(child_id, child)
            if latest['container_id'] != identity['container_id'] or latest['identity_generation'] != identity['identity_generation']:
                raise Refusal('Container identity changed before administration.', 409, 'stale_generation')
            return identity, lease_id
        except Exception:
            if lease_id:
                try:
                    self._resume(child, lease_id)
                except Refusal:
                    raise Refusal('Administrative preparation failed and resume is unknown.', 503, 'admin_unknown') from None
            raise

    def _resume(self, child, lease_id, attempts=1):
        if not lease_id:
            return
        for attempt in range(attempts):
            try:
                result = self.lifecycle.runtime(child, 'control-resume', '--lease-id', lease_id)
                if result.get('resumed') is True:
                    return
            except Refusal:
                pass
            if attempt + 1 < attempts:
                time.sleep(2)
        raise Refusal('Runtime resume result is unknown.', 503, 'admin_unknown')

    def _resource_profile(self, child_id, child, expected_generation, parameters):
        if not isinstance(parameters, dict) or set(parameters) != {'profile'}:
            raise Refusal('Resource update requires one fixed profile name.')
        profile_name = parameters['profile']
        manifest = self.manifest(child_id)
        profile = manifest['resource_profiles'].get(profile_name)
        if profile is None:
            raise Refusal('Resource profile is outside the fixed allowlist.', 403, 'forbidden')
        identity, lease_id = self._prepare_mutation(child_id, child, expected_generation)
        try:
            cpus = ('%.9f' % (profile['nano_cpus'] / 1_000_000_000)).rstrip('0').rstrip('.')
            self._run(['update', '--memory', str(profile['memory_bytes']), '--cpus', cpus,
                       '--pids-limit', str(profile['pids_limit']), identity['container_id']], timeout=45)
            try:
                after = self._inspect(child_id, child)
            except Refusal:
                raise Refusal('Resource update was applied but verification is unknown.',
                              503, 'admin_unknown') from None
            if (after['container_id'] != identity['container_id']
                    or any(after[field] != profile[field] for field in ('memory_bytes', 'nano_cpus', 'pids_limit'))):
                raise Refusal('Resource update could not be verified.', 503, 'admin_unknown')
            return {'action': 'resource-profile', 'profile': profile_name,
                    'resources': {key: after[key] for key in ('memory_bytes', 'nano_cpus', 'pids_limit')},
                    'container_id': after['container_id']}
        finally:
            self._resume(child, lease_id)

    def _estimate_backup(self, child_id, child, identity):
        manifest = self.manifest(child_id)
        paths = [child['home'].rstrip('/') + '/' + value for value in manifest['backup_paths']]
        raw = self._run(['exec', '-i', '--user', manifest['expected_user'], '--env', 'FM_HOME=' + child['home'],
                         identity['container_id'], '/usr/bin/du', '-sb', '--one-file-system', *paths], maximum=65_536)
        total = 0
        for line in raw.decode('utf-8', 'replace').splitlines():
            match = re.fullmatch(r'(\d+)\s+(.+)', line)
            if not match or match[2] not in paths:
                raise Refusal('Backup estimate is invalid.', 503, 'admin_unknown')
            total += int(match[1])
        if total <= 0 or total > manifest['max_backup_bytes']:
            raise Refusal('Child state exceeds its bounded backup quota.', 409, 'backup_too_large')
        usage_path = self.backup_root
        while not usage_path.exists() and usage_path != usage_path.parent:
            usage_path = usage_path.parent
        usage = self.disk_usage(usage_path)
        reserve = max(self.reserve_bytes, usage.total * self.reserve_percent // 100)
        live_ceiling = min(manifest['max_backup_bytes'], usage.free - reserve - 16 * 1024**2)
        if live_ceiling <= 0 or total > live_ceiling:
            raise Refusal('Host storage reserve blocks this backup.', 409, 'storage_blocked')
        return total, live_ceiling

    @staticmethod
    def _backup_command(child, manifest):
        return ['--format=pax', '--numeric-owner', '--one-file-system',
                '--exclude=.codex', '--exclude=auth.json', '--exclude=*token*', '--exclude=*secret*',
                '--exclude=*.pem', '--exclude=*.key', '-C', child['home'], '-cf', '-', '--',
                *manifest['backup_paths']]

    @staticmethod
    def _backup_home_mount(child, manifest):
        child_home = PurePosixPath(child['home'])
        matches = [mount for mount in manifest['expected_mounts'] if mount['type'] == 'bind'
                   and PurePosixPath(mount['destination']) == child_home]
        if len(matches) != 1:
            raise Refusal('Administrative backup home identity is unavailable.', 503, 'admin_unknown')
        return matches[0]

    def _helper_identity(self, reference, child_id, operation_id, image_id, child):
        try:
            value = json.loads(self._run(['inspect', '--format', '{{json .}}', reference], maximum=MAX_BODY))
        except (ValueError, UnicodeError):
            raise Refusal('Backup helper identity is invalid.', 503, 'admin_unknown') from None
        manifest = self.manifest(child_id)
        home_mount = self._backup_home_mount(child, manifest)
        config = value.get('Config') or {}
        labels = (value.get('Config') or {}).get('Labels') or {}
        host = value.get('HostConfig') or {}
        helper_id = value.get('Id', '')
        expected_name = '/fm-backup-' + child_id + '-' + operation_id
        actual_mounts = [(mount.get('Source'), mount.get('Destination'), mount.get('RW'),
                          mount.get('Type'), mount.get('Propagation'))
                         for mount in value.get('Mounts') or []]
        expected_mounts = [(home_mount['source'], home_mount['destination'], False, 'bind', 'rprivate')]
        if (not CONTAINER_ID.fullmatch(helper_id) or value.get('Name') != expected_name
                or value.get('Image') != image_id
                or config.get('User') != manifest['expected_user']
                or config.get('Entrypoint') != ['/usr/bin/tar']
                or config.get('Cmd') != self._backup_command(child, manifest)
                or labels.get('io.maycha.fleet.operation') != 'state-backup'
                or labels.get('io.maycha.fleet.child-id') != child_id
                or labels.get('io.maycha.fleet.operation-id') != operation_id
                or host.get('NetworkMode') != 'none' or host.get('Privileged') is not False
                or host.get('ReadonlyRootfs') is not True or set(host.get('CapDrop') or []) != {'ALL'}
                or host.get('CapAdd') not in (None, [])
                or host.get('SecurityOpt') != ['no-new-privileges:true']
                or (host.get('LogConfig') or {}).get('Type') != 'none'
                or host.get('PidsLimit') != 64 or host.get('Memory') != 268435456
                or host.get('NanoCpus') != 500000000
                or host.get('AutoRemove') is not False
                or host.get('VolumesFrom') not in (None, [])
                or host.get('PortBindings') not in (None, {})
                or host.get('Devices') not in (None, [])
                or host.get('DeviceRequests') not in (None, [])
                or actual_mounts != expected_mounts):
            raise Refusal('Backup helper no longer matches its fixed identity.', 503, 'admin_unknown')
        return helper_id

    def _helper_id_from_cidfile(self, cidfile, child_id, operation_id, image_id, child):
        helper_id = None
        try:
            info = cidfile.lstat()
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > 100):
                raise Refusal('Backup helper ID file is unsafe.', 503, 'admin_unknown')
            helper_id = cidfile.read_text().strip()
            if not CONTAINER_ID.fullmatch(helper_id):
                raise Refusal('Backup helper ID is invalid.', 503, 'admin_unknown')
        helper_name = 'fm-backup-' + child_id + '-' + operation_id
        raw = self._run([
            'ps', '-a', '--no-trunc', '--filter', 'name=^/' + helper_name + '$',
            '--filter', 'label=io.maycha.fleet.operation=state-backup',
            '--filter', 'label=io.maycha.fleet.child-id=' + child_id,
            '--filter', 'label=io.maycha.fleet.operation-id=' + operation_id,
            '--format', '{{.ID}}'], maximum=4096)
        listed = [line.strip() for line in raw.decode('ascii', 'strict').splitlines() if line.strip()]
        if any(not CONTAINER_ID.fullmatch(value) for value in listed) or len(listed) > 1:
            raise Refusal('Backup helper lookup is ambiguous.', 503, 'admin_unknown')
        if not listed:
            return None
        if helper_id is not None and listed[0] != helper_id:
            raise Refusal('Backup helper ID no longer matches its protected record.', 503, 'admin_unknown')
        return self._helper_identity(listed[0], child_id, operation_id, image_id, child)

    @staticmethod
    def _fsync_directory(path):
        if os.name == 'nt':
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_recovery_marker(self, path, child_id, operation_id, identity,
                               expected_generation, lease_id):
        marker = {
            'schema': 'firstmate-backup-recovery.v1', 'child_id': child_id,
            'operation_id': operation_id, 'container_id': identity['container_id'],
            'image_id': identity['image_id'], 'runtime_generation': expected_generation,
            'lease_id': lease_id,
            'verified_running': True, 'stop_intent': True,
            'stop_completed': False, 'restored': False,
        }
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb', closefd=True) as stream:
                stream.write(canonical(marker) + b'\n'); stream.flush(); os.fsync(stream.fileno())
            self._fsync_directory(path.parent)
        except Exception:
            try:
                path.unlink()
                self._fsync_directory(path.parent)
            except OSError:
                pass
            raise Refusal('Backup recovery intent could not be made durable.', 503, 'admin_unknown') from None
        return marker

    def _read_recovery_marker(self, path, child_id, operation_id):
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > 4096):
                raise Refusal('Backup recovery marker is unsafe.', 503, 'admin_unknown')
            value = json.loads(path.read_bytes())
        except Refusal:
            raise
        except (OSError, ValueError, UnicodeError):
            raise Refusal('Backup recovery marker is unreadable.', 503, 'admin_unknown') from None
        keys = {'schema', 'child_id', 'operation_id', 'container_id', 'image_id',
                'runtime_generation', 'lease_id', 'verified_running', 'stop_intent',
                'stop_completed', 'restored'}
        manifest = self.manifest(child_id)
        if (not isinstance(value, dict) or set(value) != keys
                or value.get('schema') != 'firstmate-backup-recovery.v1'
                or value.get('child_id') != child_id or value.get('operation_id') != operation_id
                or not isinstance(value.get('container_id'), str)
                or not CONTAINER_ID.fullmatch(value['container_id'])
                or value.get('image_id') != manifest['pinned_image_id']
                or not isinstance(value.get('runtime_generation'), str)
                or not value['runtime_generation'] or len(value['runtime_generation']) > 300
                or not isinstance(value.get('lease_id'), str)
                or not value['lease_id'] or len(value['lease_id']) > 300
                or value.get('verified_running') is not True or value.get('stop_intent') is not True
                or type(value.get('stop_completed')) is not bool
                or type(value.get('restored')) is not bool):
            raise Refusal('Backup recovery marker does not prove a prior running child.',
                          503, 'admin_unknown')
        return value

    def _transition_recovery_marker(self, path, marker, **changes):
        transitioned = {**marker, **changes}
        temporary = path.with_name('.partial.' + path.name)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb', closefd=True) as stream:
                stream.write(canonical(transitioned) + b'\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except Exception:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            raise Refusal('Backup recovery transition could not be made durable.', 503, 'admin_unknown') from None
        return transitioned

    def _mark_recovery_stop_completed(self, path, marker):
        return self._transition_recovery_marker(path, marker, stop_completed=True)

    def _mark_recovery_restored(self, path, marker):
        return self._transition_recovery_marker(path, marker, restored=True)

    def _backup(self, child_id, child, expected_generation, operation_id, parameters):
        if parameters != {}:
            raise Refusal('Backup uses only the root-owned path manifest.')
        manifest = self.manifest(child_id)
        identity, lease_id = self._prepare_mutation(child_id, child, expected_generation)
        stop_attempted = False
        recovery_marker = None
        recovery_record = None
        restart_verified = False
        cleanup_unknown = False
        try:
            if not identity['running']:
                raise Refusal('State backup requires a running, verified idle child.', 409, 'not_running')
            estimate, live_ceiling = self._estimate_backup(child_id, child, identity)
            try:
                # Capacity is checked against the nearest existing ancestor
                # before any backup directory or partial artifact is created.
                self.backup_root.mkdir(mode=0o700, exist_ok=True)
                _protected_directory(self.backup_root)
            except OSError:
                raise Refusal('Protected backup storage is unavailable.',
                              503, 'admin_unknown') from None
            child_root = self.backup_root / child_id
            child_root.mkdir(mode=0o700, exist_ok=True)
            child_root = _protected_directory(child_root)
            final = child_root / (operation_id + '.tar')
            partial = child_root / ('.partial.' + operation_id + '.tar')
            manifest_path = child_root / (operation_id + '.json')
            temporary_record = manifest_path.with_name('.partial.' + manifest_path.name)
            cidfile = child_root / ('.helper.' + operation_id + '.cid')
            recovery_record = child_root / ('.recovery.' + operation_id + '.json')
            recovery_temporary = recovery_record.with_name('.partial.' + recovery_record.name)
            helper_name = 'fm-backup-' + child_id + '-' + operation_id
            if any(path.exists() for path in (final, partial, manifest_path, temporary_record,
                                               cidfile, recovery_record, recovery_temporary)):
                raise Refusal('Backup identifier already exists.', 409, 'conflict')
            home_mount = self._backup_home_mount(child, manifest)
            mount_spec = ('type=bind,source=' + home_mount['source'] + ',target=' + home_mount['destination']
                          + ',readonly,bind-propagation=rprivate')
            args = [self.docker, 'run', '--name', helper_name, '--cidfile', str(cidfile),
                    '--log-driver', 'none', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges:true', '--pids-limit', '64', '--memory', '268435456',
                    '--cpus', '0.5', '--user', manifest['expected_user'], '--entrypoint', '/usr/bin/tar',
                    '--mount', mount_spec,
                    '--label', 'io.maycha.fleet.operation=state-backup',
                    '--label', 'io.maycha.fleet.child-id=' + child_id,
                    '--label', 'io.maycha.fleet.operation-id=' + operation_id,
                    identity['image_id'], *self._backup_command(child, manifest)]
            environment = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
            descriptor = None
            process = None
            timer = None
            timed_out = threading.Event()
            helper_id = None
            committed = False
            result = None
            failure = None
            size, sha256 = 0, hashlib.sha256()
            try:
                recovery_marker = self._write_recovery_marker(
                    recovery_record, child_id, operation_id, identity, expected_generation, lease_id)
                descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb', closefd=True) as stream:
                    descriptor = None
                    stop_attempted = True
                    try:
                        self._run(['stop', '--time', '20', identity['container_id']], timeout=45, maximum=65_536)
                    except Refusal:
                        try:
                            stopped = self._inspect(child_id, child)['running'] is False
                        except Refusal:
                            raise Refusal('Backup stop result is unknown.', 503, 'admin_unknown') from None
                        if not stopped:
                            raise
                    try:
                        running_after_stop = self._inspect(child_id, child)['running']
                    except Refusal:
                        raise Refusal('Backup stop verification is unknown.', 503, 'admin_unknown') from None
                    if running_after_stop:
                        raise Refusal('Child stop could not be verified.', 503, 'admin_unknown')
                    recovery_marker = self._mark_recovery_stop_completed(
                        recovery_record, recovery_marker)
                    try:
                        process = self.popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, env=environment)
                    except OSError:
                        raise Refusal('Backup could not start.', 503, 'admin_unknown') from None
                    def expire():
                        timed_out.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
                    timer = threading.Timer(120, expire)
                    timer.daemon = True
                    timer.start()
                    while True:
                        chunk = process.stdout.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > live_ceiling:
                            process.kill()
                            raise Refusal('Backup exceeded its byte limit after stopping the child.', 503, 'admin_unknown')
                        if size % (16 * 1024**2) < len(chunk):
                            usage = self.disk_usage(self.backup_root)
                            reserve = max(self.reserve_bytes, usage.total * self.reserve_percent // 100)
                            if usage.free <= reserve + 16 * 1024**2:
                                process.kill()
                                raise Refusal('Host storage changed after stopping the child.', 503, 'admin_unknown')
                        stream.write(chunk); sha256.update(chunk)
                    stream.flush(); os.fsync(stream.fileno())
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                    raise Refusal('Backup result is unknown.', 503, 'admin_unknown') from None
                if timed_out.is_set():
                    raise Refusal('Backup result is unknown.', 503, 'admin_unknown')
                helper_id = self._helper_id_from_cidfile(
                    cidfile, child_id, operation_id, identity['image_id'], child)
                if helper_id is None:
                    raise Refusal('Backup helper ID is unavailable.', 503, 'admin_unknown')
                if process.returncode:
                    raise Refusal('Backup failed; archive diagnostics were withheld.', 503, 'admin_unknown')
                if size == 0 or size > estimate + 16 * 1024**2:
                    raise Refusal('Backup size could not be reconciled.', 503, 'admin_unknown')
                record = {'schema': 'firstmate-child-backup.v1', 'backup_id': operation_id,
                          'child_id': child_id, 'created_unix': int(time.time()), 'size_bytes': size,
                          'sha256': sha256.hexdigest(), 'paths': manifest['backup_paths'],
                          'container_id': identity['container_id'], 'image_id': identity['image_id'],
                          'runtime_generation': expected_generation}
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                record_fd = os.open(temporary_record, flags, 0o600)
                with os.fdopen(record_fd, 'wb', closefd=True) as stream:
                    stream.write(canonical(record) + b'\n'); stream.flush(); os.fsync(stream.fileno())
                os.replace(partial, final); os.replace(temporary_record, manifest_path)
                self._fsync_directory(child_root)
                committed = True
                result = record
            except Refusal as error:
                failure = error
            except Exception:
                failure = Refusal('Backup result is unknown; inspect before retry.', 503, 'admin_unknown')
            finally:
                cleanup_unknown = False
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        cleanup_unknown = True
                if timer is not None:
                    timer.cancel()
                if process is not None and process.poll() is None:
                    try:
                        process.kill(); process.wait()
                    except (OSError, subprocess.SubprocessError):
                        cleanup_unknown = True
                try:
                    if helper_id is None:
                        helper_id = self._helper_id_from_cidfile(
                            cidfile, child_id, operation_id, identity['image_id'], child)
                    if helper_id is not None:
                        self._run(['rm', '-f', helper_id], timeout=30, maximum=65_536)
                except (Refusal, OSError, UnicodeError):
                    cleanup_unknown = True
                try:
                    if cidfile.exists():
                        cidfile.unlink()
                except OSError:
                    cleanup_unknown = True
                if stop_attempted:
                    try:
                        self._run(['start', identity['container_id']], timeout=45, maximum=65_536)
                    except Refusal:
                        pass
                    try:
                        after_start = self._inspect(child_id, child)
                        if (after_start['container_id'] != identity['container_id']
                                or after_start['running'] is not True):
                            raise Refusal('Child restart after backup could not be verified.',
                                          503, 'admin_unknown')
                        self._recover_runtime(
                            child_id, child, allow_start=False,
                            expected_container_id=identity['container_id'], expected_lease_id=lease_id,
                            require_running=True)
                        restart_verified = True
                    except Refusal:
                        cleanup_unknown = True
                else:
                    restart_verified = True
                for candidate in (partial, temporary_record):
                    try:
                        if candidate.exists():
                            candidate.unlink()
                    except OSError:
                        cleanup_unknown = True
                if not committed:
                    for candidate in (final, manifest_path):
                        try:
                            if candidate.exists():
                                candidate.unlink()
                        except OSError:
                            cleanup_unknown = True
                try:
                    self._fsync_directory(child_root)
                except OSError:
                    cleanup_unknown = True
                if cleanup_unknown:
                    failure = Refusal('Backup cleanup or child restart is unknown.', 503, 'admin_unknown')
            if failure is not None:
                if stop_attempted and failure.status < 500:
                    raise Refusal('Backup changed runtime state before failing; inspect before retry.',
                                  503, 'admin_unknown') from None
                raise failure
            return result
        finally:
            if not stop_attempted:
                self._resume(child, lease_id)
            if recovery_marker is not None and restart_verified:
                recovery_marker = self._mark_recovery_restored(recovery_record, recovery_marker)
                if not cleanup_unknown:
                    try:
                        recovery_record.unlink()
                        self._fsync_directory(recovery_record.parent)
                    except OSError:
                        raise Refusal('Restored backup marker cleanup is unknown.',
                                      503, 'admin_unknown') from None

    def _validate_backup_record(self, path, child_id, manifest):
        match = re.fullmatch(r'(' + UUID_NAME.pattern + r')\.json', path.name)
        if not match:
            raise Refusal('Backup metadata name is invalid.', 503, 'admin_unknown')
        backup_id = uuid_id(match.group(1))
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > 65_536):
                raise Refusal('Backup metadata is unsafe.', 503, 'admin_unknown')
            value = json.loads(path.read_bytes())
        except Refusal:
            raise
        except (OSError, ValueError, UnicodeError):
            raise Refusal('Backup metadata is unreadable.', 503, 'admin_unknown') from None
        expected_keys = {'schema', 'backup_id', 'child_id', 'created_unix', 'size_bytes', 'sha256',
                         'paths', 'container_id', 'image_id', 'runtime_generation'}
        if (not isinstance(value, dict) or set(value) != expected_keys
                or value.get('schema') != 'firstmate-child-backup.v1'
                or value.get('backup_id') != backup_id or value.get('child_id') != child_id
                or type(value.get('created_unix')) is not int or value['created_unix'] < 0
                or type(value.get('size_bytes')) is not int
                or not 0 < value['size_bytes'] <= manifest['max_backup_bytes']
                or not isinstance(value.get('sha256'), str) or not SHA256.fullmatch(value['sha256'])
                or value.get('paths') != manifest['backup_paths']
                or not isinstance(value.get('container_id'), str)
                or not CONTAINER_ID.fullmatch(value['container_id'])
                or value.get('image_id') != manifest['pinned_image_id']
                or not isinstance(value.get('runtime_generation'), str)
                or not value['runtime_generation'] or len(value['runtime_generation']) > 300):
            raise Refusal('Backup metadata does not match its fixed child manifest.', 503, 'admin_unknown')
        archive = path.parent / (backup_id + '.tar')
        try:
            archive_info = archive.lstat()
        except FileNotFoundError:
            raise Refusal('Backup archive is unavailable.', 503, 'admin_unknown') from None
        if (not stat.S_ISREG(archive_info.st_mode) or archive_info.st_uid != 0
                or archive_info.st_mode & 0o077 or archive_info.st_nlink != 1
                or archive_info.st_size != value['size_bytes']):
            raise Refusal('Backup archive metadata is unsafe.', 503, 'admin_unknown')
        return value

    def recover_interrupted(self, rows):
        """Reconcile only broker-owned backup helpers after an exclusive restart."""
        if not self.enabled:
            return
        executing_backups = set()
        for row in rows:
            try:
                payload = json.loads(row['payload'])
                child_id = identifier(row['child_id'])
                uuid_id(payload['operation_id'])
            except (KeyError, TypeError, ValueError, Refusal):
                raise Refusal('Interrupted administrative journal is invalid.', 503, 'admin_unknown') from None
            if (payload.get('action') not in ('backup', 'resource-profile')
                    or payload.get('child_id') != child_id or child_id not in self.manifests):
                raise Refusal('Interrupted administrative journal scope is invalid.', 503, 'admin_unknown')
            if payload['action'] == 'backup':
                executing_backups.add((child_id, payload['operation_id']))
        recovery_targets = set()
        orphan_helpers = set()
        for child_id in self.manifests:
            child_root = self.backup_root / child_id
            if not child_root.exists():
                continue
            _protected_directory(child_root)
            for marker in child_root.glob('.recovery.*.json'):
                match = re.fullmatch(r'\.recovery\.(' + UUID_NAME.pattern + r')\.json', marker.name)
                if not match:
                    raise Refusal('Unexpected backup recovery marker is present.', 503, 'admin_unknown')
                recovery_targets.add((child_id, uuid_id(match.group(1))))
            for cidfile in child_root.glob('.helper.*.cid'):
                match = re.fullmatch(r'\.helper\.(' + UUID_NAME.pattern + r')\.cid', cidfile.name)
                if not match:
                    raise Refusal('Unexpected backup helper record is present.', 503, 'admin_unknown')
                orphan_helpers.add((child_id, uuid_id(match.group(1))))
        for child_id, operation_id in sorted(recovery_targets):
            self._recover_backup(
                child_id, operation_id,
                allow_start=(child_id, operation_id) in executing_backups)
            orphan_helpers.discard((child_id, operation_id))
        for child_id, operation_id in sorted(orphan_helpers):
            self._recover_orphan_helper(child_id, operation_id)

    def _recover_runtime(self, child_id, child, *, allow_start, expected_container_id=None,
                         expected_lease_id=None, require_running=False):
        identity = self._inspect(child_id, child)
        if expected_container_id is not None and identity['container_id'] != expected_container_id:
            raise Refusal('Recovery container identity changed.', 503, 'admin_unknown')
        if not identity['running']:
            if not allow_start:
                if require_running:
                    raise Refusal('Child stopped during runtime recovery.', 503, 'admin_unknown')
                return
            self._run(['start', identity['container_id']], timeout=45, maximum=65_536)
            identity = self._inspect(child_id, child)
            if identity['container_id'] != expected_container_id or not identity['running']:
                raise Refusal('Recovered child did not start.', 503, 'admin_unknown')
        for attempt in range(30):
            try:
                status = self.lifecycle.status(child)
                if status.get('identity_verified') is True:
                    lease_id = status.get('lease_id')
                    if lease_id:
                        if expected_lease_id is not None and lease_id != expected_lease_id:
                            raise Refusal('Recovered runtime has a different quiesce lease.',
                                          503, 'admin_unknown')
                        self._resume(child, lease_id, attempts=1)
                    return
            except Refusal:
                pass
            if attempt < 29:
                time.sleep(2)
        raise Refusal('Recovered child runtime readiness is unknown.', 503, 'admin_unknown')

    def _recover_orphan_helper(self, child_id, operation_id):
        child = self.children[child_id]
        manifest = self.manifest(child_id)
        child_root = _protected_directory(self.backup_root / child_id)
        cidfile = child_root / ('.helper.' + operation_id + '.cid')
        helper_id = self._helper_id_from_cidfile(
            cidfile, child_id, operation_id, manifest['pinned_image_id'], child)
        if helper_id is not None:
            self._run(['rm', '-f', helper_id], timeout=30, maximum=65_536)
        if cidfile.exists():
            cidfile.unlink()
        self._fsync_directory(child_root)

    def _recover_backup(self, child_id, operation_id, *, allow_start):
        child = self.children[child_id]
        manifest = self.manifest(child_id)
        child_root = _protected_directory(self.backup_root / child_id)
        cidfile = child_root / ('.helper.' + operation_id + '.cid')
        recovery_record = child_root / ('.recovery.' + operation_id + '.json')
        marker = self._read_recovery_marker(recovery_record, child_id, operation_id)
        unknown = False
        identity = self._inspect(child_id, child)
        if (identity['container_id'] != marker['container_id']
                or identity['image_id'] != marker['image_id']):
            raise Refusal('Interrupted backup child identity changed.', 503, 'admin_unknown')
        try:
            helper_id = self._helper_id_from_cidfile(
                cidfile, child_id, operation_id, manifest['pinned_image_id'], child)
            if helper_id is not None:
                self._run(['rm', '-f', helper_id], timeout=30, maximum=65_536)
        except (Refusal, OSError, UnicodeError):
            unknown = True
        try:
            if cidfile.exists():
                cidfile.unlink()
        except OSError:
            unknown = True
        try:
            for candidate in (child_root / ('.partial.' + operation_id + '.tar'),
                              child_root / ('.partial.' + operation_id + '.json'),
                              child_root / ('.partial..recovery.' + operation_id + '.json')):
                if candidate.exists():
                    candidate.unlink()
            final = child_root / (operation_id + '.tar')
            record = child_root / (operation_id + '.json')
            if final.exists() != record.exists():
                for candidate in (final, record):
                    if candidate.exists():
                        candidate.unlink()
            elif record.exists():
                self._validate_backup_record(record, child_id, manifest)
        except (Refusal, OSError, ValueError, UnicodeError):
            # Archive integrity cannot prevent restoration of a bot which the
            # durable marker proves was running before this operation.
            unknown = True
        if marker['restored'] is False:
            try:
                if identity['running']:
                    self._recover_runtime(
                        child_id, child, allow_start=False,
                        expected_container_id=marker['container_id'],
                        expected_lease_id=marker['lease_id'], require_running=True)
                elif allow_start and marker['stop_completed'] is True:
                    self._recover_runtime(
                        child_id, child, allow_start=True,
                        expected_container_id=marker['container_id'],
                        expected_lease_id=marker['lease_id'])
                else:
                    raise Refusal('Recovery marker does not authorize starting this stopped child.',
                                  503, 'admin_unknown')
                marker = self._mark_recovery_restored(recovery_record, marker)
            except Refusal:
                unknown = True
        if marker['restored'] is True and not unknown:
            try:
                recovery_record.unlink()
            except OSError:
                unknown = True
        try:
            if child_root.exists():
                self._fsync_directory(child_root)
        except OSError:
            unknown = True
        if unknown:
            raise Refusal('Interrupted backup recovery is unknown; operator inspection is required.',
                          503, 'admin_unknown')

    def backups(self, principal, child_id, child):
        self.authorize(principal, child_id, child)
        root = self.backup_root / child_id
        if not root.exists():
            return {'child_id': child_id, 'backups': []}
        _protected_directory(root)
        results = []
        paths = [path for path in root.iterdir()
                 if re.fullmatch(UUID_NAME.pattern + r'\.json', path.name)]
        for path in paths:
            results.append(self._validate_backup_record(path, child_id, self.manifest(child_id)))
        results.sort(key=lambda value: (value['created_unix'], value['backup_id']), reverse=True)
        return {'child_id': child_id, 'backups': results[:100], 'restore': 'operator-only-offline'}

    def operation(self, principal, child_id, child, payload, db, lock, child_lock):
        self.authorize(principal, child_id, child)
        if (not isinstance(payload, dict) or set(payload) != {'operation_id', 'action', 'expected_generation', 'parameters'}
                or payload['action'] not in ('resource-profile', 'backup') or not isinstance(payload['parameters'], dict)):
            raise Refusal('Invalid administrative operation.')
        operation_id = uuid_id(payload['operation_id'])
        text(payload['expected_generation'], 300)
        stored = {**payload, 'principal': principal, 'child_id': child_id}
        fingerprint = digest(stored)
        with child_lock:
            with lock, db:
                old = db.execute('SELECT * FROM admin_operations WHERE operation_id=?', (operation_id,)).fetchone()
                if old:
                    if old['digest'] != fingerprint:
                        raise Refusal('Administrative operation ID conflicts with existing content.', 409, 'conflict')
                    return {'operation_id': operation_id, 'state': old['state'],
                            'result': json.loads(old['result']) if old['result'] else None, 'duplicate': True}
                db.execute('INSERT INTO admin_operations VALUES (?,?,?,?,?,NULL)',
                           (operation_id, child_id, fingerprint, canonical(stored).decode(), 'executing'))
            try:
                if payload['action'] == 'resource-profile':
                    result = self._resource_profile(child_id, child, payload['expected_generation'], payload['parameters'])
                else:
                    # The filesystem reserve is host-global, so backup estimate,
                    # archive streaming and reconciliation are one serialized unit.
                    with self.backup_lock:
                        result = self._backup(child_id, child, payload['expected_generation'],
                                              operation_id, payload['parameters'])
            except Refusal as error:
                with lock, db:
                    state = 'unknown' if error.status >= 500 else 'refused'
                    db.execute('UPDATE admin_operations SET state=?,result=? WHERE operation_id=?',
                               (state, canonical({'error': error.code}).decode(), operation_id))
                raise
            except Exception:
                with lock, db:
                    db.execute('UPDATE admin_operations SET state=?,result=? WHERE operation_id=?',
                               ('unknown', canonical({'error': 'admin_unknown'}).decode(), operation_id))
                raise Refusal('Administrative result is unknown; inspect before retry.', 503, 'admin_unknown') from None
            with lock, db:
                db.execute('UPDATE admin_operations SET state=?,result=? WHERE operation_id=?',
                           ('complete', canonical(result).decode(), operation_id))
            return {'operation_id': operation_id, 'state': 'complete', 'result': result}

    def operation_status(self, principal, child_id, child, operation_id, db, lock):
        self.authorize(principal, child_id, child)
        operation_id = uuid_id(operation_id)
        with lock:
            row = db.execute('SELECT * FROM admin_operations WHERE operation_id=? AND child_id=?',
                             (operation_id, child_id)).fetchone()
        if not row:
            raise Refusal('Administrative operation is unavailable.', 404, 'not_found')
        return {'operation_id': operation_id, 'state': row['state'],
                'result': json.loads(row['result']) if row['result'] else None}
