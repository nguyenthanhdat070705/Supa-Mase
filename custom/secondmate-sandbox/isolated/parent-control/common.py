"""Shared, credential-free wire contracts for the explicit parent adapter."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
import uuid

MAX_BODY = 2_000_000
CONFIG_PATHS = frozenset({
    'config/inherited-runtime.json', 'config/crew-dispatch.json',
    'config/crew-harness', 'config/backend', 'config/backlog-backend',
    'config/startup-memory-budget',
})
DESTINATIONS = frozenset({'data/learnings.md', 'data/knowledge-reviewed.md'})
KINDS = frozenset({'done', 'blocked', 'failed', 'progress', 'decision', 'pr-ready', 'knowledge-proposal'})


class Refusal(Exception):
    def __init__(self, message, status=400, code='refused'):
        super().__init__(message)
        self.status = status
        self.code = code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', value):
        raise Refusal('Invalid configured identity.')
    return value


def uuid_id(value):
    try:
        valid = str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Refusal('Expected a canonical UUID.')
    return value


def text(value, maximum=100_000, empty=False):
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum or ('\x00' in value) or (not empty and not value.strip()):
        raise Refusal('Invalid or oversized text field.')
    return value


def brain_path(value):
    if not isinstance(value, str) or '\\' in value or not value or value.startswith('/'):
        raise Refusal('Brain path is outside the export allowlist.')
    path = PurePosixPath(value)
    if str(path) != value or any(x in ('.', '..', '') for x in value.split('/')):
        raise Refusal('Brain path is outside the export allowlist.')
    if value in CONFIG_PATHS:
        return value
    parts = path.parts
    if len(parts) >= 4 and parts[:2] == ('.agents', 'skills'):
        identifier(parts[2])
        if not all(re.fullmatch(r'[A-Za-z0-9._-]+', p) and not p.startswith('.') for p in parts[3:]):
            raise Refusal('Invalid curated skill path.')
        return value
    raise Refusal('Brain path is outside the export allowlist.')


def validate_brain(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'revision', 'source_commit', 'files'} or value['schema'] != 'brain-snapshot.v1':
        raise Refusal('Invalid brain snapshot schema.')
    if not isinstance(value['source_commit'], str) or not re.fullmatch(r'[0-9a-f]{40}|unknown', value['source_commit']):
        raise Refusal('Invalid brain source commit.')
    files = value['files']
    if not isinstance(files, list) or len(files) > 500:
        raise Refusal('Invalid brain file list.')
    seen = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'content'}:
            raise Refusal('Invalid brain file entry.')
        brain_path(item['path'])
        text(item['content'], 500_000, empty=True)
        if hashlib.sha256(item['content'].encode()).hexdigest() != item['sha256']:
            raise Refusal('Brain file digest does not match.')
        seen.append(item['path'])
    if seen != sorted(set(seen)) or digest(files) != value['revision'] or len(canonical(value)) > MAX_BODY:
        raise Refusal('Brain revision, ordering, or size is invalid.')
    return value


def validate_proposal(value):
    keys = {'schema', 'proposal_id', 'version', 'manifest', 'content', 'sha256'}
    if not isinstance(value, dict) or set(value) != keys or value['schema'] != 'knowledge-proposal.v1':
        raise Refusal('Invalid knowledge proposal schema.')
    uuid_id(value['proposal_id'])
    if type(value['version']) is not int or not 1 <= value['version'] <= 1_000_000:
        raise Refusal('Invalid proposal version.')
    manifest = value['manifest']
    if not isinstance(manifest, dict) or set(manifest) != {'claims', 'evidence', 'scope'}:
        raise Refusal('Invalid proposal manifest.')
    if not isinstance(manifest['scope'], dict) or not isinstance(manifest['scope'].get('domain'), str):
        raise Refusal('Proposal requires a domain scope.')
    text(manifest['scope']['domain'], 200)
    if len(canonical(manifest['scope'])) > 4000:
        raise Refusal('Proposal scope is oversized.')
    evidence_ids = set()
    if not isinstance(manifest['evidence'], list) or len(manifest['evidence']) > 128:
        raise Refusal('Invalid evidence list.')
    for item in manifest['evidence']:
        if not isinstance(item, dict) or set(item) != {'id', 'description', 'source'}:
            raise Refusal('Invalid evidence entry.')
        identifier(item['id'])
        text(item['description'], 8000)
        text(item['source'], 2000)
        if item['id'] in evidence_ids:
            raise Refusal('Duplicate evidence id.')
        evidence_ids.add(item['id'])
    claims = manifest['claims']
    if not isinstance(claims, list) or not 1 <= len(claims) <= 64:
        raise Refusal('Expected 1-64 claims.')
    claim_ids = set()
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {'id', 'text', 'evidence_ids'}:
            raise Refusal('Invalid claim entry.')
        identifier(claim['id'])
        text(claim['text'], 16000)
        refs = claim['evidence_ids']
        if not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x in evidence_ids for x in refs) or len(refs) != len(set(refs)):
            raise Refusal('Claim evidence references are invalid.')
        if claim['id'] in claim_ids:
            raise Refusal('Duplicate claim id.')
        claim_ids.add(claim['id'])
    text(value['content'], 500_000, empty=True)
    payload = {k: v for k, v in value.items() if k != 'sha256'}
    if digest(payload) != value['sha256'] or len(canonical(value)) > MAX_BODY:
        raise Refusal('Proposal content digest or size is invalid.')
    return value
