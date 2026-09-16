import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import token_helper


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "token"
        self.path.write_bytes(b"synthetic-only\n")
        self.path.chmod(0o600)

    def read(self):
        # Only ancestors are substituted: temporary test storage is under /tmp,
        # while production must reject world-writable ancestors.
        original = Path.lstat
        def info(path):
            if path in self.path.parents:
                return mock.Mock(st_mode=0o40755, st_uid=0)
            return original(path)
        with mock.patch("token_helper.os.geteuid", return_value=0), \
                mock.patch.object(Path, "lstat", info):
            return token_helper.read_token(self.path)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "Linux root fixture")
    def test_plain_token_and_permissions(self):
        self.assertEqual(self.read(), b"synthetic-only")
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.read()

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "Linux root fixture")
    def test_links_and_malformed_input(self):
        alias = self.path.with_name("alias")
        os.link(self.path, alias)
        with self.assertRaises(ValueError):
            self.read()
        alias.unlink()
        for raw in (b"", b"two lines\nvalue", b"secret\r\n", b"x" * 4097):
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.read()
        self.path.unlink()
        self.path.symlink_to("/dev/null")
        with self.assertRaises(OSError):
            self.read()

    def test_unprivileged_and_untrusted_ancestors(self):
        with mock.patch("token_helper.os.geteuid", return_value=1000):
            with self.assertRaises(ValueError):
                token_helper.read_token(self.path)
        with mock.patch("token_helper.os.geteuid", return_value=0), \
                mock.patch.object(Path, "lstat", return_value=mock.Mock(
                    st_mode=0o40777, st_uid=0)):
            with self.assertRaises(ValueError):
                token_helper.read_token(self.path)


if __name__ == "__main__":
    unittest.main()
