#!/usr/bin/env python3
"""Trusted host control adapter. No primary-home mount, arbitrary exec, or auto import."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from common import (DESTINATIONS, KINDS, MAX_BODY, Refusal, canonical, digest,
                    identifier, text, uuid_id, validate_brain, validate_proposal)
from lifecycle import Lifecycle


def protected_path(path, *, secret=False, directory=False, allow_missing=False):
    path = Path(path)
    if not path.is_absolute():
        raise Refusal('Operator files must have absolute paths.')
    for part in [path, *path.parents]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if part == path and allow_missing:
                continue
            raise Refusal('Operator path is unavailable.') from None
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refusal('Operator file or parent has unsafe ownership or permissions.')
        if part != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise Refusal('Operator parent must be a protected directory.')
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refusal('Operator file must be a protected regular file.')
        if part == path and secret and info.st_mode & 0o077:
            raise Refusal('Operator private path must not be accessible by other users.')
    return path


def private_file(path, secret=False):
    path = protected_path(path, secret=secret)
    return path.read_bytes()


def validate_database(path):
    database = protected_path(path, secret=True, allow_missing=True)
    protected_path(database.parent, directory=True, secret=True)
    # SQLite will open these adjacent files when recovering WAL state.
    for suffix in ('-wal', '-shm', '-journal'):
        protected_path(str(database) + suffix, secret=True, allow_missing=True)
    protected_path(database.parent / 'service.lock', secret=True, allow_missing=True)
    return database


def load_config(path):
    config = json.loads(private_file(path, secret=True))
    tokens = {}
    for role, section in [('parent', 'parents'), ('child', 'children'), ('operator', 'operators')]:
        for name, entry in config.get(section, {}).items():
            identifier(name)
            token = private_file(entry['token_file'], secret=True).decode().strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{40,200}', token) or token in tokens:
                raise Refusal('Scoped tokens must be unique and contain at least 40 URL-safe characters.')
            tokens[token] = {'role': role, 'id': name}
    return config, tokens


class DockerExecutor:
    def __init__(self, docker='/usr/bin/docker', timeout=30):
        self.docker, self.timeout = docker, timeout

    def __call__(self, child, operation, payload=None):
        suffix = {'status': ['status'], 'enqueue': ['enqueue-parent', '--request-file', '/dev/stdin']}.get(operation)
        if suffix is None:
            raise Refusal('Unsupported runtime operation.')
        argv = [self.docker, 'exec', '-i', '--user', child.get('exec_user', '1000:1000'),
                '--env', 'FM_HOME=' + child['home'], child['container'],
                '/usr/bin/python3', '/opt/secondmate/bridge.py', *suffix]
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
        try:
            result = subprocess.run(argv, input=canonical(payload) if payload is not None else b'',
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal('Runtime completion is unknown; reconcile the same request ID.', 503, 'runtime_unknown') from None
        if result.returncode or len(result.stdout) > MAX_BODY:
            raise Refusal('Runtime request failed; no subprocess output was disclosed.', 503, 'runtime_unknown')
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise Refusal('Runtime returned an invalid response.', 503, 'runtime_unknown') from None
        if not isinstance(value, dict):
            raise Refusal('Runtime returned an invalid response.', 503, 'runtime_unknown')
        return value


class Application:
    def __init__(self, config, tokens, executor=None, data_service=None, lifecycle=None, recover_interrupted=False):
        if config.get('version') != 1:
            raise Refusal('Unsupported operator configuration version.')
        self.config, self.tokens = config, tokens
        self.parents = config.get('parents', {})
        self.children = config.get('children', {})
        self.operators = config.get('operators', {})
        containers = set()
        for child_id, child in self.children.items():
            identifier(child_id)
            if child.get('parent_id') not in self.parents:
                raise Refusal('Every child must have a configured real parent.')
            identifier(child.get('container'))
            if child['container'] in containers:
                raise Refusal('Each child must own a distinct fixed container.')
            containers.add(child['container'])
            home = child.get('home', '')
            if not isinstance(home, str) or not home.startswith('/') or '..' in home.split('/') or '\x00' in home:
                raise Refusal('Child home must be a fixed absolute path.')
            if not re.fullmatch(r'[0-9]+:[0-9]+', child.get('exec_user', '1000:1000')):
                raise Refusal('Child execution identity must be a fixed UID:GID.')
        self.executor = executor or DockerExecutor()
        self.lifecycle = lifecycle or Lifecycle()
        self.child_locks = {child_id: threading.RLock() for child_id in self.children}
        self.data_service = data_service
        self.lock = threading.RLock()
        self.db = sqlite3.connect(config['database'], check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, parent_id TEXT NOT NULL,
            digest TEXT NOT NULL, envelope TEXT NOT NULL, delivery TEXT NOT NULL, result TEXT);
          CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            child_id TEXT NOT NULL, parent_id TEXT NOT NULL, digest TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS proposals (
            child_id TEXT NOT NULL, proposal_id TEXT NOT NULL, version INTEGER NOT NULL,
            digest TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(child_id,proposal_id,version));
          CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, parent_id TEXT NOT NULL,
            digest TEXT NOT NULL, payload TEXT NOT NULL, receipt TEXT);
          CREATE TABLE IF NOT EXISTS brains (
            child_id TEXT NOT NULL, revision TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(child_id,revision));
          CREATE TABLE IF NOT EXISTS controls (
            operation_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, digest TEXT NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT);
        ''')
        if recover_interrupted:
            # After a proven exclusive service restart, interrupted attempts are
            # unknown. Runtime enqueue dedupes an explicit same-ID retry.
            with self.db:
                self.db.execute("UPDATE requests SET delivery='unknown' WHERE delivery='delivering'")
                self.db.execute("UPDATE controls SET state='unknown' WHERE state='executing'")

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith('Bearer '):
            raise Refusal('Authentication required.', 401, 'unauthorized')
        candidate = authorization[7:]
        for token, principal in self.tokens.items():
            if hmac.compare_digest(token, candidate):
                return principal
        raise Refusal('Authentication required.', 401, 'unauthorized')

    def child_scope(self, principal, child_id, role=None):
        identifier(child_id)
        child = self.children.get(child_id)
        if child is None:
            raise Refusal('Child scope is unavailable.', 403, 'forbidden')
        if role and principal['role'] != role:
            raise Refusal('This operation requires a different authority.', 403, 'forbidden')
        allowed = ((principal['role'] == 'child' and principal['id'] == child_id)
                   or (principal['role'] == 'parent' and principal['id'] == child['parent_id'])
                   or (principal['role'] == 'operator' and child_id in self.operators[principal['id']].get('children', [])))
        if not allowed:
            raise Refusal('Child scope is unavailable.', 403, 'forbidden')
        return child

    def event(self, child_id, payload):
        event_id = uuid_id(payload.get('event_id'))
        fingerprint = digest(payload)
        old = self.db.execute('SELECT digest,sequence FROM events WHERE event_id=?', (event_id,)).fetchone()
        if old:
            if old['digest'] != fingerprint:
                raise Refusal('Event ID already refers to different content.', 409, 'conflict')
            return old['sequence']
        cursor = self.db.execute('INSERT INTO events(event_id,child_id,parent_id,digest,payload,created) VALUES (?,?,?,?,?,?)',
                                 (event_id, child_id, self.children[child_id]['parent_id'], fingerprint,
                                  canonical(payload).decode(), time.time()))
        return cursor.lastrowid

    def send(self, principal, child_id, payload, retry=False):
        self.child_scope(principal, child_id, 'parent')
        with self.child_locks[child_id]:
            return self._send(principal, child_id, payload, retry)

    def _send(self, principal, child_id, payload, retry=False):
        child = self.child_scope(principal, child_id, 'parent')
        if set(payload) != {'request_id', 'correlation', 'body', 'scope'}:
            raise Refusal('Invalid parent request fields.')
        request_id = uuid_id(payload['request_id'])
        if not isinstance(payload['correlation'], str) or not re.fullmatch(r'[0-9a-f]{16}', payload['correlation']):
            raise Refusal('Invalid request correlation.')
        text(payload['body'])
        if not isinstance(payload['scope'], dict) or len(canonical(payload['scope'])) > 8000:
            raise Refusal('Invalid request scope.')
        envelope = {'schema': 'parent-request.v1', **payload, 'child_id': child_id,
                    'authority': {'role': 'parent', 'approval': False}}
        fingerprint = digest(envelope)
        with self.lock, self.db:
            old = self.db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if old and (old['digest'] != fingerprint or old['parent_id'] != principal['id']):
                raise Refusal('Request ID already refers to different content or owner.', 409, 'conflict')
            if old and not retry:
                return {'request_id': request_id, 'correlation': payload['correlation'], 'delivery': old['delivery'], 'duplicate': True}
            if old and old['delivery'] == 'delivered':
                return {'request_id': request_id, 'correlation': payload['correlation'], 'delivery': 'delivered', 'duplicate': True}
            if old and old['delivery'] == 'delivering':
                raise Refusal('This request is being delivered; inspect before retry.', 409, 'delivery_pending')
            if old:
                self.db.execute('UPDATE requests SET delivery=? WHERE request_id=?', ('delivering', request_id))
            else:
                self.db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,NULL)',
                                (request_id, child_id, principal['id'], fingerprint, canonical(envelope).decode(), 'delivering'))
        try:
            result = self.executor(child, 'enqueue', envelope)
            if result.get('request_id') != request_id or result.get('status') not in ('queued', 'duplicate'):
                raise Refusal('Runtime delivery acknowledgement is invalid.', 503, 'runtime_unknown')
        except Refusal:
            with self.lock, self.db:
                self.db.execute('UPDATE requests SET delivery=? WHERE request_id=?', ('unknown', request_id))
            raise
        with self.lock, self.db:
            self.db.execute('UPDATE requests SET delivery=?,result=? WHERE request_id=?',
                            ('delivered', canonical(result).decode(), request_id))
        return {'request_id': request_id, 'correlation': payload['correlation'], 'delivery': 'delivered', 'result': result}

    def report(self, principal, child_id, payload):
        self.child_scope(principal, child_id, 'child')
        if not isinstance(payload, dict) or set(payload) - {'event_id', 'request_id', 'correlation', 'kind', 'text', 'artifact'}:
            raise Refusal('Invalid report fields.')
        uuid_id(payload.get('event_id'))
        if payload.get('kind') not in KINDS:
            raise Refusal('Invalid outcome kind.')
        text(payload.get('text'), 100_000)
        if 'artifact' in payload and len(canonical(payload['artifact'])) > 16_000:
            raise Refusal('Report artifact metadata is oversized.')
        if ('request_id' in payload) != ('correlation' in payload):
            raise Refusal('Request ID and correlation must be supplied together.')
        with self.lock, self.db:
            if 'request_id' in payload:
                uuid_id(payload['request_id'])
                request = self.db.execute('SELECT child_id,envelope FROM requests WHERE request_id=?', (payload['request_id'],)).fetchone()
                if not request or request['child_id'] != child_id or json.loads(request['envelope'])['correlation'] != payload['correlation']:
                    raise Refusal('Report does not match this child request.', 403, 'forbidden')
            # Child identity is assigned by authenticated scope, never by submitted text.
            result = {**payload, 'child_id': child_id}
            sequence = self.event(child_id, result)
        return {'event_id': payload['event_id'], 'sequence': sequence, 'stored': True}

    def propose(self, principal, child_id, payload):
        self.child_scope(principal, child_id, 'child')
        validate_proposal(payload)
        key = (child_id, payload['proposal_id'], payload['version'])
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'knowledge:' + ':'.join(map(str, key)) + ':' + payload['sha256']))
        with self.lock, self.db:
            old = self.db.execute('SELECT digest FROM proposals WHERE child_id=? AND proposal_id=? AND version=?', key).fetchone()
            if old and old['digest'] != payload['sha256']:
                raise Refusal('Proposal version is immutable; submit a new version.', 409, 'conflict')
            self.db.execute('INSERT OR IGNORE INTO proposals VALUES (?,?,?,?,?)', (*key, payload['sha256'], canonical(payload).decode()))
            self.event(child_id, {'event_id': event_id, 'child_id': child_id, 'kind': 'knowledge-proposal',
                                 'text': 'Knowledge proposal requires Mac review; receipt is not approval or import.',
                                 'proposal_id': payload['proposal_id'], 'version': payload['version'],
                                 'sha256': payload['sha256'], 'requires_mac_notification': True})
        return {'proposal_id': payload['proposal_id'], 'version': payload['version'], 'sha256': payload['sha256'],
                'event_id': event_id, 'approval_required': True}

    def approve(self, principal, child_id, payload):
        child = self.child_scope(principal, child_id)
        if principal['role'] != 'operator':
            raise Refusal('Only a separately authenticated operator can record verified captain approvals.', 403, 'forbidden')
        expected = {'approval_id', 'proposal_id', 'version', 'sha256', 'selected_claims', 'destination', 'approval_reference'}
        if set(payload) != expected:
            raise Refusal('Approval requires an exact proposal, claims, destination and explicit reference.')
        uuid_id(payload['approval_id']); uuid_id(payload['proposal_id'])
        if type(payload['version']) is not int or not 1 <= payload['version'] <= 1_000_000:
            raise Refusal('Approval requires an exact proposal version.')
        if payload['destination'] not in DESTINATIONS:
            raise Refusal('Knowledge destination is not allowlisted.')
        reference = payload['approval_reference']
        if not isinstance(reference, dict) or set(reference) != {'captain_user_id', 'source', 'reference', 'quoted_approval'}:
            raise Refusal('Explicit captain approval provenance is required.')
        if type(reference['captain_user_id']) is not int or reference['captain_user_id'] != self.config['captain_user_id']:
            raise Refusal('Approval must name the configured captain.', 403, 'forbidden')
        for name in ['source', 'reference', 'quoted_approval']:
            text(reference[name], 6000)
        with self.lock, self.db:
            row = self.db.execute('SELECT payload FROM proposals WHERE child_id=? AND proposal_id=? AND version=?',
                                  (child_id, payload['proposal_id'], payload['version'])).fetchone()
            if not row:
                raise Refusal('Proposal version is unavailable.', 404, 'not_found')
            proposal = json.loads(row['payload'])
            if payload['sha256'] != proposal['sha256']:
                raise Refusal('Approval hash does not match this proposal version.', 409, 'conflict')
            selected = payload['selected_claims']
            ids = {x['id'] for x in proposal['manifest']['claims']}
            if not isinstance(selected, list) or not selected or not all(isinstance(x, str) and x in ids for x in selected) or len(selected) != len(set(selected)):
                raise Refusal('Select exact, distinct claim IDs.')
            approval = {'schema': 'knowledge-approval.v1', **payload, 'child_id': child_id,
                        'parent_id': child['parent_id'], 'recorded_by': principal['id'], 'proposal': proposal}
            fingerprint = digest(approval)
            old = self.db.execute('SELECT digest FROM approvals WHERE approval_id=?', (payload['approval_id'],)).fetchone()
            if old and old['digest'] != fingerprint:
                raise Refusal('Approval ID already refers to different content.', 409, 'conflict')
            self.db.execute('INSERT OR IGNORE INTO approvals VALUES (?,?,?,?,?,NULL)',
                            (payload['approval_id'], child_id, child['parent_id'], fingerprint, canonical(approval).decode()))
        return {'approval': approval, 'applied': False}

    def control(self, principal, child_id, payload):
        child = self.child_scope(principal, child_id)
        if principal['role'] not in ('parent', 'operator'):
            raise Refusal('Child cannot control the parent or its own host lifecycle.', 403, 'forbidden')
        if set(payload) - {'operation_id', 'action', 'expected_generation', 'operator_recovery_reference'} or not {'operation_id', 'action', 'expected_generation'} <= set(payload):
            raise Refusal('Lifecycle requires operation ID, action, and exact expected generation.')
        operation_id = uuid_id(payload['operation_id'])
        text(payload['expected_generation'], 200)
        recovery = 'operator_recovery_reference' in payload
        if recovery:
            if principal['role'] != 'operator' or self.operators[principal['id']].get('allow_recovery') is not True:
                raise Refusal('Exceptional recovery requires a separately scoped operator.', 403, 'forbidden')
            text(payload['operator_recovery_reference'], 6000)
        stored = {**payload, 'principal': principal, 'child_id': child_id}
        fingerprint = digest(stored)
        with self.child_locks[child_id]:
            with self.lock, self.db:
                old = self.db.execute('SELECT * FROM controls WHERE operation_id=?', (operation_id,)).fetchone()
                if old:
                    if old['digest'] != fingerprint:
                        raise Refusal('Lifecycle operation ID conflicts with an existing operation.', 409, 'conflict')
                    return {'operation_id': operation_id, 'state': old['state'],
                            'result': json.loads(old['result']) if old['result'] else None, 'duplicate': True}
                self.db.execute('INSERT INTO controls VALUES (?,?,?,?,?,NULL)',
                                (operation_id, child_id, fingerprint, canonical(stored).decode(), 'executing'))
            try:
                result = self.lifecycle.execute(child, payload['action'], payload['expected_generation'], recovery)
            except Refusal as error:
                with self.lock, self.db:
                    self.db.execute('UPDATE controls SET state=?,result=? WHERE operation_id=?',
                                    ('unknown' if error.status >= 500 else 'refused', canonical({'error': error.code}).decode(), operation_id))
                raise
            with self.lock, self.db:
                self.db.execute('UPDATE controls SET state=?,result=? WHERE operation_id=?', ('complete', canonical(result).decode(), operation_id))
            return {'operation_id': operation_id, 'state': 'complete', 'result': result}

    def request_inventory(self, child_id):
        with self.lock:
            rows = self.db.execute('SELECT request_id,delivery,envelope FROM requests WHERE child_id=? ORDER BY rowid DESC LIMIT 100', (child_id,)).fetchall()
            events = self.db.execute('SELECT payload FROM events WHERE child_id=? ORDER BY sequence DESC LIMIT 1000', (child_id,)).fetchall()
        outcomes = {}
        for item in events:
            event = json.loads(item['payload'])
            if event.get('request_id') and event['request_id'] not in outcomes:
                outcomes[event['request_id']] = {'kind': event['kind'], 'event_id': event['event_id']}
        return [{'request_id': row['request_id'], 'correlation': json.loads(row['envelope'])['correlation'],
                 'delivery': row['delivery'], 'latest_report': outcomes.get(row['request_id'])} for row in rows]

    def handle(self, method, target, principal, payload=None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise Refusal('Expected a JSON object.')
        parsed = urlsplit(target)
        path, query = parsed.path, parse_qs(parsed.query)
        if method == 'GET' and path == '/v1/children':
            if principal['role'] != 'parent':
                raise Refusal('Only a parent lists its children.', 403, 'forbidden')
            return {'adapter': 'explicit-container-parent.v1', 'children': [
                {'child_id': cid, 'parent_id': child['parent_id'], 'scope': child.get('scope', {}), 'backend': 'tmux'}
                for cid, child in self.children.items() if child['parent_id'] == principal['id']]}
        if method == 'GET' and path == '/v1/reports':
            if principal['role'] != 'parent':
                raise Refusal('Only a parent pulls its report inbox.', 403, 'forbidden')
            try:
                after = int(query.get('after', ['0'])[0])
            except ValueError:
                raise Refusal('Invalid report cursor.') from None
            if after < 0:
                raise Refusal('Invalid report cursor.')
            with self.lock:
                rows = self.db.execute('SELECT sequence,payload FROM events WHERE parent_id=? AND sequence>? ORDER BY sequence LIMIT 20', (principal['id'], after)).fetchall()
            return {'events': [{'sequence': row['sequence'], **json.loads(row['payload'])} for row in rows],
                    'next_cursor': rows[-1]['sequence'] if rows else after, 'automatic_import': False}
        match = re.fullmatch(r'/v1/approvals/([0-9a-f-]{36})(/receipt)?', path)
        if match:
            if principal['role'] != 'parent':
                raise Refusal('Only the owning parent can read or acknowledge promotion.', 403, 'forbidden')
            with self.lock, self.db:
                row = self.db.execute('SELECT * FROM approvals WHERE approval_id=? AND parent_id=?', (uuid_id(match[1]), principal['id'])).fetchone()
                if not row:
                    raise Refusal('Approval is unavailable.', 404, 'not_found')
                if method == 'GET' and not match[2]:
                    return {'approval': json.loads(row['payload']), 'receipt': json.loads(row['receipt']) if row['receipt'] else None}
                if method == 'POST' and match[2]:
                    approval = json.loads(row['payload'])
                    if set(payload) != {'approval_id', 'sha256', 'destination', 'result_sha256'} or any(payload[k] != approval[k] for k in ['approval_id', 'sha256', 'destination']) or not re.fullmatch(r'[0-9a-f]{64}', payload['result_sha256']):
                        raise Refusal('Promotion receipt does not match approval.')
                    encoded = canonical(payload).decode()
                    if row['receipt'] and row['receipt'] != encoded:
                        raise Refusal('Promotion already has a different receipt.', 409, 'conflict')
                    self.db.execute('UPDATE approvals SET receipt=? WHERE approval_id=?', (encoded, match[1]))
                    return {'recorded': True, 'receipt': payload}
        if method == 'POST' and path in ('/v1/data/query', '/v1/data/schema'):
            child_id = principal['id'] if principal['role'] == 'child' else payload.get('child_id')
            self.child_scope(principal, child_id)
            if principal['role'] not in ('child', 'parent') or (principal['role'] == 'child' and 'child_id' in payload):
                raise Refusal('Data scope must come from authentication.', 403, 'forbidden')
            if self.data_service is None:
                raise Refusal('Read-only data access is not configured.', 503, 'not_configured')
            request = {k: v for k, v in payload.items() if k != 'child_id'}
            try:
                return (self.data_service.execute if path.endswith('/query') else self.data_service.schema)(child_id, request)
            except Exception as error:
                code = getattr(error, 'code', 'data_refused')
                status = getattr(error, 'status', 400)
                raise Refusal('Read-only data request was refused.', status if type(status) is int and 400 <= status <= 599 else 400,
                              code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]+', code) else 'data_refused') from None
        match = re.fullmatch(r'/v1/children/([A-Za-z0-9][A-Za-z0-9._-]{0,79})/(status|control|requests|requests/retry|reports|proposals|approvals|brain)', path)
        if not match:
            raise Refusal('Unknown control endpoint.', 404, 'not_found')
        child_id, action = match.groups()
        child = self.child_scope(principal, child_id)
        if method == 'GET' and action == 'status':
            self.child_scope(principal, child_id, 'parent')
            try:
                status = self.executor(child, 'status')
                try:
                    control = self.lifecycle.status(child)
                except Refusal:
                    control = {'reachability': 'unknown'}
                return {'child_id': child_id, 'parent_id': child['parent_id'], 'reachability': 'reachable', 'runtime': status, 'control': control, 'requests': self.request_inventory(child_id)}
            except Refusal:
                try:
                    control = self.lifecycle.status(child)
                except Refusal:
                    control = {'reachability': 'unknown'}
                return {'child_id': child_id, 'parent_id': child['parent_id'], 'reachability': 'unknown', 'automatic_relaunch': False, 'control': control, 'requests': self.request_inventory(child_id)}
        if method == 'POST' and action == 'control':
            return self.control(principal, child_id, payload)
        if method == 'POST' and action in ('requests', 'requests/retry'):
            return self.send(principal, child_id, payload, retry=action.endswith('/retry'))
        if method == 'POST' and action == 'reports':
            return self.report(principal, child_id, payload)
        if method == 'POST' and action == 'proposals':
            return self.propose(principal, child_id, payload)
        if method == 'GET' and action == 'proposals':
            self.child_scope(principal, child_id, 'parent')
            with self.lock:
                if 'proposal_id' in query and 'version' in query:
                    proposal_id = uuid_id(query['proposal_id'][0])
                    try:
                        version = int(query['version'][0])
                    except ValueError:
                        raise Refusal('Invalid proposal version.') from None
                    row = self.db.execute('SELECT payload FROM proposals WHERE child_id=? AND proposal_id=? AND version=?', (child_id, proposal_id, version)).fetchone()
                    if not row:
                        raise Refusal('Proposal version is unavailable.', 404, 'not_found')
                    return {'proposal': json.loads(row['payload']), 'automatic_import': False}
                rows = self.db.execute('SELECT payload FROM proposals WHERE child_id=? ORDER BY rowid DESC LIMIT 100', (child_id,)).fetchall()
            items = [json.loads(row['payload']) for row in rows]
            return {'proposals': [{k: item[k] for k in ('proposal_id', 'version', 'sha256')} for item in items], 'automatic_import': False}
        if method == 'POST' and action == 'approvals':
            return self.approve(principal, child_id, payload)
        if action == 'brain':
            if method == 'POST':
                self.child_scope(principal, child_id, 'parent')
                validate_brain(payload)
                with self.lock, self.db:
                    self.db.execute('INSERT OR IGNORE INTO brains VALUES (?,?,?,?)', (child_id, payload['revision'], canonical(payload).decode(), time.time()))
                return {'revision': payload['revision'], 'published': True}
            if method == 'GET':
                with self.lock:
                    row = self.db.execute('SELECT payload FROM brains WHERE child_id=? ORDER BY created DESC,rowid DESC LIMIT 1', (child_id,)).fetchone()
                if not row:
                    raise Refusal('No parent brain snapshot is published.', 404, 'not_found')
                return json.loads(row['payload'])
        raise Refusal('Method is unavailable for this endpoint.', 405, 'method_not_allowed')


class Handler(BaseHTTPRequestHandler):
    server_version = 'FirstmateControl/1'
    def log_message(self, format, *args):
        pass  # No URL, request body, auth header, or subprocess logs.

    def respond(self, status, value):
        data = canonical(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        try:
            self.connection.settimeout(40)
            principal = self.server.application.authenticate(self.headers.get('Authorization'))
            payload = {}
            if self.command == 'POST':
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise Refusal('Expected bounded JSON with Content-Length.')
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= MAX_BODY:
                    raise Refusal('Invalid request body length.', 413, 'too_large')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise Refusal('Incomplete JSON body.')
                payload = json.loads(raw)
            self.respond(200, self.server.application.handle(self.command, self.path, principal, payload))
        except Refusal as error:
            self.respond(error.status, {'error': error.code, 'message': str(error)})
        except (ValueError, UnicodeError):
            self.respond(400, {'error': 'invalid_json', 'message': 'Invalid JSON or request framing.'})
        except Exception:
            self.respond(500, {'error': 'internal_error', 'message': 'Control operation failed; sensitive diagnostics were withheld.'})

    do_GET = dispatch
    do_POST = dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    config, tokens = load_config(args.config)
    database = validate_database(config['database'])
    data_service = None
    if config.get('data_access_config_file'):
        module_path = Path(__file__).resolve().parent.parent / 'data-access/data_service.py'
        private_file(module_path)
        spec = importlib.util.spec_from_file_location('firstmate_data_service', module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        data_service = module.ReadOnlyService(json.loads(private_file(config['data_access_config_file'], secret=True)))
    service_lock = None
    if not args.check:
        import fcntl
        lock_path = database.parent / 'service.lock'
        if lock_path.is_symlink():
            raise Refusal('Unsafe service lock.')
        service_lock = lock_path.open('a+')
        try:
            fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal('Another control service owns this state directory.') from None
    application = Application(config, tokens, data_service=data_service, recover_interrupted=not args.check)
    host, port = config.get('listen_host', '127.0.0.1'), config.get('listen_port', 8787)
    if host not in ('127.0.0.1', '::1') and not config.get('tls_cert_file') and config.get('allow_private_http') is not True:
        raise Refusal('Non-loopback HTTP requires TLS or explicit private-network configuration.')
    if args.check:
        print(json.dumps({'configuration': 'valid', 'adapter': 'explicit-container-parent.v1',
                          'children': list(application.children), 'data_access': bool(data_service),
                          'runtime_probes_performed': False, 'automatic_import': False}))
        return
    server = ThreadingHTTPServer((host, port), Handler)
    server.application = application
    if config.get('tls_cert_file'):
        private_file(config['tls_cert_file'])
        private_file(config['tls_key_file'], secret=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config['tls_cert_file'], config['tls_key_file'])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except Refusal as error:
        sys.exit('Control service refused: ' + str(error))
