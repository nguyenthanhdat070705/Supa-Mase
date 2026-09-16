#!/usr/bin/env python3
"""Offline, narrowly scoped migration from a static seed parent to the real HTTP parent.

This uses the stock remote *filesystem* marker only. It creates no fake Herdr
endpoint, native remote registry, or parent launch metadata.
"""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from common import Refusal, canonical, identifier
from local_ops import atomic, confined, home_path


def bind(home, parent_id, child_id, parent_url, previous_parent_home='/home/nguye/provisioner'):
    home = home_path(home)
    if os.name == 'posix' and os.geteuid() != home.stat().st_uid:
        raise Refusal('Run the offline binding helper as the existing child-home owner.')
    identifier(parent_id); identifier(child_id)
    url = urlsplit(parent_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise Refusal('Parent URL must be a fixed HTTP(S) service origin.')
    identity = confined(home, '.fm-secondmate-home')
    if not identity.is_file() or identity.read_text().strip() != child_id:
        raise Refusal('Child identity marker does not match the configured child.')
    marker = confined(home, '.fm-secondmate-parent')
    previous = marker.read_bytes()
    allowed_previous = ('schema=fm-secondmate-parent.v1\nroute=local\nparent_home=' + previous_parent_home + '\n').encode()
    expected_marker = b'schema=fm-secondmate-parent.v1\nroute=remote\n'
    if previous not in (allowed_previous, expected_marker):
        raise Refusal('Existing parent marker is not the exact expected static or adapter binding.')
    target = confined(home, 'data/parent-control-binding.json', True)
    binding = {'schema': 'parent-control-binding.v1', 'parent_id': parent_id,
               'child_id': child_id, 'parent_url': parent_url.rstrip('/')}
    encoded = canonical(binding) + b'\n'
    if target.exists() and json.loads(target.read_text()) != binding:
        raise Refusal('Refusing to reparent an existing adapter binding.')
    # Take the actual bridge service lock across this two-file publication. The
    # OS file lock is shared across host/container namespaces for the same inode.
    lock_path = confined(home, 'state/secondmate-telegram/service.lock', True)
    import fcntl
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal('Stop the child service before changing its parent binding.') from None
        # A concurrent operator may have completed a bind before we acquired
        # the lock. Revalidate under that lock before publishing either file.
        if identity.read_text().strip() != child_id:
            raise Refusal('Child identity changed before binding.')
        previous = marker.read_bytes()
        if previous not in (allowed_previous, expected_marker):
            raise Refusal('Existing parent marker changed before binding.')
        if target.exists() and json.loads(target.read_text()) != binding:
            raise Refusal('Refusing to reparent an existing adapter binding.')
        if previous != expected_marker:
            backup = confined(home, 'state/parent-control/binding-backups/' + uuid.uuid4().hex + '.json', True)
            atomic(backup, canonical({'previous_marker': previous.decode(), 'new_binding': binding}) + b'\n')
            atomic(marker, expected_marker)
        if not target.exists():
            atomic(target, encoded)
    return {'binding': binding, 'stock_filesystem_route': 'remote',
            'parent_report_path': 'state/parent-replies.status', 'native_remote_backend': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--parent-id', required=True)
    parser.add_argument('--child-id', required=True)
    parser.add_argument('--parent-url', required=True)
    parser.add_argument('--previous-parent-home', default='/home/nguye/provisioner')
    args = parser.parse_args()
    try:
        print(json.dumps(bind(args.home, args.parent_id, args.child_id, args.parent_url, args.previous_parent_home)))
    except Refusal as error:
        raise SystemExit(str(error))
