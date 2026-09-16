#!/usr/bin/env python3
"""Parent/operator HTTP client. This client needs no Docker CLI or socket."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler
import uuid

from common import MAX_BODY, Refusal, canonical, identifier, uuid_id
from local_ops import (apply_approval, atomic, confined, export_brain, home_path,
                       intake_reports, locked, read_cursor)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, config):
        self.config = config
        self.parent_id = identifier(config['parent_id'])
        self.url = config['base_url'].rstrip('/')
        parsed = urlsplit(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise Refusal('Control service URL must be a configured HTTP(S) origin.')
        token_path = Path(config['token_file'])
        if not token_path.is_absolute() or token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_nlink != 1:
            raise Refusal('Scoped token path is unsafe.')
        if os.name != 'nt' and token_path.stat().st_mode & 0o077:
            raise Refusal('Scoped client token must not be group/other accessible.')
        self.token = token_path.read_text().strip()
        context = ssl.create_default_context(cafile=config.get('ca_file'))
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))

    def call(self, method, path, payload=None):
        request = Request(self.url + path, method=method,
                          data=canonical(payload) if payload is not None else None,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=45) as response:
                content = response.read(MAX_BODY * 4 + 1)
        except HTTPError as error:
            # Server errors contain only fixed diagnostics; never echo raw HTTP bodies.
            try:
                value = json.loads(error.read(2000))
                code = value.get('error', 'refused')
            except Exception:
                code = 'refused'
            raise Refusal('Control service refused this request.', error.code,
                          code if isinstance(code, str) else 'refused') from None
        except (URLError, TimeoutError, OSError):
            raise Refusal('Control request completion is unknown; retain its exact request ID.', 503, 'transport_unknown') from None
        if len(content) > MAX_BODY * 4:
            raise Refusal('Control response exceeds the client bound.')
        try:
            return json.loads(content)
        except (ValueError, UnicodeError):
            raise Refusal('Control service returned invalid JSON.') from None


def notify_pending(home, config):
    """Deliver only through an explicitly configured parent-owned notification argv.

    This program does not know a Telegram destination/token. The callback receives
    one review-event JSON on stdin and must implement the authorized Mac DM path.
    Uncertain callback outcomes are held for operator reconciliation, not replayed.
    """
    command = config.get('notifier_command')
    if not command:
        return {'configured': False, 'sent': 0}
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command) or not Path(command[0]).is_absolute():
        raise Refusal('Notification callback must be a fixed absolute argv.')
    home = home_path(home)
    directory = confined(home, 'data/secondmate-inbox/events', True)
    sent = 0
    with locked(home, 'report-notify'):
        for path in sorted(directory.glob('*.json')):
            event = json.loads(confined(home, path.relative_to(home).as_posix()).read_text())
            if not (event.get('requires_mac_notification') or event.get('kind') in ('blocked', 'failed', 'decision', 'pr-ready')):
                continue
            event_id = uuid_id(event['event_id'])
            record = confined(home, 'state/parent-control/notifications/' + event_id + '.json', True)
            if record.exists():
                continue  # sent and unknown both require no blind duplicate.
            atomic(record, canonical({'state': 'sending', 'event_id': event_id}) + b'\n')
            environment = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'FM_HOME': str(home), 'LANG': 'C.UTF-8'}
            try:
                result = subprocess.run(command, input=canonical(event), env=environment,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
                state = 'sent' if result.returncode == 0 else 'unknown'
            except (OSError, subprocess.TimeoutExpired):
                state = 'unknown'
            atomic(record, canonical({'state': state, 'event_id': event_id}) + b'\n')
            if state == 'sent':
                sent += 1
    return {'configured': True, 'sent': sent, 'uncertain_outcomes_require_reconciliation': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--home', default=os.environ.get('FM_HOME'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    status = sub.add_parser('status'); status.add_argument('child')
    send = sub.add_parser('send'); send.add_argument('child'); send.add_argument('--file', required=True)
    send.add_argument('--scope-file'); send.add_argument('--request-id'); send.add_argument('--correlation')
    send.add_argument('--retry', action='store_true')
    sub.add_parser('pull')
    collect = sub.add_parser('collect'); collect.add_argument('--interval', type=int, default=10); collect.add_argument('--once', action='store_true')
    proposals = sub.add_parser('proposals'); proposals.add_argument('child'); proposals.add_argument('--proposal-id'); proposals.add_argument('--version', type=int)
    export = sub.add_parser('publish-brain'); export.add_argument('child'); export.add_argument('--skill', action='append', default=[])
    export.add_argument('--include-backend', action='store_true'); export.add_argument('--model'); export.add_argument('--effort')
    approve = sub.add_parser('approve'); approve.add_argument('child'); approve.add_argument('--file', required=True)
    apply = sub.add_parser('apply'); apply.add_argument('approval_id')
    control = sub.add_parser('control'); control.add_argument('child'); control.add_argument('action', choices=['start', 'stop', 'restart', 'resume'])
    control.add_argument('--expected-generation', required=True); control.add_argument('--operation-id')
    control.add_argument('--operator-recovery-reference')
    args = parser.parse_args()
    os.umask(0o077)
    config_path = Path(args.config)
    if config_path.is_symlink() or not config_path.is_file():
        raise Refusal('Client configuration is unavailable or unsafe.')
    config = json.loads(config_path.read_text())
    client = Client(config)
    child = identifier(args.child) if hasattr(args, 'child') else None
    route = '/v1/children/' + child if child else None
    if args.command == 'list':
        result = client.call('GET', '/v1/children')
    elif args.command == 'status':
        result = client.call('GET', route + '/status')
    elif args.command == 'send':
        if not args.home:
            raise Refusal('FM_HOME is required to persist the request before sending.')
        home = home_path(args.home)
        request_id = uuid_id(args.request_id) if args.request_id else str(uuid.uuid4())
        record = confined(home, 'state/parent-control/requests/' + request_id + '.json', True)
        if args.retry:
            if not record.exists():
                raise Refusal('Retry requires the original durable request record.')
            payload = json.loads(record.read_text())
        else:
            payload = {'request_id': request_id, 'correlation': args.correlation or secrets.token_hex(8),
                       'body': Path(args.file).read_text(),
                       'scope': json.loads(Path(args.scope_file).read_text()) if args.scope_file else {}}
            if record.exists() and json.loads(record.read_text()) != payload:
                raise Refusal('Local request ID already has different content.')
            if not record.exists():
                atomic(record, canonical(payload) + b'\n')
        result = client.call('POST', route + ('/requests/retry' if args.retry else '/requests'), payload)
        result['durable_request'] = str(record)
    elif args.command in ('pull', 'collect'):
        if not args.home:
            raise Refusal('FM_HOME is required for parent report intake.')
        if args.command == 'collect' and not 2 <= args.interval <= 3600:
            raise Refusal('Collector interval must be 2-3600 seconds.')
        while True:
            response = client.call('GET', '/v1/reports?after=' + str(read_cursor(args.home)))
            result = intake_reports(args.home, response)
            result['notification'] = notify_pending(args.home, config)
            if args.command == 'pull' or args.once:
                break
            if result['stored']:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            time.sleep(args.interval)
    elif args.command == 'proposals':
        query = '?' + urlencode({'proposal_id': args.proposal_id, 'version': args.version}) if args.proposal_id and args.version else ''
        result = client.call('GET', route + '/proposals' + query)
    elif args.command == 'publish-brain':
        if not args.home:
            raise Refusal('FM_HOME is required for allowlisted parent export.')
        result = client.call('POST', route + '/brain', export_brain(args.home, args.skill, args.include_backend, args.model, args.effort))
    elif args.command == 'approve':
        result = client.call('POST', route + '/approvals', json.loads(Path(args.file).read_text()))
    elif args.command == 'apply':
        if not args.home:
            raise Refusal('FM_HOME is required for parent-owned promotion.')
        approval_id = uuid_id(args.approval_id)
        response = client.call('GET', '/v1/approvals/' + approval_id)
        receipt = apply_approval(args.home, response['approval'], client.parent_id)
        result = client.call('POST', '/v1/approvals/' + approval_id + '/receipt', receipt)
    elif args.command == 'control':
        payload = {'operation_id': args.operation_id or str(uuid.uuid4()), 'action': args.action,
                   'expected_generation': args.expected_generation}
        if args.operator_recovery_reference:
            payload['operator_recovery_reference'] = args.operator_recovery_reference
        result = client.call('POST', route + '/control', payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Refusal as error:
        print(json.dumps({'error': error.code, 'message': str(error)}), file=sys.stderr)
        sys.exit(1)
