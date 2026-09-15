"""Durability ordering and protected host SQLite path regression tests."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common import Refusal
import local_ops
from server import validate_database


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


if __name__ == '__main__':
    unittest.main()
