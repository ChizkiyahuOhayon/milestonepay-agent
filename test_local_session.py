from pathlib import Path
import stat
import tempfile
import unittest
from local_session import load_session


class LocalSessionTests(unittest.TestCase):
    def test_identity_survives_reload_and_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_id"
            self.assertEqual(load_session(path), load_session(path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_different_local_directories_do_not_share_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertNotEqual(load_session(Path(directory) / "a"), load_session(Path(directory) / "b"))


if __name__ == "__main__":
    unittest.main()
