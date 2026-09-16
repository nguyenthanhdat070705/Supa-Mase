"""Durable advisory cases; no agent execution, personal-device or approval power."""
import json
import re
import uuid

from common import Refusal, canonical, digest, text, uuid_id


CASE_AUTHORITY = {
    'origin': 'untrusted-child-case', 'purpose': 'analysis-and-advice',
    'approval': False, 'personal_device_actions': False, 'knowledge_import': False,
    'merge': False, 'deploy': False, 'administration': False,
}
REPLY_AUTHORITY = {'role': 'parent-advisor', 'approval': False}


def evidence(value):
    if not isinstance(value, list) or len(value) > 16:
        raise Refusal('Escalation evidence must be a bounded list.')
    for item in value:
        if not isinstance(item, dict) or set(item) != {'label', 'source'}:
            raise Refusal('Escalation evidence fields are invalid.')
        text(item['label'], 200)
        text(item['source'], 2000)


def signed(value, schema, keys):
    if not isinstance(value, dict) or set(value) != keys or value.get('schema') != schema:
        raise Refusal('Escalation wire schema is invalid.')
    if value['sha256'] != digest({k: v for k, v in value.items() if k != 'sha256'}):
        raise Refusal('Escalation content hash does not match.')
    if len(canonical(value)) > 120_000:
        raise Refusal('Escalation content is oversized.')


def validate_request(value):
    signed(value, 'escalation-request.v1', {'schema', 'escalation_id', 'task_id', 'reason',
                                         'question', 'context', 'evidence', 'sha256'})
    uuid_id(value['escalation_id'])
    text(value['task_id'], 120)
    if value['reason'] not in ('needs-analysis', 'needs-decision', 'blocked'):
        raise Refusal('Escalation reason is invalid.')
    text(value['question'], 16_000)
    text(value['context'], 64_000, empty=True)
    evidence(value['evidence'])
    return value


def validate_response(value):
    signed(value, 'escalation-response.v1', {'schema', 'response_id', 'escalation_id',
                                          'request_sha256', 'claim_id', 'body', 'evidence', 'sha256'})
    for field in ('response_id', 'escalation_id', 'claim_id'):
        uuid_id(value[field])
    if not isinstance(value['request_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['request_sha256']):
        raise Refusal('Escalation request hash is invalid.')
    text(value['body'], 100_000)
    evidence(value['evidence'])
    return value


class Escalations:
    def __init__(self, application):
        self.app = application
        with self.app.lock, self.app.db:
            self.app.db.execute('''CREATE TABLE IF NOT EXISTS escalations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                child_id TEXT NOT NULL, parent_id TEXT NOT NULL,
                escalation_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                request TEXT NOT NULL, status TEXT NOT NULL,
                claim_id TEXT, response TEXT, cancellation TEXT, ack TEXT,
                UNIQUE(child_id, escalation_id))''')

    def row(self, child_id, escalation_id):
        uuid_id(escalation_id)
        row = self.app.db.execute('SELECT * FROM escalations WHERE child_id=? AND escalation_id=?',
                                  (child_id, escalation_id)).fetchone()
        if not row:
            raise Refusal('Escalation is unavailable.', 404, 'not_found')
        if row['parent_id'] != self.app.children[child_id]['parent_id']:
            raise Refusal('Escalation retains its original owner; operator migration is required.', 409, 'owner_changed')
        return row

    def case(self, row):
        return {'schema': 'escalation-case.v1', 'child_id': row['child_id'], 'parent_id': row['parent_id'],
                'request': json.loads(row['request']), 'status': row['status'],
                'claim_id': row['claim_id'], 'acknowledged': bool(row['ack']),
                'authority': dict(CASE_AUTHORITY)}

    def result(self, row):
        content = row['cancellation'] if row['status'] == 'cancelled' else row['response']
        if not content:
            return None
        return {'status': row['status'], 'escalation_id': row['escalation_id'],
                'request_sha256': row['request_hash'], 'response': json.loads(content),
                'authority': dict(REPLY_AUTHORITY)}

    def submit(self, principal, child_id, payload):
        child = self.app.child_scope(principal, child_id, 'child')
        validate_request(payload)
        with self.app.lock, self.app.db:
            old = self.app.db.execute('SELECT * FROM escalations WHERE child_id=? AND escalation_id=?',
                                      (child_id, payload['escalation_id'])).fetchone()
            if old and (old['request_hash'] != payload['sha256'] or old['parent_id'] != child['parent_id']):
                raise Refusal('Escalation ID already binds different content or parent.', 409, 'conflict')
            if not old:
                self.app.db.execute('''INSERT INTO escalations
                    (child_id,parent_id,escalation_id,request_hash,request,status)
                    VALUES (?,?,?,?,?,'submitted')''',
                    (child_id, child['parent_id'], payload['escalation_id'], payload['sha256'], canonical(payload).decode()))
                event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'escalation:' + child_id + ':' + payload['escalation_id']))
                self.app.event(child_id, {'event_id': event_id, 'child_id': child_id, 'kind': 'decision',
                    'text': 'Child requests analysis or advice. This case grants no captain or personal-device authority.',
                    'escalation_id': payload['escalation_id'], 'request_sha256': payload['sha256']})
            row = self.row(child_id, payload['escalation_id'])
        return {'escalation_id': payload['escalation_id'], 'sha256': payload['sha256'],
                'stored': True, 'status': row['status']}

    def handle(self, method, path, principal, payload):
        if path == '/v1/escalations':
            if method != 'GET' or principal['role'] != 'parent':
                raise Refusal('Only the owning parent can pull advisory cases.', 403, 'forbidden')
            with self.app.lock:
                rows = self.app.db.execute('''SELECT * FROM escalations
                    WHERE parent_id=? AND status IN ('submitted','claimed')
                    ORDER BY CASE status WHEN 'submitted' THEN 0 ELSE 1 END,sequence LIMIT 20''',
                    (principal['id'],)).fetchall()
                rows = [row for row in rows if self.app.children.get(row['child_id'], {}).get('parent_id') == principal['id']]
                return {'cases': [self.case(row) for row in rows]}
        match = re.fullmatch(r'/v1/children/([A-Za-z0-9][A-Za-z0-9._-]{0,79})/escalations(?:/(results|[0-9a-f-]{36})(?:/(claim|reply|ack|cancel))?)?', path)
        if not match:
            raise Refusal('Unknown escalation endpoint.', 404, 'not_found')
        child_id, escalation_id, action = match.groups()
        self.app.child_scope(principal, child_id)
        if escalation_id is None and method == 'POST':
            return self.submit(principal, child_id, payload)
        if escalation_id == 'results' and action is None and method == 'GET':
            self.app.child_scope(principal, child_id, 'child')
            with self.app.lock:
                rows = self.app.db.execute('''SELECT * FROM escalations WHERE child_id=? AND parent_id=?
                    AND status IN ('answered','cancelled') AND ack IS NULL ORDER BY sequence LIMIT 20''',
                    (child_id, self.app.children[child_id]['parent_id'])).fetchall()
                return {'results': [self.result(row) for row in rows]}
        if not escalation_id or escalation_id == 'results':
            raise Refusal('Unsupported escalation operation.', 405, 'method_not_allowed')
        with self.app.lock, self.app.db:
            row = self.row(child_id, escalation_id)
            if method == 'GET' and action is None and principal['role'] in ('parent', 'child'):
                return {'case': self.case(row), 'result': self.result(row)}
            if method != 'POST':
                raise Refusal('Unsupported escalation operation.', 405, 'method_not_allowed')
            if action == 'claim':
                self.app.child_scope(principal, child_id, 'parent')
                if set(payload) != {'claim_id', 'request_sha256'} or payload['request_sha256'] != row['request_hash']:
                    raise Refusal('Claim must name the exact immutable case.')
                uuid_id(payload['claim_id'])
                if row['status'] == 'submitted':
                    self.app.db.execute("UPDATE escalations SET status='claimed',claim_id=? WHERE child_id=? AND escalation_id=?",
                                        (payload['claim_id'], child_id, escalation_id))
                elif row['claim_id'] != payload['claim_id'] or row['status'] not in ('claimed', 'answered'):
                    raise Refusal('Case is claimed, cancelled or closed; never redispatch blindly.', 409, 'claim_conflict')
                return {'case': self.case(self.row(child_id, escalation_id)), 'claimed': True}
            if action == 'reply':
                self.app.child_scope(principal, child_id, 'parent')
                validate_response(payload)
                if (payload['escalation_id'] != escalation_id or payload['request_sha256'] != row['request_hash']
                        or payload['claim_id'] != row['claim_id']):
                    raise Refusal('Reply does not match the exact parent claim.', 409, 'reply_conflict')
                encoded = canonical(payload).decode()
                if row['status'] == 'answered' and row['response'] == encoded:
                    return {'stored': True, 'result': self.result(row)}
                if row['status'] != 'claimed' or row['response'] is not None:
                    raise Refusal('Case no longer accepts a reply.', 409, 'case_closed')
                self.app.db.execute("UPDATE escalations SET status='answered',response=? WHERE child_id=? AND escalation_id=?",
                                    (encoded, child_id, escalation_id))
                return {'stored': True, 'result': self.result(self.row(child_id, escalation_id))}
            if action == 'cancel':
                self.app.child_scope(principal, child_id, 'child')
                if set(payload) != {'request_sha256', 'reason'} or payload['request_sha256'] != row['request_hash']:
                    raise Refusal('Cancellation must name the exact immutable case.')
                text(payload['reason'], 2000)
                cancellation = {'schema': 'escalation-cancellation.v1',
                    'response_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'escalation-cancel:' + child_id + ':' + escalation_id)),
                    'escalation_id': escalation_id, 'request_sha256': row['request_hash'], 'reason': payload['reason']}
                cancellation['sha256'] = digest(cancellation)
                encoded = canonical(cancellation).decode()
                if row['cancellation'] and row['cancellation'] != encoded:
                    raise Refusal('Cancellation is immutable.', 409, 'conflict')
                if row['cancellation'] == encoded:
                    return {'stored': True, 'result': self.result(row)}
                if row['ack']:
                    raise Refusal('An acknowledged case cannot be cancelled.', 409, 'case_closed')
                self.app.db.execute("UPDATE escalations SET status='cancelled',cancellation=? WHERE child_id=? AND escalation_id=?",
                                    (encoded, child_id, escalation_id))
                return {'stored': True, 'result': self.result(self.row(child_id, escalation_id))}
            if action == 'ack':
                self.app.child_scope(principal, child_id, 'child')
                result = self.result(row)
                if (set(payload) != {'response_id', 'sha256'} or result is None
                        or any(payload[key] != result['response'][key] for key in ('response_id', 'sha256'))):
                    raise Refusal('Acknowledgement must match the current terminal result.', 409, 'ack_conflict')
                encoded = canonical(payload).decode()
                if row['ack'] and row['ack'] != encoded:
                    raise Refusal('Acknowledgement conflicts with the stored receipt.', 409, 'conflict')
                self.app.db.execute('UPDATE escalations SET ack=? WHERE child_id=? AND escalation_id=?',
                                    (encoded, child_id, escalation_id))
                return {'stored': True, 'acknowledged': True, **payload}
        raise Refusal('Unsupported escalation operation.', 405, 'method_not_allowed')
