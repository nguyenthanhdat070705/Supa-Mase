"""Fixed-container lifecycle with generation checks and a runtime quiesce lease."""
import json
import re
import subprocess

from common import Refusal, canonical, digest


class Lifecycle:
    def __init__(self, docker='/usr/bin/docker', runner=None):
        self.docker = docker
        self.runner = runner or subprocess.run

    def run(self, args, timeout=40):
        try:
            result = self.runner([self.docker, *args], input=b'', stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=timeout, check=False,
                                 env={'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'})
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal('Lifecycle completion is unknown; inspect this operation before retry.', 503, 'lifecycle_unknown') from None
        if result.returncode or len(result.stdout) > 2_000_000:
            raise Refusal('Lifecycle operation failed; no command output was disclosed.', 503, 'lifecycle_unknown')
        return result.stdout

    def runtime(self, child, *arguments):
        output = self.run(['exec', '-i', '--user', child.get('exec_user', '1000:1000'),
                           '--env', 'FM_HOME=' + child['home'], child['container'],
                           '/usr/bin/python3', '/opt/secondmate/bridge.py', *arguments])
        try:
            value = json.loads(output)
        except ValueError:
            raise Refusal('Invalid lifecycle runtime response.', 503, 'lifecycle_unknown') from None
        if not isinstance(value, dict):
            raise Refusal('Invalid lifecycle runtime response.', 503, 'lifecycle_unknown')
        return value

    def inspect(self, child):
        template = '{"id":{{json .Id}},"running":{{json .State.Running}},"started_at":{{json .State.StartedAt}},"status":{{json .State.Status}}}'
        try:
            value = json.loads(self.run(['inspect', '--format', template, child['container']]))
        except ValueError:
            raise Refusal('Invalid container identity response.', 503, 'lifecycle_unknown') from None
        if not isinstance(value, dict) or not re.fullmatch(r'[0-9a-f]{64}', value.get('id', '')) or type(value.get('running')) is not bool:
            raise Refusal('Unverified configured container identity.', 503, 'lifecycle_unknown')
        value['container_generation'] = 'container:' + digest(value)
        return value

    def status(self, child):
        container = self.inspect(child)
        if not container['running']:
            return {'generation': container['container_generation'], 'container': container,
                    'running': False, 'identity_verified': True, 'safe_to_stop': False}
        try:
            runtime = self.runtime(child, 'control-check')
        except Refusal:
            return {'generation': container['container_generation'], 'container': container,
                    'running': True, 'identity_verified': False, 'safe_to_stop': False, 'runtime_unknown': True}
        return {**runtime, 'container': container, 'running': True}

    def execute(self, child, action, expected_generation, operator_recovery=False):
        before = self.status(child)
        if before.get('generation') != expected_generation:
            raise Refusal('Runtime generation changed; obtain a fresh status before acting.', 409, 'stale_generation')
        if before.get('identity_verified') is not True and not operator_recovery:
            raise Refusal('Runtime identity is unverified.', 409, 'unverified_runtime')
        container = before['container']
        if action == 'start':
            if before['running']:
                raise Refusal('The configured container is already running; no duplicate is created.', 409, 'already_running')
        elif action == 'resume':
            if not before['running'] or not before.get('lease_id'):
                raise Refusal('No matching active quiesce lease is available.', 409, 'no_lease')
            result = self.runtime(child, 'control-resume', '--lease-id', before['lease_id'])
            return {'action': action, 'before_generation': expected_generation, 'result': result}
        elif action in ('stop', 'restart'):
            if not before['running']:
                raise Refusal('Container is stopped; use start with its current generation.', 409, 'not_running')
            if not operator_recovery:
                if before.get('safe_to_stop') is not True:
                    raise Refusal('Active or uncertain work prevents normal maintenance.', 409, 'active_work')
                lease = self.runtime(child, 'control-check', '--quiesce', '--expected-generation', expected_generation)
                if lease.get('generation') != expected_generation or not lease.get('lease_id') or lease.get('safe_to_stop') is not True:
                    raise Refusal('Runtime did not grant a safe matching lifecycle lease.', 409, 'lease_refused')
                check = self.runtime(child, 'control-check')
                if check.get('generation') != expected_generation or check.get('lease_id') != lease['lease_id'] or check.get('safe_to_stop') is not True:
                    raise Refusal('Runtime changed after quiesce; leave it paused and inspect.', 409, 'lease_changed')
        else:
            raise Refusal('Unsupported lifecycle action.')
        latest = self.inspect(child)
        if latest['container_generation'] != container['container_generation']:
            raise Refusal('Container identity changed before lifecycle action.', 409, 'stale_generation')
        # Existing named container only: no create/run/compose/exec shell surface.
        arguments = [action, '--time', '20', container['id']] if action in ('stop', 'restart') else ['start', container['id']]
        self.run(arguments, timeout=45)
        after = self.inspect(child)
        if after['id'] != container['id'] or after['running'] != (action != 'stop'):
            raise Refusal('Lifecycle result does not match the requested state.', 503, 'lifecycle_unknown')
        return {'action': action, 'before_generation': expected_generation, 'container': after,
                'agent_readiness': 'not_yet_verified' if action != 'stop' else 'stopped',
                'operator_recovery': operator_recovery}
