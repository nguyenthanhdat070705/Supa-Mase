#!/usr/bin/env python3
"""Offline no-follow preparation for the exact child-home bind mount."""

import argparse
import json
import os
from pathlib import PurePosixPath
import re
import stat
import subprocess
import sys


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
DOCKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
DOCKER = "/usr/bin/docker"


def refuse(message):
    raise RuntimeError(message)


def canonical_absolute(value, label):
    if (not isinstance(value, str) or not value.startswith('/') or '\x00' in value
            or any(character == ',' or ord(character) < 32 for character in value)):
        refuse(label + ' must be an absolute POSIX path.')
    path = PurePosixPath(value)
    if path.as_posix() != value or '..' in path.parts or value == '/':
        refuse(label + ' must be a canonical, scoped POSIX path.')
    return path


def container_is_offline(name, allow_absent):
    environment = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
    try:
        result = subprocess.run(
            [DOCKER, 'inspect', '--type', 'container', '--format', '{{json .State}}', name],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=20,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        refuse('Container state is unknown; do not prepare the bind.')
    if result.returncode:
        if allow_absent:
            try:
                listing = subprocess.run(
                    [DOCKER, 'container', 'ls', '--all', '--no-trunc',
                     '--format', '{{.Names}}'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    check=False, timeout=20, env=environment,
                )
            except (OSError, subprocess.TimeoutExpired):
                refuse('Container absence is unknown; do not prepare the bind.')
            if listing.returncode == 0 and len(listing.stdout) <= 1_048_576:
                try:
                    names = listing.stdout.decode('ascii', 'strict').splitlines()
                except UnicodeError:
                    refuse('Container absence listing is invalid; do not prepare the bind.')
                if (len(names) == len(set(names))
                        and all(DOCKER_NAME.fullmatch(item) for item in names)
                        and name not in names):
                    return 'absent'
        refuse('The exact container must be stopped, or explicitly declared absent on first install.')
    try:
        state = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        refuse('Container state is invalid; do not prepare the bind.')
    if (not isinstance(state, dict) or type(state.get('Running')) is not bool
            or state.get('Running') or state.get('Paused') is True or state.get('Restarting') is True
            or state.get('Status') not in ('created', 'exited')):
        refuse('The exact container must be fully stopped before bind preparation.')
    return str(state.get('Status', 'stopped'))


def open_directory(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open('/', flags)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def prepare(deployment_root, child_id, expected_uid, expected_gid):
    root = canonical_absolute(deployment_root, 'Deployment root')
    home_root = root / 'home'
    parent_fd = open_directory(home_root)
    child_fd = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        try:
            child_fd = os.open(child_id, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            refuse('Seed the exact child-home directory before bind preparation.')
        details = os.fstat(child_fd)
        if (not stat.S_ISDIR(details.st_mode) or details.st_uid != expected_uid
                or details.st_gid != expected_gid or stat.S_IMODE(details.st_mode) != 0o700):
            refuse('Exact child-home must be mode 0700 and owned by the configured child UID:GID.')
        os.fsync(child_fd)
        os.fsync(parent_fd)
        return {
            'schema': 'fm-exact-home-bind.v1',
            'source': (home_root / child_id).as_posix(),
            'destination': '/home/nguye/' + child_id,
            'uid': details.st_uid,
            'gid': details.st_gid,
            'mode': format(stat.S_IMODE(details.st_mode), '04o'),
            'device': details.st_dev,
            'inode': details.st_ino,
        }
    finally:
        if child_fd is not None:
            os.close(child_fd)
        os.close(parent_fd)


def require_absent(deployment_root, child_id):
    root = canonical_absolute(deployment_root, 'Deployment root')
    home_root = root / 'home'
    parent_fd = open_directory(home_root)
    try:
        try:
            os.stat(child_id, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.fsync(parent_fd)
            return {
                'schema': 'fm-absent-child-home.v1',
                'source': (home_root / child_id).as_posix(),
                'absent': True,
            }
        refuse('Child-home entry already exists; seed must start from an absent path.')
    finally:
        os.close(parent_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment-root', required=True)
    parser.add_argument('--child-id', required=True)
    parser.add_argument('--container', required=True)
    parser.add_argument('--expected-uid', type=int, default=1000)
    parser.add_argument('--expected-gid', type=int, default=1000)
    parser.add_argument('--allow-absent', action='store_true')
    parser.add_argument('--require-absent', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        refuse('Run bind preparation as root through the reviewed operator channel.')
    if not IDENTIFIER.fullmatch(args.child_id) or not DOCKER_NAME.fullmatch(args.container):
        refuse('Child ID and Docker container name must be fixed valid identifiers.')
    if not 0 <= args.expected_uid <= 2**31 - 1 or not 0 <= args.expected_gid <= 2**31 - 1:
        refuse('Expected UID:GID is invalid.')
    status = container_is_offline(args.container, args.allow_absent)
    if args.require_absent:
        if not args.allow_absent or status != 'absent':
            refuse('--require-absent is only valid for a proven first install with no container.')
        result = require_absent(args.deployment_root, args.child_id)
    else:
        result = prepare(
            args.deployment_root, args.child_id, args.expected_uid, args.expected_gid)
    result['container'] = args.container
    result['container_state'] = status
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as error:
        print('refused: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
