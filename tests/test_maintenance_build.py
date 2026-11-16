import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance import build
from maintenance.model_config import ModelConfig


class BuildBackupTests(unittest.TestCase):
    def test_assets_are_backed_up_before_sync_and_survive_later_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "app/assets/memory"
            assets.mkdir(parents=True)
            (assets / "memory_pack.db").write_bytes(b"previous packaged database")
            (assets / "daily_memories.json").write_bytes(b"previous daily pool")
            (root / "app/pubspec.yaml").write_text("version: 0.1.260926+3\n")

            def synchronize(**kwargs):
                backups = list((root / "data/backups").glob("before-build-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual((backups[0] / "memory_pack.db").read_bytes(), b"previous packaged database")
                return {"status": "completed", "voice_failed": 0}

            with (
                patch.object(build, "load_model_config", return_value=ModelConfig("private-test-key-123")),
                patch.object(build, "run_sync", side_effect=synchronize),
                patch.object(build, "build_release", side_effect=RuntimeError("curation failed")),
            ):
                with self.assertRaisesRegex(build.ReleaseError, "curation failed"):
                    build.run_build(root, version_name="0.1.260927", emit=lambda _: None)
            backup = next((root / "data/backups").glob("before-build-*"))
            self.assertEqual((backup / "daily_memories.json").read_bytes(), b"previous daily pool")
            self.assertEqual((backup / "pubspec.yaml").read_text(), "version: 0.1.260926+3\n")
            self.assertFalse((root / "releases").exists())

    def test_legacy_release_command_delegates_to_complete_workflow(self):
        from maintenance import release

        with patch.object(build, "main", return_value=17) as main:
            self.assertEqual(release.main(), 17)
        main.assert_called_once_with()

    def test_cli_has_no_prepared_pool_or_lock_bypass(self):
        for arguments in (["--prepared-pool-sha256", "a" * 64], ["--maintenance-lock-held"]):
            with (
                self.subTest(arguments=arguments),
                patch.object(build, "run_build") as run,
                patch("sys.stderr"),
                self.assertRaises(SystemExit),
            ):
                build.main(arguments)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
