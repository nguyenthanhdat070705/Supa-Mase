#!/usr/bin/env python3
"""Portable, outbound-only advisory inbox. It never installs or launches firstmate."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import time
import uuid

from client import Client
from common import Refusal, canonical, digest, identifier, uuid_id
from escalations import CASE_AUTHORITY, validate_request, validate_response
from local_ops import atomic, confined, home_path


@contextmanager
def receiver_lock(state):
    """OS-held lock survives as an inode but is released on process death."""
    path = confined(state, 'state/parent-control/advisory-receiver.lock', True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refusal('Another advisory receiver owns this state directory.', 409, 'locked') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Receiver:
    def __init__(self, client, state, config, runner=None):
        self.client, self.config = client, config
        self.state = home_path(state)
        self.receiver_id = identifier(config['receiver_id'])
        self.runner = runner or subprocess.run
        database = confined(self.state, 'receiver.sqlite3')
        for suffix in ('', '-journal', '-wal', '-shm'):
            candidate = confined(self.state, 'receiver.sqlite3' + suffix)
            if candidate.exists() and not candidate.is_file():
                raise Refusal('Unsafe receiver journal path.')
        self.db = sqlite3.connect(database)
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS cases (
            child_id TEXT NOT NULL, escalation_id TEXT NOT NULL, record TEXT NOT NULL,
            PRIMARY KEY(child_id,escalation_id))''')
        self.db.commit()

    def path(self, child_id, escalation_id):
        identifier(child_id); uuid_id(escalation_id)
        return confined(self.state, 'cases/' + child_id + '/' + escalation_id + '.json', True)

    def save(self, record):
        # SQLite FULL is the execution authority on Windows and POSIX. The JSON
        # file is a review copy; a lost rename cannot roll back dispatch state.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cases VALUES (?,?,?)',
                            (record['case']['child_id'], record['case']['request']['escalation_id'], canonical(record).decode()))
        atomic(self.path(record['case']['child_id'], record['case']['request']['escalation_id']), canonical(record) + b'\n')

    def load(self, child_id, escalation_id):
        row = self.db.execute('SELECT record FROM cases WHERE child_id=? AND escalation_id=?',
                              (child_id, escalation_id)).fetchone()
        return json.loads(row[0]) if row else None

    def validate_case(self, case):
        if (not isinstance(case, dict) or case.get('schema') != 'escalation-case.v1'
                or case.get('parent_id') != self.client.parent_id or case.get('authority') != CASE_AUTHORITY):
            raise Refusal('Case does not belong to this parent advisory inbox.')
        identifier(case['child_id'])
        validate_request(case['request'])
        return case

    def route(self, case):
        return '/v1/children/' + case['child_id'] + '/escalations/' + case['request']['escalation_id']

    def record(self, case):
        self.validate_case(case)
        path = self.path(case['child_id'], case['request']['escalation_id'])
        record = self.load(case['child_id'], case['request']['escalation_id'])
        if record:
            if record['case']['request'] != case['request'] or record['case']['parent_id'] != case['parent_id']:
                raise Refusal('An existing local case changed identity or content.', 409, 'conflict')
            return record
        if path.exists():
            raise Refusal('A review copy exists without its execution journal; reconcile before dispatch.', 409, 'journal_missing')
        claim_id = str(uuid.uuid4())
        # A claim without this receiver's durable record may already have run.
        # Merely sharing the parent credential does not authorize replay.
        state = 'received' if case['status'] == 'submitted' else 'unknown'
        record = {'schema': 'escalation-receiver-record.v1', 'receiver_id': self.receiver_id,
                  'case': case, 'claim_id': claim_id, 'state': state}
        self.save(record)
        return record

    def dispatch(self, record):
        command = self.config.get('handler_argv')
        if not command:
            return  # Durable receipt is useful before the user installs firstmate.
        if (not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command)
                or not Path(command[0]).is_absolute() or not Path(command[0]).is_file()
                or Path(command[0]).suffix.lower() in ('.bat', '.cmd', '.ps1')):
            raise Refusal('Configure one fixed absolute executable; shell wrappers are not accepted.')
        cwd = home_path(self.config['handler_cwd'])
        timeout = self.config.get('handler_timeout_seconds', 60)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise Refusal('Handler timeout must be 1-3600 seconds.')
        case, claim_id = record['case'], record['claim_id']
        route = self.route(case)
        if record['state'] not in ('received', 'claimed'):
            return
        current = self.client.call('GET', route)['case']
        self.validate_case(current)
        if (current['request'] != case['request'] or current['status'] not in ('submitted', 'claimed')
                or (current['claim_id'] is not None and current['claim_id'] != claim_id)):
            record['state'] = 'closed'
            self.save(record)
            return
        if record['state'] == 'received':
            reply = self.client.call('POST', route + '/claim',
                                     {'claim_id': claim_id, 'request_sha256': case['request']['sha256']})
            verified = self.validate_case(reply.get('case'))
            if verified['request'] != case['request'] or verified['claim_id'] != claim_id:
                raise Refusal('Host claim receipt changed the case or dispatch identity.')
            record['state'] = 'claimed'
            self.save(record)
        if record['state'] != 'claimed':
            return
        current = self.client.call('GET', route)['case']
        self.validate_case(current)
        if (current['request'] != case['request'] or current['claim_id'] != claim_id
                or current['status'] != 'claimed'):
            record['state'] = 'closed'
            self.save(record)
            return
        envelope = {'schema': 'firstmate-advisory-dispatch.v1', 'dispatch_id': claim_id,
                    'receiver_id': self.receiver_id, 'case': current,
                    'authority': dict(CASE_AUTHORITY)}
        record['state'] = 'dispatching'
        self.save(record)  # Durable before any external handler effect.
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        try:
            # Output may contain private diagnostics. Bound the read and never
            # disclose stderr or malformed stdout. Case content never builds argv.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                result = self.runner(command, input=canonical(envelope), cwd=str(cwd),
                                     stdout=output, stderr=errors, shell=False, timeout=timeout,
                                     creationflags=flags, check=False)
                output.seek(0)
                raw = output.read(240_001)
            if result.returncode or len(raw) > 240_000:
                raise ValueError('No bounded successful handler receipt')
            receipt = json.loads(raw)
            if (not isinstance(receipt, dict) or set(receipt) - {'schema', 'dispatch_id', 'accepted', 'response'}
                    or receipt.get('schema') != 'firstmate-advisory-receipt.v1'
                    or receipt.get('dispatch_id') != claim_id or receipt.get('accepted') is not True):
                raise ValueError('Handler receipt mismatch')
            if 'response' in receipt:
                response = validate_response(receipt['response'])
                if (response['escalation_id'] != case['request']['escalation_id']
                        or response['request_sha256'] != case['request']['sha256'] or response['claim_id'] != claim_id):
                    raise ValueError('Handler response identity mismatch')
                record['response'] = response
            record['state'] = 'dispatched'
            self.save(record)
        except (OSError, subprocess.TimeoutExpired, ValueError, Refusal):
            record['state'] = 'unknown'
            self.save(record)
            raise Refusal('Handler completion is unknown; inspect its dispatch ID before any recovery.', 409, 'dispatch_unknown') from None

    def send_reply(self, record):
        if not record.get('response') or record['state'] in ('replied', 'closed'):
            return
        response = validate_response(record['response'])
        remote = self.client.call('GET', self.route(record['case']))
        current = self.validate_case(remote['case'])
        if current['request'] != record['case']['request'] or current['claim_id'] != record['claim_id']:
            record['state'] = 'closed'
            record['last_error'] = 'remote_identity_changed'
            self.save(record)
            return
        if current['status'] == 'cancelled':
            record['state'] = 'closed'
            self.save(record)
            return
        if current['status'] == 'answered':
            record['state'] = 'replied' if remote.get('result', {}).get('response') == response else 'closed'
            self.save(record)
            return
        result = self.client.call('POST', self.route(record['case']) + '/reply', response)
        accepted = result.get('result', {})
        if result.get('stored') is not True or accepted.get('response') != response or accepted.get('status') != 'answered':
            raise Refusal('Host reply receipt does not match the immutable response.')
        record['state'] = 'replied'
        self.save(record)

    def receive_once(self):
        summary = {'received': 0, 'dispatched': 0, 'replied': 0, 'attention': 0}
        with receiver_lock(self.state):
            processed = set()
            batch = self.client.call('GET', '/v1/escalations')
            if not isinstance(batch.get('cases'), list) or len(batch['cases']) > 20:
                raise Refusal('Invalid advisory inbox batch.')
            for case in batch['cases']:
                record = self.record(case)
                processed.add((case['child_id'], case['request']['escalation_id']))
                summary['received'] += 1
                if record['state'] == 'dispatching':
                    record['state'] = 'unknown'
                    self.save(record)
                try:
                    self.dispatch(record)
                    self.send_reply(record)
                except Refusal as error:
                    summary['attention'] += 1
                    record['last_error'] = error.code
                    self.save(record)
                if record['state'] == 'dispatched':
                    summary['dispatched'] += 1
                if record['state'] == 'replied':
                    summary['replied'] += 1
                if record['state'] == 'unknown':
                    summary['attention'] += 1
            # A reply may already be stored remotely when its receipt was lost;
            # answered cases leave the open list, so retry local reply records too.
            for row in self.db.execute('SELECT record FROM cases').fetchall():
                record = json.loads(row[0])
                key = (record['case']['child_id'], record['case']['request']['escalation_id'])
                if key not in processed and record['state'] in ('received', 'claimed'):
                    try:
                        self.dispatch(record)
                    except Refusal:
                        summary['attention'] += 1
                if record.get('response') and record['state'] == 'dispatched':
                    try:
                        self.send_reply(record)
                    except Refusal:
                        summary['attention'] += 1
        return summary

    def reply(self, child_id, escalation_id, draft):
        with receiver_lock(self.state):
            identifier(child_id); uuid_id(escalation_id)
            record = self.load(child_id, escalation_id)
            if record is None:
                raise Refusal('Reply requires the original durable local claim record.')
            if record['state'] not in ('dispatched', 'unknown', 'replied'):
                raise Refusal('Only a previously dispatched or explicitly reconciled case may reply.')
            if not isinstance(draft, dict) or set(draft) != {'body', 'evidence'}:
                raise Refusal('Reply draft contains body and evidence only.')
            response = {'schema': 'escalation-response.v1',
                        'response_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'advisory-reply:' + record['claim_id'])),
                        'escalation_id': escalation_id, 'request_sha256': record['case']['request']['sha256'],
                        'claim_id': record['claim_id'], **draft}
            response['sha256'] = digest(response)
            validate_response(response)
            if record.get('response') and record['response'] != response:
                raise Refusal('Stored advisory response is immutable.', 409, 'conflict')
            record['response'] = response
            if record['state'] != 'replied':
                record['state'] = 'dispatched'
            self.save(record)
            self.send_reply(record)
            return {'response_id': response['response_id'], 'sha256': response['sha256'], 'state': record['state']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True, help='Existing private adapter state directory; no firstmate installation required')
    sub = parser.add_subparsers(dest='command', required=True)
    receive = sub.add_parser('receive'); receive.add_argument('--once', action='store_true')
    receive.add_argument('--interval', type=int, default=15)
    reply = sub.add_parser('reply'); reply.add_argument('child'); reply.add_argument('escalation_id'); reply.add_argument('--file', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config_path = Path(args.config)
    if not config_path.is_absolute() or config_path.is_symlink() or not config_path.is_file():
        raise Refusal('Use an existing trusted absolute receiver configuration path.')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    receiver = Receiver(Client(config), args.state, config)
    if args.command == 'reply':
        print(json.dumps(receiver.reply(args.child, args.escalation_id, json.loads(Path(args.file).read_text(encoding='utf-8')))))
        return
    if not 2 <= args.interval <= 3600:
        raise Refusal('Receiver interval must be 2-3600 seconds.')
    while True:
        try:
            result = receiver.receive_once()
            print(json.dumps(result), flush=True)
        except (Refusal, OSError, ValueError, sqlite3.Error) as error:
            if args.once:
                raise
            print(json.dumps({'state': 'waiting', 'reason': error.code if isinstance(error, Refusal) else 'local_or_transport_unavailable'}), flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError, sqlite3.Error) as error:
        sys.exit('Advisory receiver refused: ' + (error.code if isinstance(error, Refusal) else 'local_or_transport_unavailable'))
