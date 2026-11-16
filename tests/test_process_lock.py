import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from maintenance.core.process_lock import process_lock


class ProcessLockTests(unittest.TestCase):
    def attempt(self, path):
        code = "from maintenance.core.process_lock import process_lock,AlreadyRunning\nimport sys\ntry:\n with process_lock(sys.argv[1]): pass\nexcept AlreadyRunning: sys.exit(2)"
        return subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, timeout=10
        ).returncode

    def test_second_process_denied_then_released_despite_marker_remaining(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "worker.lock"
            with process_lock(path):
                self.assertEqual(self.attempt(path), 2)
            self.assertTrue(path.exists())
            self.assertEqual(self.attempt(path), 0)

    def test_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "worker.lock"
            with self.assertRaises(ValueError):
                with process_lock(path):
                    raise ValueError("test")
            self.assertEqual(self.attempt(path), 0)
