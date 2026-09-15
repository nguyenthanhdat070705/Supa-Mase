#!/bin/sh
# Root-only installer for the reviewed sandbox Git broker. Never enable shell tracing.
set -eu
exec /usr/bin/python3 -I - "$@" <<'PY'
import base64
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys

ACCOUNT = 'sm-git'
BASE = Path('/var/lib/secondmate-git-broker')
REPO = BASE / 'repo.git'
STATE = BASE / 'state'
LOGIN_HOME = BASE / 'home'
CODE = Path('/usr/local/libexec/secondmate-git-broker')
HELPER = Path('/usr/local/libexec/secondmate-git-token')
CONF = Path('/etc/secondmate-git-broker/config.json')
SUDOERS = Path('/etc/sudoers.d/secondmate-git-broker')
SSHD = Path('/etc/ssh/sshd_config.d/00-secondmate-git-broker.conf')
MANIFEST = BASE / 'installation.json'
STAGE = Path('/home/dat/team-sandbox/git-broker')
PUBKEY = Path('/home/dat/team-sandbox/home/.ssh/id_ed25519.pub')
UPSTREAM = 'https://github.com/DemandPlanningMC/demand-planning-maycha.git'
EXPECTED = {
    'broker.py': '0046aceee7da4cb51978682ab213fe7f73d3b086ff0f6add616c02d2de598a6e',
    'pre-receive': '1746161fbc656600663331c7cd6c9ded529a0e33d082bf157d22511f63ef510e',
    'post-receive': 'd9531c3477bfdd57e010033e3c27e07d2ec3eaad8e3d0bc4ffdda111c227ebe5',
}
CHECK = sys.argv[1:] == ['--check']
if sys.argv[1:] not in ([], ['--check']):
    sys.exit('Usage: install-broker.sh [--check]')
if os.geteuid() != 0:
    sys.exit('Run this installer as root.')
os.umask(0o077)
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
       'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
       'GIT_ASKPASS': '/bin/false', 'SSH_ASKPASS': '/bin/false'}

def fail(message):
    raise RuntimeError(message)

def run(argv, *, env=None, timeout=120, allowed=(0,), input=None):
    # All subprocess output is captured, including Git and token-helper errors.
    try:
        result = subprocess.run(argv, env=env or ENV, input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        fail('A required subprocess failed to start or timed out; no subprocess output was disclosed.')
    if result.returncode not in allowed:
        fail('A required subprocess failed; no subprocess output was disclosed: ' + argv[0])
    return result

def trusted(path, *, directory=False, owner=0, exact_mode=None):
    try:
        info = path.lstat()
    except OSError:
        fail('Required installed path is absent or unreadable: ' + str(path))
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        fail('Unsafe ownership, type or permissions: ' + str(path))
    if exact_mode is not None and stat.S_IMODE(info.st_mode) != exact_mode:
        fail('Unexpected permissions: ' + str(path))

def ancestor_guard(path):
    for parent in reversed(path.parents):
        if parent.exists():
            trusted(parent, directory=True)

def directory(path, owner=0, mode=0o755):
    ancestor_guard(path)
    if not path.exists() and not path.is_symlink():
        if CHECK:
            fail('Missing installed directory: ' + str(path))
        path.mkdir(mode=mode)
        os.chown(path, owner, 0 if owner == 0 else grp.getgrnam(ACCOUNT).gr_gid)
        os.chmod(path, mode)
    trusted(path, directory=True, owner=owner, exact_mode=mode)

def file(path, content, mode=0o644):
    ancestor_guard(path)
    if path.exists() or path.is_symlink():
        trusted(path, exact_mode=mode)
        if path.read_bytes() != content:
            fail('Refusing to replace conflicting installed file: ' + str(path))
        return
    if CHECK:
        fail('Missing installed file: ' + str(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, mode)

def git(*args, env=None, allowed=(0,)):
    return run(['/usr/bin/git', '--git-dir=' + str(REPO), *args], env=env, allowed=allowed)

def repository_config():
    return (b'[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n'
            b'[receive]\n\tdenyDeletes = true\n\tdenyNonFastForwards = true\n'
            b'\tfsckObjects = true\n\tautogc = false\n'
            b'[transfer]\n\tfsckObjects = true\n[gc]\n\tauto = 0\n')

def checked_tree(path, uid, mutate=False):
    # Only the explicitly allowed objects/refs/logs subtrees are writable by sm-git.
    for current, dirs, files in os.walk(path, followlinks=False):
        for target in [Path(current), *[Path(current) / n for n in dirs + files]]:
            info = target.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                fail('Unexpected link or special file in broker storage: ' + str(target))
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                fail('Unexpected hardlink in broker storage: ' + str(target))
            if mutate:
                os.chown(target, uid, grp.getgrnam(ACCOUNT).gr_gid)
                os.chmod(target, 0o755 if stat.S_ISDIR(info.st_mode) else 0o644)
            elif info.st_uid != uid or info.st_mode & 0o022:
                fail('Unexpected writable storage owner or mode: ' + str(target))

def ssh_effective():
    run(['/usr/sbin/sshd', '-t'])
    output = run(['/usr/sbin/sshd', '-T', '-C',
                  'user=sm-git,host=localhost,addr=127.0.0.1']).stdout.decode()
    values = dict(line.split(' ', 1) for line in output.splitlines() if ' ' in line)
    expected = {'passwordauthentication': 'no', 'kbdinteractiveauthentication': 'no',
                'permittty': 'no', 'allowtcpforwarding': 'no', 'x11forwarding': 'no',
                'permittunnel': 'no', 'permituserenvironment': 'no', 'permituserrc': 'no',
                'disableforwarding': 'yes', 'pubkeyauthentication': 'yes',
                'authenticationmethods': 'publickey',
                'authorizedkeysfile': str(LOGIN_HOME / '.ssh/authorized_keys'),
                'forcecommand': '/usr/bin/python3 -I ' + str(CODE / 'broker.py') + ' forced-command'}
    for key, value in expected.items():
        if values.get(key) != value:
            fail('Effective sshd setting is not the required value: ' + key)
    if values.get('authorizedkeyscommand') not in (None, 'none'):
        fail('An external authorized-keys command could admit unrelated keys.')
    if values.get('trustedusercakeys') not in (None, 'none'):
        fail('A trusted SSH CA could admit unrelated keys.')

def main():
    for tool in ['/usr/bin/git', '/usr/bin/gh', '/usr/bin/docker', '/usr/bin/sudo',
                 '/usr/sbin/sshd', '/usr/sbin/visudo', '/usr/sbin/useradd', '/usr/bin/systemctl']:
        if not os.access(tool, os.X_OK):
            fail('Missing required executable: ' + tool)
    sources = {}
    for name, digest in EXPECTED.items():
        source = STAGE / name
        if source.is_symlink() or not source.is_file():
            fail('Missing or unsafe staged source: ' + name)
        content = source.read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(content).hexdigest() != digest:
            fail('Staged source differs from reviewed bundle: ' + name)
        sources[name] = content
    if PUBKEY.is_symlink() or not PUBKEY.is_file():
        fail('Missing or unsafe sandbox public key.')
    key = PUBKEY.read_text().strip().split()
    if len(key) not in (2, 3) or key[0] != 'ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', key[1]):
        fail('Expected one plain Ed25519 sandbox public key.')
    try:
        raw = base64.b64decode(key[1], validate=True)
        if len(raw) != 51 or raw[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
            fail('Malformed Ed25519 public key.')
    except ValueError:
        fail('Malformed public-key encoding.')
    public_key = key[0] + ' ' + key[1]
    record = (json.dumps({'schema': 1, 'account': ACCOUNT, 'sources': EXPECTED,
                          'public_key': public_key, 'upstream': UPSTREAM},
                         sort_keys=True, indent=2) + '\n').encode()
    existing = MANIFEST.exists() or MANIFEST.is_symlink()
    if existing:
        trusted(BASE, directory=True, exact_mode=0o755)
        file(MANIFEST, record, 0o600)
    else:
        if CHECK:
            fail('Broker installation manifest is absent.')
        try:
            pwd.getpwnam(ACCOUNT)
        except KeyError:
            pass
        else:
            fail('Refusing to adopt an existing sm-git account.')
        try:
            grp.getgrnam(ACCOUNT)
        except KeyError:
            pass
        else:
            fail('Refusing to adopt an existing sm-git group.')
        for path in [BASE, CODE, HELPER, CONF.parent, SUDOERS, SSHD]:
            if path.exists() or path.is_symlink():
                fail('Refusing unowned preexisting installation path: ' + str(path))
        directory(BASE)
        file(MANIFEST, record, 0o600)
    try:
        user = pwd.getpwnam(ACCOUNT)
    except KeyError:
        if CHECK:
            fail('Broker account is absent.')
        run(['/usr/sbin/useradd', '--system', '--user-group', '--no-create-home',
             '--home-dir', str(LOGIN_HOME), '--shell', '/bin/sh', '--password', '!', ACCOUNT])
        user = pwd.getpwnam(ACCOUNT)
    if user.pw_uid == 0 or user.pw_dir != str(LOGIN_HOME) or user.pw_shell != '/bin/sh':
        fail('Broker account does not match the dedicated installation.')
    groups = os.getgrouplist(ACCOUNT, user.pw_gid)
    if groups != [grp.getgrnam(ACCOUNT).gr_gid] or user.pw_gid != grp.getgrnam(ACCOUNT).gr_gid:
        fail('Broker account has unexpected supplementary groups.')
    shadow = run(['/usr/bin/getent', 'shadow', ACCOUNT]).stdout.decode().split(':')
    if len(shadow) < 2 or not shadow[1].startswith(('!', '*')):
        fail('Broker account password is not locked.')
    for path in [Path('/usr/local/libexec'), CODE, CONF.parent, LOGIN_HOME, LOGIN_HOME / '.ssh']:
        directory(path)
    directory(STATE, user.pw_uid, 0o700)
    file(CODE / 'broker.py', sources['broker.py'], 0o755)
    helper = (b'#!/bin/sh\nset -eu\n[ "$#" -eq 0 ] || exit 64\n'
              b'exec /usr/bin/docker exec firstmate gh auth token --user DemandPlanningMC\n')
    file(HELPER, helper, 0o755)
    config = {'repository': str(REPO), 'state_dir': str(STATE),
              'credential_command': ['/usr/bin/sudo', '-n', str(HELPER)]}
    file(CONF, (json.dumps(config, indent=2) + '\n').encode())
    sudo_text = ('# Managed by secondmate-git-broker installer. Empty args are intentional.\n'
                 'sm-git ALL=(root) NOPASSWD: ' + str(HELPER) + ' ""\n').encode()
    file(SUDOERS, sudo_text, 0o440)
    run(['/usr/sbin/visudo', '-cf', str(SUDOERS)])
    forced = '/usr/bin/python3 -I ' + str(CODE / 'broker.py') + ' forced-command'
    key_text = ('restrict,command="' + forced + '" ' + public_key +
                ' secondmate-sandbox-git-only\n').encode()
    file(LOGIN_HOME / '.ssh/authorized_keys', key_text, 0o644)

    new_repository = not REPO.exists()
    if new_repository:
        if CHECK:
            fail('Broker repository is absent.')
        run(['/usr/bin/git', 'init', '--bare', '--template=', '--initial-branch=main', str(REPO)])
        # Only replace git-init's known default config in this newly created repository.
        baseline = b'[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n'
        if (REPO / 'config').read_bytes() != baseline:
            fail('Unexpected git-init configuration; repository left unchanged.')
        (REPO / 'config').write_bytes(repository_config())
        os.chmod(REPO / 'config', 0o644)
        os.chmod(REPO / 'HEAD', 0o644)
        os.chmod(REPO, 0o755)
    trusted(REPO, directory=True, exact_mode=0o755)
    file(REPO / 'config', repository_config())
    file(REPO / 'HEAD', b'ref: refs/heads/main\n')
    for name in ['hooks', 'logs']:
        if not (REPO / name).exists():
            if CHECK:
                fail('Missing repository directory: ' + name)
            (REPO / name).mkdir(mode=0o755)
            os.chmod(REPO / name, 0o755)
    trusted(REPO / 'hooks', directory=True, exact_mode=0o755)
    for name in ['pre-receive', 'post-receive']:
        file(REPO / 'hooks' / name, sources[name], 0o755)
    if set(p.name for p in (REPO / 'hooks').iterdir()) != {'pre-receive', 'post-receive'}:
        fail('Unexpected repository hooks.')
    if (REPO / 'packed-refs').exists():
        trusted(REPO / 'packed-refs')
    for entry in REPO.iterdir():
        if entry.name not in {'objects', 'refs', 'logs'}:
            trusted(entry, directory=entry.is_dir())
    if (REPO / 'objects/info/alternates').exists() or (REPO / 'objects/info/http-alternates').exists():
        fail('Object alternate storage is not allowed.')
    if not new_repository:
        for name in ['objects', 'refs', 'logs']:
            checked_tree(REPO / name, user.pw_uid)
    if not CHECK:
        token_result = run([str(HELPER)])
        token = token_result.stdout.decode().strip()
        if not token or any(c.isspace() for c in token):
            fail('Credential helper did not return one nonempty token.')
        env = dict(ENV)
        settings = [('credential.helper', ''), ('http.proxy', ''), ('http.followRedirects', 'false'),
                    ('http.https://github.com/.extraheader', 'Authorization: Basic ' +
                     base64.b64encode(('x-access-token:' + token).encode()).decode())]
        env['GIT_CONFIG_COUNT'] = str(len(settings))
        for i, (key_name, value) in enumerate(settings):
            env['GIT_CONFIG_KEY_' + str(i)] = key_name
            env['GIT_CONFIG_VALUE_' + str(i)] = value
        git('fetch', '--no-tags', '--no-auto-gc', '--no-write-fetch-head', UPSTREAM,
            'refs/heads/main:refs/heads/main', env=env)
        del token, token_result, settings, env, value
        for name in ['objects', 'refs', 'logs']:
            checked_tree(REPO / name, user.pw_uid, mutate=True)
    for name in ['objects', 'refs', 'logs']:
        checked_tree(REPO / name, user.pw_uid)
    sha = git('rev-parse', '--verify', 'refs/heads/main^{commit}').stdout.decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        fail('Seeded main commit could not be verified.')
    git('fsck', '--full', '--strict')
    ssh_text = ('# Managed by secondmate-git-broker installer.\n'
                'Match User sm-git\n'
                '    AuthenticationMethods publickey\n'
                '    PubkeyAuthentication yes\n'
                '    PasswordAuthentication no\n'
                '    KbdInteractiveAuthentication no\n'
                '    PermitTTY no\n'
                '    DisableForwarding yes\n'
                '    AllowTcpForwarding no\n'
                '    X11Forwarding no\n'
                '    PermitTunnel no\n'
                '    PermitUserRC no\n'
                '    AuthorizedKeysFile ' + str(LOGIN_HOME / '.ssh/authorized_keys') + '\n'
                '    ForceCommand ' + forced + '\n'
                'Match all\n').encode()
    newly_written = not SSHD.exists()
    file(SSHD, ssh_text)
    try:
        ssh_effective()
    except Exception:
        # Remove only this exact just-created file; never reload invalid SSH config.
        if newly_written and not CHECK and SSHD.is_file() and SSHD.read_bytes() == ssh_text:
            SSHD.unlink()
        raise
    if not CHECK:
        run(['/usr/bin/systemctl', 'reload', 'ssh'])
    print('BROKER INSTALLATION: ' + ('checked' if CHECK else 'installed'))
    print('Upstream main SHA: ' + sha)
    print('Expected SSH clone: sm-git@BROKER_HOST:team-sandbox-repo')
    print('Expected initial clone branch: main; expected initial HEAD: ' + sha)
    print('Client SSH clone and forwarding-denial checks remain required with the sandbox key.')

try:
    main()
except Exception as error:
    # Only our fixed/path-only errors are shown; no subprocess output or token is logged.
    if isinstance(error, RuntimeError):
        print('BROKER INSTALLATION REFUSED: ' + str(error), file=sys.stderr)
    else:
        print('BROKER INSTALLATION REFUSED: an internal or filesystem operation failed; no sensitive output disclosed.', file=sys.stderr)
    sys.exit(1)
PY
