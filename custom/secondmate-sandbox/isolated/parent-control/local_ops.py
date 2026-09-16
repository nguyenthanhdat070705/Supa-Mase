"""Parent-only policy export, report intake, and explicitly approved knowledge writes."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from common import (CONFIG_PATHS, DESTINATIONS, MAX_BODY, Refusal, brain_path,
                    canonical, digest, identifier, uuid_id, validate_brain, validate_proposal)


def home_path(home):
    path = Path(home)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise Refusal('Specify an existing absolute parent FM_HOME without symlinks.')
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise Refusal('Parent home cannot contain symlinked ancestors.')
    return path


def confined(home, relative, create_parents=False):
    if not isinstance(relative, str) or '\\' in relative or relative.startswith('/') or any(x in ('', '.', '..') for x in relative.split('/')):
        raise Refusal('Invalid local relative path.')
    target = home.joinpath(*relative.split('/'))
    for part in [target, *target.parents]:
        if part == home.parent:
            break
        if part.is_symlink():
            raise Refusal('Refusing symlinked local artifact.')
        if part.exists() and part.is_file() and part.stat().st_nlink != 1:
            raise Refusal('Refusing hardlinked local artifact.')
    if create_parents:
        missing = []
        directory = target.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            fsync_directory(directory.parent)
    return target


def fsync_directory(path):
    # Linux deployment requires durable directory entries before a later cursor,
    # receipt, or external notification can acknowledge the associated write.
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic(path, data):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise Refusal('Unsafe local write destination.')
    temp = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def locked(home, name):
    path = confined(home, 'state/parent-control/' + name + '.lock', True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Refusal('Another parent operation holds this lock; inspect stale locks before retrying.', 409, 'locked') from None
    os.close(fd)
    try:
        yield
    finally:
        path.unlink()


def export_brain(home, skills=(), include_backend=False, model=None, effort=None):
    home = home_path(home)
    selected = sorted(CONFIG_PATHS - {'config/backend'})
    if include_backend:
        selected.append('config/backend')
    for skill in skills:
        identifier(skill)
        directory = confined(home, '.agents/skills/' + skill)
        if not directory.is_dir() or not (directory / 'SKILL.md').is_file():
            raise Refusal('An explicitly selected curated skill is unavailable.')
        for path in directory.rglob('*'):
            if path.is_symlink():
                raise Refusal('Curated skill exports cannot follow symlinks.')
            if path.is_file():
                selected.append(path.relative_to(home).as_posix())
    files = []
    for relative in sorted(set(selected)):
        brain_path(relative)
        path = confined(home, relative)
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > 500_000:
            raise Refusal('Policy file is not a bounded text file.')
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeError:
            raise Refusal('Curated policy must contain UTF-8 text.') from None
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{24,}', content):
            raise Refusal('Possible credential detected in selected policy; remove it before export.')
        files.append({'path': relative, 'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()})
    if model is not None or effort is not None:
        if not model or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,150}', model) or effort not in {'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}:
            raise Refusal('Explicit model and supported effort are required together.')
        content = json.dumps({'model': model, 'reasoning_effort': effort}, sort_keys=True) + '\n'
        files = [x for x in files if x['path'] != 'config/inherited-runtime.json']
        files.append({'path': 'config/inherited-runtime.json', 'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()})
    files.sort(key=lambda item: item['path'])
    source_commit = 'unknown'
    try:
        result = subprocess.run(['git', '-C', str(home), 'rev-parse', 'HEAD'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
        candidate = result.stdout.decode().strip()
        if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', candidate):
            source_commit = candidate
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    snapshot = {'schema': 'brain-snapshot.v1', 'revision': digest(files), 'source_commit': source_commit, 'files': files}
    validate_brain(snapshot)
    return snapshot


def intake_reports(home, response):
    home = home_path(home)
    if not isinstance(response, dict) or not isinstance(response.get('events'), list):
        raise Refusal('Invalid parent report batch.')
    cursor_path = confined(home, 'state/parent-control/report-cursor.json', True)
    stored = []
    with locked(home, 'report-intake'):
        cursor = json.loads(cursor_path.read_text()).get('cursor', 0) if cursor_path.exists() else 0
        for event in response['events']:
            event_id = uuid_id(event.get('event_id'))
            identifier(event.get('child_id'))
            sequence = event.get('sequence')
            if type(sequence) is not int or sequence <= 0:
                raise Refusal('Invalid parent event sequence.')
            encoded = canonical(event) + b'\n'
            path = confined(home, 'data/secondmate-inbox/events/' + event_id + '.json', True)
            if path.exists() and path.read_bytes() != encoded:
                raise Refusal('Parent event changed after receipt.', 409, 'conflict')
            if not path.exists():
                atomic(path, encoded)
                stored.append({'event_id': event_id, 'path': str(path), 'event': event})
            cursor = max(cursor, sequence)
        if response.get('next_cursor') != cursor and response['events']:
            raise Refusal('Report batch cursor is inconsistent.')
        atomic(cursor_path, canonical({'cursor': cursor}) + b'\n')
    return {'stored': stored, 'cursor': cursor, 'automatic_import': False,
            'notification_required': [x for x in stored if x['event'].get('requires_mac_notification') or x['event'].get('kind') in ('blocked', 'failed', 'decision', 'pr-ready')]}


def read_cursor(home):
    home = home_path(home)
    path = confined(home, 'state/parent-control/report-cursor.json')
    return json.loads(path.read_text()).get('cursor', 0) if path.exists() else 0


def apply_approval(home, approval, parent_id):
    home = home_path(home)
    if not isinstance(approval, dict) or approval.get('schema') != 'knowledge-approval.v1' or approval.get('parent_id') != parent_id:
        raise Refusal('Approval does not belong to this parent.')
    approval_id = uuid_id(approval.get('approval_id'))
    destination = approval.get('destination')
    if destination not in DESTINATIONS:
        raise Refusal('Knowledge destination is not allowlisted.')
    proposal = validate_proposal(approval.get('proposal'))
    if any(approval.get(k) != proposal[k] for k in ['proposal_id', 'version', 'sha256']):
        raise Refusal('Approved proposal version or hash changed.')
    selected = approval.get('selected_claims')
    claims = {x['id']: x for x in proposal['manifest']['claims']}
    if not isinstance(selected, list) or not selected or not all(isinstance(x, str) and x in claims for x in selected) or len(selected) != len(set(selected)):
        raise Refusal('Approved claims are invalid.')
    reference = approval.get('approval_reference')
    if not isinstance(reference, dict) or not all(reference.get(k) for k in ['captain_user_id', 'source', 'reference', 'quoted_approval']):
        raise Refusal('Explicit captain approval provenance is missing.')
    marker = '<!-- knowledge-approval:' + approval_id + ' sha256:' + proposal['sha256'] + ' -->'
    lines = [marker, '### Reviewed knowledge from ' + approval['child_id'],
             '', 'Proposal: ' + proposal['proposal_id'] + ' v' + str(proposal['version']),
             'Scope: ' + json.dumps(proposal['manifest']['scope'], ensure_ascii=False, sort_keys=True),
             'Approval reference: ' + reference['source'] + ' / ' + reference['reference'], '']
    evidence = {x['id']: x for x in proposal['manifest']['evidence']}
    for claim_id in selected:
        claim = claims[claim_id]
        lines.extend(['- [' + claim_id + '] ' + claim['text'],
                      '  Evidence: ' + '; '.join(evidence[e]['source'] for e in claim['evidence_ids'])])
    block = ('\n'.join(lines) + '\n').encode('utf-8')
    receipt_path = confined(home, 'state/parent-control/promotions/' + approval_id + '.json', True)
    target = confined(home, destination, True)
    with locked(home, 'knowledge-apply'):
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt['sha256'] != proposal['sha256'] or receipt['destination'] != destination:
                raise Refusal('Existing promotion receipt conflicts with this approval.', 409, 'conflict')
            return receipt
        prior = target.read_bytes() if target.exists() else b''
        if marker.encode() in prior:
            if prior.count(marker.encode()) != 1 or block not in prior:
                raise Refusal('Existing promotion marker has different content.', 409, 'conflict')
            result = prior  # Recover a crash after durable target write, before receipt.
        else:
            result = prior + (b'\n\n' if prior else b'') + block
            atomic(target, result)
        receipt = {'approval_id': approval_id, 'sha256': proposal['sha256'],
                   'destination': destination, 'result_sha256': hashlib.sha256(result).hexdigest()}
        atomic(receipt_path, canonical(receipt) + b'\n')
        return receipt
