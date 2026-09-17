"""Socket-level and structural tests for bounded HTTP admission."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import server as control_server


class PartialHeaderHandler(BaseHTTPRequestHandler):
    """Records worker admission while request parsing blocks on headers."""

    lock = threading.Lock()
    saturated = threading.Event()
    idle = threading.Event()
    active = 0
    maximum_active = 0
    total_started = 0
    initial_timeouts = []
    expected_workers = 4

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.active = 0
            cls.maximum_active = 0
            cls.total_started = 0
            cls.initial_timeouts = []
            cls.saturated.clear()
            cls.idle.clear()

    def setup(self):
        super().setup()
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.total_started += 1
            cls.maximum_active = max(cls.maximum_active, cls.active)
            cls.initial_timeouts.append(self.connection.gettimeout())
            cls.idle.clear()
            if cls.active == cls.expected_workers:
                cls.saturated.set()

    def finish(self):
        cls = type(self)
        try:
            super().finish()
        finally:
            with cls.lock:
                cls.active -= 1
                if cls.active == 0:
                    cls.idle.set()

    def do_GET(self):
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class BoundedServerSocketTests(unittest.TestCase):
    @staticmethod
    def connection_was_closed(client):
        client.settimeout(1)
        try:
            return client.recv(1) == b''
        except socket.timeout:
            return False
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            return True

    @staticmethod
    def wait_for_started(count, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with PartialHeaderHandler.lock:
                if PartialHeaderHandler.total_started >= count:
                    return True
            time.sleep(0.01)
        return False

    @staticmethod
    def wait_for_peer_counts(server, expected, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with server._peer_lock:
                if server._peer_counts == expected:
                    return True
            time.sleep(0.01)
        return False

    def test_global_and_per_ip_admission_and_timeout_release(self):
        PartialHeaderHandler.reset()
        server = control_server.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), PartialHeaderHandler,
            max_workers=5, max_workers_per_ip=4, header_timeout=2)
        serving = threading.Thread(
            target=server.serve_forever, kwargs={'poll_interval': 0.01},
            daemon=True)
        serving.start()
        clients = []
        try:
            address = server.server_address
            for _index in range(4):
                client = socket.create_connection(address, timeout=2)
                client.sendall(b'GET / HTTP/1.1\r\nHost: partial')
                clients.append(client)
            self.assertTrue(
                PartialHeaderHandler.saturated.wait(3),
                'four partial-header clients did not occupy all worker slots')

            with PartialHeaderHandler.lock:
                self.assertEqual(PartialHeaderHandler.active, 4)
                self.assertEqual(PartialHeaderHandler.maximum_active, 4)
                self.assertEqual(PartialHeaderHandler.total_started, 4)
                self.assertEqual(PartialHeaderHandler.initial_timeouts, [2, 2, 2, 2])

            same_ip_excess = socket.create_connection(address, timeout=2)
            clients.append(same_ip_excess)
            same_ip_excess.sendall(b'GET / HTTP/1.1\r\nHost: same-ip-excess')
            self.assertTrue(
                self.connection_was_closed(same_ip_excess),
                'fifth same-IP connection was not closed at admission')
            time.sleep(0.1)
            with PartialHeaderHandler.lock:
                self.assertEqual(PartialHeaderHandler.total_started, 4)

            other_ip = socket.create_connection(
                address, timeout=2, source_address=('127.0.0.2', 0))
            clients.append(other_ip)
            other_ip.sendall(b'GET / HTTP/1.1\r\nHost: other-peer')
            self.assertTrue(
                self.wait_for_started(5),
                'different peer was not admitted into the remaining global slot')
            with PartialHeaderHandler.lock:
                self.assertEqual(PartialHeaderHandler.active, 5)
                self.assertEqual(PartialHeaderHandler.maximum_active, 5)
            with server._peer_lock:
                self.assertEqual(server._peer_counts, {
                    '127.0.0.1': 4,
                    '127.0.0.2': 1,
                })

            global_excess = socket.create_connection(
                address, timeout=2, source_address=('127.0.0.3', 0))
            clients.append(global_excess)
            global_excess.sendall(b'GET / HTTP/1.1\r\nHost: global-excess')
            self.assertTrue(
                self.connection_was_closed(global_excess),
                'connection beyond the global bound was not closed')
            with PartialHeaderHandler.lock:
                self.assertEqual(PartialHeaderHandler.total_started, 5)
                self.assertLessEqual(PartialHeaderHandler.maximum_active, 5)

            self.assertTrue(
                PartialHeaderHandler.idle.wait(5),
                'header timeouts did not return all worker slots')
            with PartialHeaderHandler.lock:
                self.assertEqual(PartialHeaderHandler.active, 0)
            self.assertTrue(self.wait_for_peer_counts(server, {}))

            admitted = socket.create_connection(address, timeout=2)
            clients.append(admitted)
            admitted.settimeout(3)
            admitted.sendall(
                b'GET / HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n')
            response = bytearray()
            while True:
                chunk = admitted.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            self.assertIn(b'HTTP/1.0 200', response)
            self.assertTrue(response.endswith(b'ok'))
            self.assertTrue(self.wait_for_started(6, timeout=2))
            self.assertTrue(PartialHeaderHandler.idle.wait(2))
            with PartialHeaderHandler.lock:
                self.assertEqual(PartialHeaderHandler.total_started, 6)
                self.assertLessEqual(PartialHeaderHandler.maximum_active, 5)
                self.assertEqual(PartialHeaderHandler.initial_timeouts[-1], 2)
            self.assertTrue(self.wait_for_peer_counts(server, {}))
        finally:
            for client in clients:
                try:
                    client.close()
                except OSError:
                    pass
            server.shutdown()
            serving.join(3)
            server.server_close()

    def test_malformed_header_releases_peer_quota_for_next_request(self):
        PartialHeaderHandler.reset()
        server = control_server.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), PartialHeaderHandler,
            max_workers=4, max_workers_per_ip=1, header_timeout=2)
        serving = threading.Thread(
            target=server.serve_forever, kwargs={'poll_interval': 0.01},
            daemon=True)
        serving.start()
        clients = []
        try:
            malformed = socket.create_connection(server.server_address, timeout=2)
            clients.append(malformed)
            malformed.settimeout(2)
            malformed.sendall(b'NOT-A-VALID-REQUEST\r\n\r\n')
            try:
                while malformed.recv(4096):
                    pass
            except (ConnectionResetError, ConnectionAbortedError):
                pass
            self.assertTrue(PartialHeaderHandler.idle.wait(3))
            self.assertTrue(self.wait_for_peer_counts(server, {}))

            valid = socket.create_connection(server.server_address, timeout=2)
            clients.append(valid)
            valid.settimeout(2)
            valid.sendall(
                b'GET / HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n')
            response = bytearray()
            while True:
                chunk = valid.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            self.assertIn(b'HTTP/1.0 200', response)
            self.assertTrue(PartialHeaderHandler.idle.wait(2))
            self.assertTrue(self.wait_for_peer_counts(server, {}))
        finally:
            for client in clients:
                try:
                    client.close()
                except OSError:
                    pass
            server.shutdown()
            serving.join(3)
            server.server_close()

    def test_trickle_headers_hit_absolute_deadline_and_release_timer_and_slot(self):
        PartialHeaderHandler.reset()
        server = control_server.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), PartialHeaderHandler,
            max_workers=4, max_workers_per_ip=1, header_timeout=2)
        serving = threading.Thread(
            target=server.serve_forever, kwargs={'poll_interval': 0.01},
            daemon=True)
        serving.start()
        clients = []
        try:
            trickle = socket.create_connection(server.server_address, timeout=2)
            clients.append(trickle)
            started = time.monotonic()
            trickle.sendall(b'GET / HTTP/1.1\r\nHost: slow\r\nX-Trickle: ')
            self.assertTrue(self.wait_for_started(1))

            # Every byte arrives well inside the two-second socket idle timeout.
            # Only the independent absolute framing timer should terminate it.
            closed = False
            while time.monotonic() - started < 4:
                time.sleep(0.25)
                try:
                    trickle.sendall(b'x')
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError, OSError):
                    closed = True
                    break
                if self.connection_was_closed(trickle):
                    closed = True
                    break
            elapsed = time.monotonic() - started
            self.assertTrue(closed, 'trickled request outlived its absolute deadline')
            self.assertGreaterEqual(elapsed, 1.5)
            self.assertLess(elapsed, 3.5)
            self.assertTrue(PartialHeaderHandler.idle.wait(2))
            self.assertTrue(self.wait_for_peer_counts(server, {}))
            with server._framing_lock:
                self.assertEqual(server._framing_timers, {})

            # A released per-peer and global slot must admit a complete request.
            valid = socket.create_connection(server.server_address, timeout=2)
            clients.append(valid)
            valid.settimeout(2)
            valid.sendall(
                b'GET / HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n')
            response = bytearray()
            while True:
                chunk = valid.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            self.assertIn(b'HTTP/1.0 200', response)
            self.assertTrue(PartialHeaderHandler.idle.wait(2))
            self.assertTrue(self.wait_for_peer_counts(server, {}))
            with server._framing_lock:
                self.assertEqual(server._framing_timers, {})
        finally:
            for client in clients:
                try:
                    client.close()
                except OSError:
                    pass
            server.shutdown()
            serving.join(3)
            server.server_close()


class BoundedServerReleaseTests(unittest.TestCase):
    def setUp(self):
        self.server = control_server.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), PartialHeaderHandler,
            max_workers=4, max_workers_per_ip=2, header_timeout=3)

    def tearDown(self):
        self.server.server_close()

    def assert_all_global_slots_released(self):
        acquired = []
        for _index in range(4):
            acquired.append(self.server._worker_slots.acquire(blocking=False))
        self.assertEqual(acquired, [True, True, True, True])
        self.assertFalse(self.server._worker_slots.acquire(blocking=False))
        for _index in range(4):
            self.server._worker_slots.release()

    def test_thread_spawn_failure_releases_global_and_peer_admission(self):
        request = object()
        address = ('192.0.2.10', 12345)
        with patch.object(
                control_server.ThreadingHTTPServer, 'process_request',
                side_effect=RuntimeError('thread start failed')):
            with self.assertRaisesRegex(RuntimeError, 'thread start failed'):
                self.server.process_request(request, address)
        with self.server._peer_lock:
            self.assertEqual(self.server._peer_counts, {})
        self.assert_all_global_slots_released()

    def test_malformed_tls_handshake_releases_global_and_peer_admission(self):
        events = []
        peer = '192.0.2.20'

        class MalformedTlsSocket:
            def settimeout(self, value):
                events.append(('timeout', value))

            def do_handshake(self):
                events.append(('handshake',))
                raise control_server.ssl.SSLError('malformed client hello')

            def shutdown(self, how):
                events.append(('shutdown', how))

            def close(self):
                events.append(('close',))

        self.assertTrue(self.server._worker_slots.acquire(blocking=False))
        with self.server._peer_lock:
            self.server._peer_counts[peer] = 1
        request = MalformedTlsSocket()
        with patch.object(control_server.ssl, 'SSLSocket', MalformedTlsSocket):
            self.server.process_request_thread(request, (peer, 12345))
        self.assertEqual(events[:2], [('timeout', 3), ('handshake',)])
        self.assertIn(('close',), events)
        with self.server._peer_lock:
            self.assertEqual(self.server._peer_counts, {})
        self.assert_all_global_slots_released()

    def test_framing_timer_start_failure_removes_registry_and_aborts_handler(self):
        events = []
        address = ('192.0.2.30', 12345)

        class Request:
            def settimeout(self, value):
                events.append(('timeout', value))

        class BrokenTimer:
            daemon = False

            def start(self):
                events.append(('timer-start',))
                raise RuntimeError('timer thread unavailable')

            def cancel(self):
                events.append(('timer-cancel',))

        request = Request()
        with (patch.object(control_server.threading, 'Timer',
                           return_value=BrokenTimer()),
              patch.object(control_server.ThreadingHTTPServer,
                           'finish_request') as base_finish):
            with self.assertRaisesRegex(RuntimeError, 'timer thread unavailable'):
                self.server.finish_request(request, address)
        base_finish.assert_not_called()
        self.assertEqual(events, [('timeout', 3), ('timer-start',)])
        with self.server._framing_lock:
            self.assertEqual(self.server._framing_timers, {})


class HandlerDeadlineTests(unittest.TestCase):
    def test_body_deadline_shutdowns_connection_and_timer_is_cancelled(self):
        events = []
        timers = []

        class Connection:
            def shutdown(self, how):
                events.append(('shutdown', how))

        class Reader:
            def read(self, length):
                events.append(('read', length))
                timers[0].callback()
                return b'x'

        class Timer:
            def __init__(self, interval, callback):
                self.interval = interval
                self.callback = callback
                self.daemon = False
                timers.append(self)

            def start(self):
                events.append(('start', self.interval, self.daemon))

            def cancel(self):
                events.append(('cancel',))

        handler = object.__new__(control_server.Handler)
        handler.connection = Connection()
        handler.rfile = Reader()
        with patch.object(control_server.threading, 'Timer', Timer):
            self.assertEqual(handler.read_body(2), b'x')
        self.assertEqual(events, [
            ('start', 40, True),
            ('read', 2),
            ('shutdown', socket.SHUT_RDWR),
            ('cancel',),
        ])


class BoundedServerConfigurationTests(unittest.TestCase):
    def test_constructor_refuses_invalid_worker_and_header_bounds(self):
        invalid_workers = (None, True, 4.0, 3, 33)
        for value in invalid_workers:
            with self.subTest(max_workers=value):
                with self.assertRaises(control_server.Refusal):
                    control_server.BoundedThreadingHTTPServer(
                        ('127.0.0.1', 0), PartialHeaderHandler,
                        max_workers=value, header_timeout=2)

        invalid_timeouts = (None, True, 2.0, 1, 21)
        for value in invalid_timeouts:
            with self.subTest(header_timeout=value):
                with self.assertRaises(control_server.Refusal):
                    control_server.BoundedThreadingHTTPServer(
                        ('127.0.0.1', 0), PartialHeaderHandler,
                        max_workers=4, header_timeout=value)

        invalid_per_ip = (None, True, 1.0, 0, 9)
        for value in invalid_per_ip:
            with self.subTest(max_workers_per_ip=value):
                with self.assertRaises(control_server.Refusal):
                    control_server.BoundedThreadingHTTPServer(
                        ('127.0.0.1', 0), PartialHeaderHandler,
                        max_workers=4, max_workers_per_ip=value,
                        header_timeout=2)
        with self.assertRaises(control_server.Refusal):
            control_server.BoundedThreadingHTTPServer(
                ('127.0.0.1', 0), PartialHeaderHandler,
                max_workers=4, max_workers_per_ip=5, header_timeout=2)
        maximum = control_server.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), PartialHeaderHandler,
            max_workers=32, max_workers_per_ip=8, header_timeout=20)
        maximum.server_close()

    def test_check_mode_validates_and_reports_http_bounds_without_serving(self):
        fake_application = SimpleNamespace(
            children={'team-sandbox': {}},
            administration=SimpleNamespace(manifests={'team-sandbox': {}}),
        )
        base = {
            'database': '/var/lib/firstmate-control/control.sqlite3',
            'listen_host': '127.0.0.1',
            'max_http_workers': 4,
            'max_http_workers_per_ip': 2,
            'header_timeout_seconds': 2,
        }

        for field, value in (
                ('max_http_workers', 3),
                ('max_http_workers', 33),
                ('max_http_workers', True),
                ('max_http_workers_per_ip', 0),
                ('max_http_workers_per_ip', 5),
                ('max_http_workers_per_ip', True),
                ('header_timeout_seconds', 1),
                ('header_timeout_seconds', 2.0)):
            with self.subTest(field=field, value=value):
                config = {**base, field: value}
                with (patch.object(control_server.sys, 'argv', [
                            'server.py', '--config', 'fixed.json', '--check']),
                      patch.object(control_server, 'load_config',
                                   return_value=(config, {})),
                      patch.object(control_server, 'validate_database',
                                   return_value=Path(base['database'])),
                      patch.object(control_server, 'Application',
                                   return_value=fake_application),
                      patch('builtins.print') as output):
                    with self.assertRaises(control_server.Refusal):
                        control_server.main()
                output.assert_not_called()

        with (patch.object(control_server.sys, 'argv', [
                    'server.py', '--config', 'fixed.json', '--check']),
              patch.object(control_server, 'load_config',
                           return_value=(base, {})),
              patch.object(control_server, 'validate_database',
                           return_value=Path(base['database'])),
              patch.object(control_server, 'Application',
                           return_value=fake_application),
              patch.object(control_server, 'BoundedThreadingHTTPServer') as http_server,
              patch('builtins.print') as output):
            control_server.main()
        http_server.assert_not_called()
        report = json.loads(output.call_args.args[0])
        self.assertEqual(report['max_http_workers'], 4)
        self.assertEqual(report['max_http_workers_per_ip'], 2)
        self.assertEqual(report['header_timeout_seconds'], 2)
        self.assertFalse(report['runtime_probes_performed'])

    def test_main_refuses_ipv6_loopback_for_ipv4_only_server(self):
        fake_application = SimpleNamespace(
            children={'team-sandbox': {}},
            administration=SimpleNamespace(manifests={}),
        )
        config = {
            'database': '/var/lib/firstmate-control/control.sqlite3',
            'listen_host': '::1',
            'max_http_workers': 4,
            'max_http_workers_per_ip': 2,
            'header_timeout_seconds': 2,
        }
        with (patch.object(control_server.sys, 'argv', [
                    'server.py', '--config', 'fixed.json', '--check']),
              patch.object(control_server, 'load_config',
                           return_value=(config, {})),
              patch.object(control_server, 'validate_database',
                           return_value=Path(config['database'])),
              patch.object(control_server, 'Application',
                           return_value=fake_application),
              patch.object(control_server, 'BoundedThreadingHTTPServer') as http_server,
              patch('builtins.print') as output):
            with self.assertRaisesRegex(control_server.Refusal, 'IPv4-only'):
                control_server.main()
        http_server.assert_not_called()
        output.assert_not_called()

    def test_plain_http_requires_one_concrete_rfc1918_bridge(self):
        fake_application = SimpleNamespace(
            children={'team-sandbox': {}},
            administration=SimpleNamespace(manifests={}),
        )
        base = {
            'database': '/var/lib/firstmate-control/control.sqlite3',
            'max_http_workers': 4,
            'max_http_workers_per_ip': 2,
            'header_timeout_seconds': 2,
            'allow_private_http': True,
        }

        for host in ('0.0.0.0', '8.8.8.8', '169.254.1.2', 'localhost'):
            with self.subTest(host=host), \
                    patch.object(control_server.sys, 'argv', [
                        'server.py', '--config', 'fixed.json', '--check']), \
                    patch.object(control_server, 'load_config',
                                 return_value=({**base, 'listen_host': host}, {})), \
                    patch.object(control_server, 'validate_database',
                                 return_value=Path(base['database'])), \
                    patch.object(control_server, 'Application',
                                 return_value=fake_application), \
                    patch('builtins.print') as output:
                with self.assertRaises(control_server.Refusal):
                    control_server.main()
                output.assert_not_called()

        private = {**base, 'listen_host': '172.17.0.1'}
        with (patch.object(control_server.sys, 'argv', [
                    'server.py', '--config', 'fixed.json', '--check']),
              patch.object(control_server, 'load_config',
                           return_value=(private, {})),
              patch.object(control_server, 'validate_database',
                           return_value=Path(base['database'])),
              patch.object(control_server, 'Application',
                           return_value=fake_application),
              patch('builtins.print') as output):
            control_server.main()
        self.assertEqual(json.loads(output.call_args.args[0])['configuration'], 'valid')


class BoundedServerTlsArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.server = control_server.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), PartialHeaderHandler,
            max_workers=4, header_timeout=7)

    def tearDown(self):
        self.server.server_close()

    def test_accept_wraps_tls_without_handshake(self):
        raw = object()
        wrapped = object()
        address = ('127.0.0.1', 12345)

        class Context:
            def __init__(self):
                self.calls = []

            def wrap_socket(self, request, **kwargs):
                self.calls.append((request, kwargs))
                return wrapped

        context = Context()
        self.server.ssl_context = context
        with patch.object(
                control_server.ThreadingHTTPServer, 'get_request',
                return_value=(raw, address)):
            result = self.server.get_request()
        self.assertEqual(result, (wrapped, address))
        self.assertEqual(context.calls, [(
            raw,
            {'server_side': True, 'do_handshake_on_connect': False},
        )])

    def test_finish_request_sets_timeout_then_handshakes_in_worker_path(self):
        events = []
        address = ('127.0.0.1', 12345)

        class FakeTlsSocket:
            def settimeout(self, value):
                events.append(('timeout', value))

            def do_handshake(self):
                events.append(('handshake',))

        request = FakeTlsSocket()

        def base_finish(_server, seen_request, seen_address):
            self.assertIs(seen_request, request)
            self.assertEqual(seen_address, address)
            events.append(('handler',))

        with (patch.object(control_server.ssl, 'SSLSocket', FakeTlsSocket),
              patch.object(
                  control_server.ThreadingHTTPServer, 'finish_request',
                  autospec=True, side_effect=base_finish)):
            self.server.finish_request(request, address)
        self.assertEqual(events, [
            ('timeout', 7),
            ('handshake',),
            ('handler',),
        ])


if __name__ == '__main__':
    unittest.main()
