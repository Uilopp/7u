import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import setup_android_signing


class AndroidSigningTests(unittest.TestCase):
    def test_refuses_to_overwrite_existing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keystore = root / "data/release-signing/7u-release.p12"
            keystore.parent.mkdir(parents=True)
            keystore.write_bytes(b"keep existing identity")
            with patch.object(setup_android_signing, "ROOT", root):
                with self.assertRaises(SystemExit):
                    setup_android_signing.main()
            self.assertEqual(keystore.read_bytes(), b"keep existing identity")

    def test_creates_matching_backup_without_printing_password(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app/android").mkdir(parents=True)
            output = io.StringIO()
            with (
                patch.object(setup_android_signing, "ROOT", root),
                patch.object(setup_android_signing.shutil, "which", return_value="keytool"),
                patch.object(setup_android_signing.secrets, "token_urlsafe", return_value="private-password"),
                patch.object(
                    setup_android_signing.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                ) as run,
                contextlib.redirect_stdout(output),
            ):
                setup_android_signing.main()
            primary = root / "app/android/key.properties"
            backup = root / "data/release-signing/key.properties"
            self.assertEqual(primary.read_bytes(), backup.read_bytes())
            self.assertIn("storePassword=private-password", primary.read_text())
            self.assertNotIn("private-password", output.getvalue())
            self.assertNotIn("private-password", " ".join(run.call_args.args[0]))
            self.assertEqual(json.loads(output.getvalue())["status"], "created")

    def test_keytool_failure_does_not_publish_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app/android").mkdir(parents=True)
            with (
                patch.object(setup_android_signing, "ROOT", root),
                patch.object(setup_android_signing.shutil, "which", return_value="keytool"),
                patch.object(
                    setup_android_signing.subprocess, "run", return_value=SimpleNamespace(returncode=1)
                ),
            ):
                with self.assertRaises(SystemExit):
                    setup_android_signing.main()
            self.assertFalse((root / "app/android/key.properties").exists())


if __name__ == "__main__":
    unittest.main()
