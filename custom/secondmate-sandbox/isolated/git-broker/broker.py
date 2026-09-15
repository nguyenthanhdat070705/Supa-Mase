#!/usr/bin/env python3
"""Trusted Linux Git transport + PR publisher. Never run inside the agent container."""
import base64
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass

CONFIG_PATH = Path('/etc/secondmate-git-broker/config.json')
UPSTREAM = 'https://github.com/DemandPlanningMC/demand-planning-maycha.git'
REPOSITORY = 'DemandPlanningMC/demand-planning-maycha'
ALIAS = 'team-sandbox-repo'
PREFIX = 'refs/heads/sandbox/'
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
PR_URL = re.compile(r'https://github\.com/DemandPlanningMC/demand-planning-maycha/pull/[1-9][0-9]*\Z')


class BrokerError(Exception):
    """Only fixed, nonsensitive messages may enter this exception."""


@dataclass(frozen=True)
class Config:
    repository: str
    state_dir: str
    credential_command: tuple
    git: str = '/usr/bin/git'
    gh: str = '/usr/bin/gh'


def load_config():
    info = CONFIG_PATH.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise BrokerError('Config must be a root-owned regular file without group/other write.')
    data = json.loads(CONFIG_PATH.read_text())
    if set(data) != {'repository', 'state_dir', 'credential_command'}:
        raise BrokerError('Invalid operator configuration fields.')
    for key in ('repository', 'state_dir'):
        if not isinstance(data[key], str) or not Path(data[key]).is_absolute():
            raise BrokerError('Operator paths must be absolute.')
    command = data['credential_command']
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise BrokerError('Credential command must be a nonempty argv array.')
    if not Path(command[0]).is_absolute():
        raise BrokerError('Credential command executable must be absolute.')
    return Config(data['repository'], data['state_dir'], tuple(command))


def environment(config, extra_config=(), quarantine=False):
    # Do not inherit user-controlled Git config, proxy, shell startup or auth env.
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'HOME': config.state_dir, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_ASKPASS': '/bin/false', 'SSH_ASKPASS': '/bin/false'}
    settings = [('safe.directory', config.repository), ('credential.helper', ''),
                ('http.proxy', ''), ('http.followRedirects', 'false'), *extra_config]
    env['GIT_CONFIG_COUNT'] = str(len(settings))
    for i, (key, value) in enumerate(settings):
        env[f'GIT_CONFIG_KEY_{i}'] = key
        env[f'GIT_CONFIG_VALUE_{i}'] = value
    if quarantine:
        # Only pre-receive needs these, inherited from our clean receive-pack.
        for key in ('GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_QUARANTINE_PATH'):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def run(argv, env, timeout=120):
    # Never forward subprocess output: authentication errors may contain secrets.
    try:
        return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BrokerError('A broker subprocess could not start or timed out.') from None


def git(config, *args, quarantine=False, extra_config=()):
    return run([config.git, '--git-dir=' + config.repository, *args],
               environment(config, extra_config, quarantine))


def transport_command(original):
    try:
        pieces = shlex.split(original, posix=True)
    except ValueError:
        raise BrokerError('Invalid SSH Git command.') from None
    if len(pieces) != 2 or pieces[0] not in ('git-upload-pack', 'git-receive-pack') or pieces[1] != ALIAS:
        raise BrokerError('Only Git upload/receive for team-sandbox-repo is allowed.')
    return pieces[0].removeprefix('git-')


def validate_ref(config, ref):
    if not ref.startswith(PREFIX) or len(ref) > 200:
        raise BrokerError('Only refs/heads/sandbox/<name> may be written or published.')
    suffix = ref[len(PREFIX):]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*', suffix):
        raise BrokerError('Invalid sandbox branch name.')
    if git(config, 'check-ref-format', ref).returncode:
        raise BrokerError('Invalid Git branch name.')


def parse_updates(stream):
    result = []
    for line in stream:
        if len(result) >= 1024 or len(line) > 1024:
            raise BrokerError('Too many updates or malformed update.')
        parts = line.split()
        if len(parts) != 3 or not SHA.fullmatch(parts[0]) or not SHA.fullmatch(parts[1]):
            raise BrokerError('Malformed Git update.')
        result.append(tuple(parts))
    if not result:
        raise BrokerError('No Git updates supplied.')
    return result


def validate_updates(config, updates):
    for old, new, ref in updates:
        validate_ref(config, ref)
        if set(new) == {'0'}:
            raise BrokerError('Branch deletion is forbidden.')
        if git(config, 'cat-file', '-t', new, quarantine=True).stdout.strip() != 'commit':
            raise BrokerError('Sandbox refs must point to commits.')
        if set(old) != {'0'} and git(config, 'merge-base', '--is-ancestor', old, new, quarantine=True).returncode:
            raise BrokerError('Non-fast-forward or unverifiable update is forbidden.')


@contextlib.contextmanager
def publish_lock(config):
    import fcntl  # Linux runtime; offline tests replace the lock on Windows.
    with open(Path(config.state_dir) / 'publish.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_status(config, record):
    name = hashlib.sha256(record['ref'].encode()).hexdigest() + '.json'
    destination = Path(config.state_dir) / name
    temporary = destination.with_suffix('.tmp')
    record['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(record, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def credential(config):
    response = run(list(config.credential_command), environment(config), timeout=30)
    token = response.stdout.strip()
    if response.returncode or not re.fullmatch(r'[!-~]{1,4096}', token):
        raise BrokerError('Operator credential command failed or returned an invalid token.')
    return token


def existing_pr(config, env, branch):
    response = run([config.gh, 'pr', 'list', '--repo', REPOSITORY, '--base', 'main',
                    '--head', branch, '--state', 'open', '--json', 'url', '--limit', '100'], env)
    if response.returncode:
        raise BrokerError('GitHub PR lookup failed.')
    try:
        records = json.loads(response.stdout)
        if not isinstance(records, list):
            raise ValueError()
        urls = [entry['url'] for entry in records]
        if any(not isinstance(url, str) or not PR_URL.fullmatch(url) for url in urls):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise BrokerError('GitHub PR lookup returned an invalid response.') from None
    return urls[0] if urls else None


def publish(config, ref):
    validate_ref(config, ref)
    with publish_lock(config):
        head = git(config, 'rev-parse', '--verify', ref + '^{commit}')
        commit = head.stdout.strip()
        if head.returncode or not SHA.fullmatch(commit):
            raise BrokerError('Sandbox branch does not resolve to a local commit.')
        record = {'ref': ref, 'commit': commit, 'stage': 'received', 'pr_url': None, 'error': None}
        write_status(config, record)
        try:
            token = credential(config)
            encoded = base64.b64encode(('x-access-token:' + token).encode()).decode()
            pushed = git(config, 'push', '--porcelain', UPSTREAM, commit + ':' + ref,
                         extra_config=(('http.extraHeader', 'Authorization: Basic ' + encoded),))
            if pushed.returncode:
                raise BrokerError('Upstream push failed; upstream/PR success is unconfirmed. Retry safely.')
            record['stage'] = 'upstream_pushed'
            write_status(config, record)
            env = environment(config)
            env.update({'GH_TOKEN': token, 'GH_HOST': 'github.com',
                        'GH_CONFIG_DIR': config.state_dir, 'GH_PROMPT_DISABLED': '1',
                        'GH_NO_UPDATE_NOTIFIER': '1'})
            branch = ref.removeprefix('refs/heads/')
            url = existing_pr(config, env, branch)
            if not url:
                created = run([config.gh, 'pr', 'create', '--repo', REPOSITORY,
                               '--base', 'main', '--head', branch,
                               '--title', 'Sandbox: ' + branch,
                               '--body', 'Secondmate sandbox work. Captain review and manual merge required.'], env)
                # Re-read authoritative state even after create errors: retry/race-safe.
                url = existing_pr(config, env, branch)
                if not url:
                    raise BrokerError('Branch pushed, but an open PR could not be confirmed. Retry safely.')
            record.update(stage='pr_ready', pr_url=url)
            write_status(config, record)
            print('BROKER: upstream sandbox branch published; captain review: ' + url, file=sys.stderr)
            return record
        except BrokerError as error:
            record['error'] = str(error)
            write_status(config, record)
            raise


def main(argv=None):
    os.umask(0o077)
    argv = sys.argv[1:] if argv is None else argv
    config = load_config()
    if argv == ['forced-command']:
        operation = transport_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        # Fixed executable, fixed repository; never eval or shell-execute the SSH string.
        os.execve(config.git, [config.git, operation, config.repository], environment(config))
    elif argv == ['pre-receive']:
        validate_updates(config, parse_updates(sys.stdin))
    elif argv == ['post-receive']:
        updates = parse_updates(sys.stdin)
        failed = False
        for old, new, ref in updates:
            try:
                publish(config, ref)
            except BrokerError as error:
                failed = True
                print('BROKER: local receive accepted; publication needs operator retry. ' + str(error), file=sys.stderr)
        return 1 if failed else 0
    elif len(argv) == 2 and argv[0] == 'publish':
        # Operator-only retry. Deliberately unavailable via the forced SSH transport.
        publish(config, argv[1])
    else:
        raise BrokerError('Unsupported broker operation.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BrokerError as error:
        print('BROKER: ' + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Never print traceback/config/argv/credential subprocess output to the client.
        print('BROKER: internal failure; operator should check permissions and configuration.', file=sys.stderr)
        sys.exit(1)
