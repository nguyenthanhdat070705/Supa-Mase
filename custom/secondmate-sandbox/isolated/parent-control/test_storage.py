"""Durability ordering and protected host SQLite path regression tests."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from common import Refusal
import local_ops
import server as control_server
from server import Application, validate_database


MIB = 1024**2
GIB = 1024**3
PARENT = {'role': 'parent', 'id': 'firstmate'}
CHILD = {'role': 'child', 'id': 'team-sandbox'}
OPERATOR = {'role': 'operator', 'id': 'captain-operator'}


class StorageTests(unittest.TestCase):
    def test_atomic_file_is_synced_before_rename_and_directory_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'receipt.json'
            events = []
            replace = os.replace
            def renamed(source, destination):
                events.append('rename')
                return replace(source, destination)
            with patch.object(local_ops.os, 'fsync', side_effect=lambda fd: events.append('file-fsync')), \
                    patch.object(local_ops.os, 'replace', side_effect=renamed), \
                    patch.object(local_ops, 'fsync_directory', side_effect=lambda path: events.append(('directory-fsync', path))):
                local_ops.atomic(target, b'complete')
            self.assertEqual(events, ['file-fsync', 'rename', ('directory-fsync', target.parent)])
            self.assertEqual(target.read_bytes(), b'complete')

    def test_new_parent_entries_are_synced_before_artifact_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            entries = []
            with patch.object(local_ops, 'fsync_directory', side_effect=entries.append):
                target = local_ops.confined(home, 'state/inbox/events/receipt.json', True)
            self.assertEqual(entries, [home, home / 'state', home / 'state/inbox'])
            self.assertTrue(target.parent.is_dir())

    def test_sqlite_rejects_unsafe_ancestors_db_and_recovery_sidecars(self):
        # Supply platform-neutral metadata so the Linux ownership policy is also
        # regression-tested on Windows without changing real ownership.
        database = Path(os.path.abspath('synthetic-state/control.sqlite3'))
        metadata = {part: SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_nlink=1)
                    for part in database.parents}
        metadata[database.parent] = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_nlink=1)
        metadata[database] = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
        def lookup(path):
            if path not in metadata:
                raise FileNotFoundError
            return metadata[path]
        with patch.object(Path, 'lstat', autospec=True, side_effect=lookup):
            self.assertEqual(validate_database(database), database)
            cases = [
                (database.parent.parent, stat.S_IFDIR | 0o777, 0, 1),
                (database.parent.parent, stat.S_IFLNK | 0o755, 0, 1),
                (database, stat.S_IFREG | 0o600, 1000, 1),
                (database, stat.S_IFREG | 0o600, 0, 2),
                (database, stat.S_IFREG | 0o644, 0, 1),
                (Path(str(database) + '-wal'), stat.S_IFLNK | 0o600, 0, 1),
                (Path(str(database) + '-shm'), stat.S_IFREG | 0o666, 0, 1),
                (database.parent / 'service.lock', stat.S_IFREG | 0o600, 1000, 1),
            ]
            for target, mode, owner, links in cases:
                previous = metadata.get(target)
                metadata[target] = SimpleNamespace(st_mode=mode, st_uid=owner, st_nlink=links)
                with self.subTest(path=target, mode=mode, owner=owner, links=links), self.assertRaises(Refusal):
                    validate_database(database)
                if previous is None:
                    del metadata[target]
                else:
                    metadata[target] = previous
            del metadata[database]
            self.assertEqual(validate_database(database), database)


class ControlDatabaseQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.apps = []

    def tearDown(self):
        for app in reversed(self.apps):
            app.db.close()
        self.temporary.cleanup()

    def config(self, name='control.sqlite3', **overrides):
        value = {
            'version': 1,
            'database': str(self.root / name),
            'parents': {'firstmate': {'can_approve': False}},
            'children': {
                'team-sandbox': {
                    'parent_id': 'firstmate',
                    'container': 'secondmate',
                    'home': '/home/nguye/team-sandbox',
                },
            },
            'operators': {},
        }
        value.update(overrides)
        return value

    def application(self, name='control.sqlite3', *, lifecycle=None,
                    administration=None, **overrides):
        app = Application(self.config(name, **overrides), {},
                          lifecycle=lifecycle, administration=administration)
        self.apps.append(app)
        return app

    def storage_paths(self, app):
        return [app.database_path, *[
            Path(str(app.database_path) + suffix)
            for suffix in ('-wal', '-shm', '-journal')
        ]]

    def mocked_storage(self, app, sizes, *, free=None, stat_error=None,
                       disk_error=None):
        sizes = {str(path): size for path, size in sizes.items()}

        def exists(path):
            return str(path) in sizes or str(path) == stat_error

        def metadata(path):
            if str(path) == stat_error:
                raise OSError('synthetic stat failure')
            return SimpleNamespace(st_size=sizes[str(path)])

        if disk_error is not None:
            disk = patch.object(control_server.shutil, 'disk_usage',
                                side_effect=disk_error)
        else:
            disk = patch.object(control_server.shutil, 'disk_usage',
                                return_value=SimpleNamespace(free=free))
        return (
            patch.object(Path, 'exists', autospec=True, side_effect=exists),
            patch.object(Path, 'stat', autospec=True, side_effect=metadata),
            disk,
        )

    def assert_storage_blocked(self, app, routes):
        for route in routes:
            with self.subTest(route=route), self.assertRaises(Refusal) as caught:
                app.handle('POST', route, PARENT, {})
            self.assertEqual(caught.exception.status, 507)
            self.assertEqual(caught.exception.code, 'storage_blocked')

    def test_database_quota_configuration_rejects_non_integer_and_out_of_range(self):
        invalid_maxima = [True, False, None, '268435456', 256 * MIB - 1,
                          4 * GIB + 1]
        for index, maximum in enumerate(invalid_maxima):
            with self.subTest(database_max_bytes=maximum), self.assertRaises(Refusal):
                Application(self.config('invalid-max-' + str(index) + '.sqlite3',
                                        database_max_bytes=maximum), {})

        invalid_reserves = [True, False, None, '1073741824', GIB - 1,
                            100 * GIB + 1]
        for index, reserve in enumerate(invalid_reserves):
            with self.subTest(database_reserve_bytes=reserve), self.assertRaises(Refusal):
                Application(self.config('invalid-reserve-' + str(index) + '.sqlite3',
                                        database_reserve_bytes=reserve), {})

        minimum = self.application('minimum.sqlite3', database_max_bytes=256 * MIB,
                                   database_reserve_bytes=GIB)
        maximum = self.application('maximum.sqlite3', database_max_bytes=4 * GIB,
                                   database_reserve_bytes=100 * GIB)
        self.assertEqual((minimum.database_max_bytes, minimum.database_reserve_bytes),
                         (256 * MIB, GIB))
        self.assertEqual((maximum.database_max_bytes, maximum.database_reserve_bytes),
                         (4 * GIB, 100 * GIB))

    def test_sqlite_has_bounded_page_journal_and_wal_pragmas(self):
        app = self.application(database_max_bytes=256 * MIB,
                               database_reserve_bytes=GIB)
        page_size = app.db.execute('PRAGMA page_size').fetchone()[0]
        self.assertEqual(app.db.execute('PRAGMA max_page_count').fetchone()[0],
                         app.database_max_bytes // page_size)
        self.assertEqual(app.db.execute('PRAGMA journal_size_limit').fetchone()[0],
                         min(64 * MIB, app.database_max_bytes // 8))
        self.assertEqual(app.db.execute('PRAGMA wal_autocheckpoint').fetchone()[0],
                         1000)

    def test_all_post_routes_block_on_combined_database_and_sidecar_ceiling_but_get_works(self):
        app = self.application(database_max_bytes=256 * MIB,
                               database_reserve_bytes=GIB)
        # Sixty-four admitted maximum-size request bodies are reserved. MAX_BODY
        # is a decimal two million bytes, so this is exactly 128,000,000 bytes.
        self.assertEqual(64 * control_server.MAX_BODY, 128_000_000)
        paths = self.storage_paths(app)
        # Each individual file fits; their combined size exceeds the remaining
        # allowance by one byte, proving WAL/SHM/rollback journals are counted.
        admitted = app.database_max_bytes - 64 * control_server.MAX_BODY
        sizes = {path: admitted // 4 for path in paths}
        sizes[paths[-1]] += admitted - sum(sizes.values()) + 1
        mocks = self.mocked_storage(
            app, sizes,
            free=app.database_reserve_bytes + 64 * control_server.MAX_BODY + GIB)
        routes = [
            '/v1/children/team-sandbox/brain',
            '/v1/children/team-sandbox/reports',
            '/v1/escalations',
            '/v1/data/query',
        ]
        with mocks[0], mocks[1], mocks[2] as disk_usage:
            self.assert_storage_blocked(app, routes)
            before = disk_usage.call_count
            listing = app.handle('GET', '/v1/children', PARENT)
            self.assertEqual([item['child_id'] for item in listing['children']],
                             ['team-sandbox'])
            self.assertEqual(disk_usage.call_count, before)

    def test_post_blocks_when_filesystem_free_headroom_would_cross_reserve(self):
        app = self.application(database_max_bytes=256 * MIB,
                               database_reserve_bytes=GIB)
        headroom = 64 * control_server.MAX_BODY
        mocks = self.mocked_storage(
            app, {app.database_path: 0},
            free=app.database_reserve_bytes + headroom - 1)
        with mocks[0], mocks[1], mocks[2]:
            self.assert_storage_blocked(app, ['/v1/children/team-sandbox/brain'])
            self.assertEqual(app.handle('GET', '/v1/children', PARENT)['children'][0]['child_id'],
                             'team-sandbox')

    def test_soft_pressure_preserves_containment_but_blocks_ingest_and_backup(self):
        class Lifecycle:
            def __init__(self):
                self.calls = []

            def execute(self, child, action, generation, recovery=False):
                self.calls.append((child['container'], action, generation, recovery))
                return {'action': action, 'generation': 'g2'}

        class Administration:
            enabled = True
            manifests = {'team-sandbox': {}}

            def __init__(self):
                self.calls = []

            def authorize(self, principal, child_id, child):
                self.calls.append(('authorize', principal['role'], child_id))

            def lifecycle_gate(self, child_id, child, action):
                self.calls.append(('lifecycle-gate', child_id, action))

            def operation(self, principal, child_id, child, payload, db, lock,
                          child_lock):
                self.calls.append(('operation', principal['role'], payload['action'],
                                   payload['parameters']))
                # Model the durable write performed by the real implementation,
                # so this proves the emergency tier permits journaling as well as
                # dispatch under soft pressure.
                stored = {**payload, 'principal': principal, 'child_id': child_id}
                with lock, db:
                    db.execute('INSERT INTO admin_operations VALUES (?,?,?,?,?,?)', (
                        payload['operation_id'], child_id, '0' * 64,
                        control_server.canonical(stored).decode(), 'complete',
                        control_server.canonical({'profile': 'restricted'}).decode()))
                return {'operation_id': payload['operation_id'], 'state': 'complete'}

        lifecycle = Lifecycle()
        administration = Administration()
        app = self.application(
            lifecycle=lifecycle, administration=administration,
            database_max_bytes=256 * MIB, database_reserve_bytes=GIB,
            operators={'captain-operator': {
                'children': ['team-sandbox'], 'allow_recovery': True,
            }})
        headroom = 64 * control_server.MAX_BODY
        emergency = 8 * control_server.MAX_BODY
        self.assertGreater(headroom, emergency)
        # This fits the emergency budget but not the ordinary intake budget.
        used = app.database_max_bytes - 32 * control_server.MAX_BODY
        sizes = {app.database_path: used}
        free = app.database_reserve_bytes + headroom + GIB
        mocks = self.mocked_storage(app, sizes, free=free)
        with mocks[0], mocks[1], mocks[2]:
            for route, principal in (
                    ('/v1/children/team-sandbox/reports', CHILD),
                    ('/v1/children/team-sandbox/requests', PARENT)):
                with self.subTest(route=route), self.assertRaises(Refusal) as caught:
                    app.handle('POST', route, principal, {})
                self.assertEqual((caught.exception.status, caught.exception.code),
                                 (507, 'storage_blocked'))
            backup = {
                'operation_id': str(uuid.uuid4()),
                'action': 'backup',
                'expected_generation': 'g1',
                'parameters': {},
            }
            with self.assertRaises(Refusal) as caught:
                app.handle(
                    'POST', '/v1/children/team-sandbox/admin/operations',
                    PARENT, backup)
            self.assertEqual((caught.exception.status, caught.exception.code),
                             (507, 'storage_blocked'))

            for principal in (PARENT, OPERATOR):
                control = {
                    'operation_id': str(uuid.uuid4()),
                    'action': 'stop',
                    'expected_generation': 'g1',
                }
                result = app.handle(
                    'POST', '/v1/children/team-sandbox/control', principal,
                    control)
                self.assertEqual(result['state'], 'complete')

            resource = {
                'operation_id': str(uuid.uuid4()),
                'action': 'resource-profile',
                'expected_generation': 'g1',
                'parameters': {'profile': 'restricted'},
            }
            result = app.handle(
                'POST', '/v1/children/team-sandbox/admin/operations',
                OPERATOR, resource)
            self.assertEqual(result['state'], 'complete')

        self.assertEqual(
            [call[1] for call in lifecycle.calls], ['stop', 'stop'])
        row = app.db.execute(
            'SELECT state FROM admin_operations WHERE operation_id=?',
            (resource['operation_id'],)).fetchone()
        self.assertEqual(row['state'], 'complete')

    def test_hard_pressure_blocks_emergency_control_and_resource_operations(self):
        class Lifecycle:
            def __init__(self):
                self.calls = []

            def execute(self, *args):
                self.calls.append(args)
                return {'unexpected': True}

        class Administration:
            enabled = True
            manifests = {'team-sandbox': {}}

            def __init__(self):
                self.calls = []

            def authorize(self, *args):
                self.calls.append(('authorize', args))

            def lifecycle_gate(self, *args):
                self.calls.append(('lifecycle', args))

            def operation(self, *args):
                self.calls.append(('operation', args))
                return {'unexpected': True}

        lifecycle = Lifecycle()
        administration = Administration()
        app = self.application(
            lifecycle=lifecycle, administration=administration,
            database_max_bytes=256 * MIB, database_reserve_bytes=GIB)
        emergency = 8 * control_server.MAX_BODY
        conditions = (
            ('byte-cap',
             {app.database_path: app.database_max_bytes - emergency + 1},
             app.database_reserve_bytes + 64 * control_server.MAX_BODY + GIB),
            ('filesystem-reserve', {app.database_path: 0},
             app.database_reserve_bytes + emergency - 1),
        )
        for condition, sizes, free in conditions:
            mocks = self.mocked_storage(app, sizes, free=free)
            control = {
                'operation_id': str(uuid.uuid4()),
                'action': 'stop',
                'expected_generation': 'g1',
            }
            resource = {
                'operation_id': str(uuid.uuid4()),
                'action': 'resource-profile',
                'expected_generation': 'g1',
                'parameters': {'profile': 'restricted'},
            }
            with mocks[0], mocks[1], mocks[2]:
                for route, payload in (
                        ('/v1/children/team-sandbox/control', control),
                        ('/v1/children/team-sandbox/admin/operations', resource)):
                    with self.subTest(condition=condition, route=route), \
                            self.assertRaises(Refusal) as caught:
                        app.handle('POST', route, PARENT, payload)
                    self.assertEqual((caught.exception.status, caught.exception.code),
                                     (507, 'storage_blocked'))
        self.assertEqual(lifecycle.calls, [])
        self.assertEqual(administration.calls, [])

    def test_disk_usage_and_stat_errors_fail_closed_while_get_remains_available(self):
        app = self.application(database_max_bytes=256 * MIB,
                               database_reserve_bytes=GIB)
        generous = app.database_reserve_bytes + 64 * control_server.MAX_BODY + GIB
        paths = self.storage_paths(app)

        mocks = self.mocked_storage(
            app, {app.database_path: 0}, free=generous,
            stat_error=str(paths[1]))
        with mocks[0], mocks[1], mocks[2] as disk_usage:
            self.assert_storage_blocked(app, ['/v1/children/team-sandbox/brain'])
            disk_usage.assert_not_called()

        mocks = self.mocked_storage(
            app, {app.database_path: 0},
            disk_error=OSError('synthetic disk usage failure'))
        with mocks[0], mocks[1], mocks[2]:
            self.assert_storage_blocked(app, ['/v1/children/team-sandbox/brain'])
            self.assertEqual(app.handle('GET', '/v1/children', PARENT)['children'][0]['child_id'],
                             'team-sandbox')


if __name__ == '__main__':
    unittest.main()
