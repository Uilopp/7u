import base64
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from maintenance import release
from maintenance.core.process_lock import process_lock
from maintenance.model_config import ModelConfig


class MaintenanceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = self.root / "app"
        (self.app / "android/app").mkdir(parents=True)
        self.pubspec = self.app / "pubspec.yaml"
        self.original = "name: couple_app\nversion: 1.0.0+2 # private\nenvironment:\n  sdk: ^3.13.2\n"
        self.pubspec.write_text(self.original, encoding="utf-8")
        signing = self.root / "data/release-signing"
        signing.mkdir(parents=True)
        (signing / "7u-release.p12").write_bytes(b"test permanent key")
        self.properties = self.app / "android/key.properties"
        self.properties.write_text(
            "storeFile=../../../data/release-signing/7u-release.p12\n"
            "storePassword=permanent-test-password\nkeyPassword=permanent-test-password\n"
            "keyAlias=seven-u-release\n",
            encoding="utf-8",
        )
        self.apksigner = self.root / "sdk/build-tools/37.0.0/apksigner.bat"
        self.apksigner.parent.mkdir(parents=True)
        self.apksigner.write_text("mock signer")
        self.apksigner.with_name("aapt.exe" if os.name == "nt" else "aapt").write_text("mock aapt")
        self.flutter = self.root / "flutter sdk/bin/flutter.bat"
        self.flutter.parent.mkdir(parents=True)
        self.flutter.write_text("mock flutter")
        self.key = "sk-" + "a" * 24
        self.config = ModelConfig(self.key)
        # UTC Jan 1 is already Jan 2 in Beijing; release must use archive date.
        self.latest = int(datetime.fromisoformat("2024-01-01T16:01:00+00:00").timestamp())
        self.pack = {
            "messages": 1234,
            "voice_transcripts": 45,
            "links": 89,
            "latest_message_ts": self.latest,
            "bytes": 17000,
        }
        self.calls = []
        self.captured_configs = []
        self.log = []
        self.name = ""
        self.number = ""
        self.loader = self.patch("load_model_config", return_value=self.config)
        self.builder = self.patch("build_memory_pack", return_value=self.pack)
        self.daily_pool = self.patch("build_daily_pool", return_value={"status": "cached", "items": 60})
        self.patch("_tool_paths", return_value=(self.flutter, self.apksigner))
        self.certificate = self.patch("_configured_certificate", return_value="a" * 64)
        self.runner = self.patch("_run", side_effect=self.run_command)

    def patch(self, name, **kwargs):
        patcher = patch.object(release, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_command(self, command, *, cwd, env, emit):
        self.calls.append(command)
        self.assertEqual(cwd, self.app)
        self.assertEqual(env["SEVENU_ALLOW_DEBUG_SIGNING"], "false")
        self.assertNotIn("ORG_GRADLE_PROJECT_allowDebugSigning", env)
        self.assertNotIn(self.key, " ".join(command))
        if command[0] == str(self.flutter):
            self.name = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--build-name="))
            self.number = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--build-number="))
            defines = Path(
                next(arg.split("=", 1)[1] for arg in command if arg.startswith("--dart-define-from-file="))
            )
            configuration = json.loads(defines.read_text())
            self.captured_configs.append(configuration)
            emit("diagnostic " + configuration["DEEPSEEK_API_KEY"])
            emit(base64.b64encode(f"DEEPSEEK_API_KEY={configuration['DEEPSEEK_API_KEY']}".encode()).decode())
            emit("permanent-test-password")
            apk = self.app / "build/app/outputs/flutter-apk/app-release.apk"
            apk.parent.mkdir(parents=True, exist_ok=True)
            apk.write_bytes(f"APK {self.name}+{self.number}".encode())
            return "built"
        if Path(command[0]).name.startswith("aapt"):
            historical = re.fullmatch(r"7u-(.+)\+(\d+)\.apk", Path(command[-1]).name)
            if historical:
                return f"package: name='com.sevenu.app' versionCode='{historical[2]}' versionName='{historical[1]}'"
            return f"package: name='com.sevenu.app' versionCode='{self.number}' versionName='{self.name}'"
        return "Signer #1 certificate DN: CN=7u\nSigner #1 certificate SHA-256 digest: " + "a" * 64

    def build(self):
        return release.build_release(self.root, emit=self.log.append)

    def test_release_uses_archive_beijing_date_and_hides_key(self):
        result = self.build()
        self.assertEqual(result["version_name"], "1.0.240102")
        self.assertEqual(result["version_code"], 3)
        apk = Path(result["apk"])
        self.assertEqual(apk.name, "7u-1.0.240102+3.apk")
        self.assertEqual(result["sha256"], hashlib.sha256(apk.read_bytes()).hexdigest())
        self.assertIn("version: 1.0.240102+3 # private", self.pubspec.read_text())
        self.assertEqual(result["memory"], self.pack)
        self.assertEqual(self.captured_configs[0]["DEEPSEEK_API_KEY"], self.key)
        self.assertEqual(self.loader.call_args.args, (self.root / "data/model_config.dpapi",))
        self.builder.assert_called_once_with(
            self.root / "data/couple_chat.db", self.app / "assets/memory/memory_pack.db"
        )
        self.daily_pool.assert_called_once_with(
            self.root / "data/couple_chat.db",
            self.app / "assets/memory/daily_memories.json",
            config=self.config,
            fresh=True,
            emit=self.daily_pool.call_args.kwargs["emit"],
        )
        self.assertNotIn(self.key, "\n".join(self.log))
        self.assertNotIn("permanent-test-password", "\n".join(self.log))
        self.assertNotIn(
            base64.b64encode(f"DEEPSEEK_API_KEY={self.key}".encode()).decode(), "\n".join(self.log)
        )
        self.assertEqual(list((self.root / "data").glob(".release-*")), [])

    def test_same_day_releases_increment_code_and_reread_current_key(self):
        first = self.build()
        first_bytes = Path(first["apk"]).read_bytes()
        replacement_key = "sk-" + "b" * 24
        self.loader.return_value = ModelConfig(replacement_key)
        second = self.build()
        self.assertEqual(second["version_name"], first["version_name"])
        self.assertEqual(second["version_code"], first["version_code"] + 1)
        self.assertEqual(Path(first["apk"]).read_bytes(), first_bytes)
        self.assertEqual(self.captured_configs[-1]["DEEPSEEK_API_KEY"], replacement_key)
        self.assertNotIn(replacement_key, "\n".join(self.log))

    def test_full_version_is_preserved_even_if_date_differs_from_archive(self):
        result = release.build_release(self.root, version_name="2.3.260927", emit=self.log.append)
        self.assertEqual(result["version_name"], "2.3.260927")
        self.assertEqual(result["version_code"], 3)
        self.assertIn("version: 2.3.260927+3 # private", self.pubspec.read_text())

    def test_software_only_version_uses_archive_date(self):
        result = release.build_release(self.root, software_version="2.3", emit=self.log.append)
        self.assertEqual(result["version_name"], "2.3.240102")

    def test_invalid_version_is_rejected_before_any_build_or_data_work(self):
        for value in ("0.1.260229", "0.1.260932", "0.1.260927+3", "01.1.260927", "", "0.1.260927\n"):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.build_release(self.root, version_name=value)
        self.loader.assert_not_called()
        self.builder.assert_not_called()
        self.daily_pool.assert_not_called()

    def test_prepared_pool_must_match_reviewed_hash_before_and_after_build(self):
        digest = "b" * 64
        with patch.object(
            release, "validate_prepared_pool", return_value={"sha256": digest, "items": 60}
        ) as validator:
            result = release.build_release(self.root, prepared_pool_sha256=digest, emit=self.log.append)
        self.assertEqual(validator.call_count, 2)
        self.daily_pool.assert_not_called()
        self.assertEqual(result["daily_pool"]["sha256"], digest)

    def test_prepared_pool_change_during_build_prevents_publish(self):
        with patch.object(
            release, "validate_prepared_pool", side_effect=[{"sha256": "b" * 64}, {"sha256": "c" * 64}]
        ):
            with self.assertRaisesRegex(release.ReleaseError, "发生变化"):
                release.build_release(self.root, prepared_pool_sha256="b" * 64, emit=self.log.append)
        self.assertEqual(list((self.root / "releases").glob("*.apk")), [])
        self.assertEqual(self.pubspec.read_text(), self.original)

    def test_prepared_hash_mismatch_stops_before_flutter(self):
        with patch.object(release, "validate_prepared_pool", return_value={"sha256": "c" * 64}):
            with self.assertRaisesRegex(release.ReleaseError, "不是人工审查"):
                release.build_release(self.root, prepared_pool_sha256="b" * 64, emit=self.log.append)
        self.runner.assert_not_called()
        self.daily_pool.assert_not_called()

    def test_old_generated_apk_cannot_stand_in_for_a_new_successful_build(self):
        apk = self.app / "build/app/outputs/flutter-apk/app-release.apk"
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b"old output with same version from a failed previous run")
        self.runner.side_effect = lambda *args, **kwargs: ""
        with self.assertRaisesRegex(release.ReleaseError, "没有生成有效 APK"):
            self.build()
        self.assertEqual(list((self.root / "releases").glob("*.apk")), [])
        self.assertEqual(self.pubspec.read_text(), self.original)

    def test_failed_daily_pool_cannot_publish_or_update_version(self):
        self.daily_pool.side_effect = RuntimeError("daily memory validation failed")
        with self.assertRaisesRegex(release.ReleaseError, "daily memory"):
            self.build()
        self.assertEqual(self.pubspec.read_text(), self.original)
        self.assertEqual(list((self.root / "releases").glob("*.apk")), [])
        self.runner.assert_not_called()

    def test_existing_release_codes_win_over_pubspec_even_other_version_names(self):
        existing = self.root / "releases/7u-9.0.230101+42.apk"
        existing.parent.mkdir()
        existing.write_bytes(b"existing formal package")
        result = self.build()
        self.assertEqual(result["version_code"], 43)
        self.assertEqual(existing.read_bytes(), b"existing formal package")

    def test_empty_memory_does_not_use_today_as_version(self):
        self.builder.return_value = {**self.pack, "latest_message_ts": None}
        with self.assertRaisesRegex(release.ReleaseError, "没有可读聊天日期"):
            self.build()
        self.runner.assert_not_called()
        self.assertEqual(self.pubspec.read_text(), self.original)

    def test_build_failure_keeps_old_package_and_version_and_redacts_error(self):
        existing = self.root / "releases/7u-1.0.240102+2.apk"
        existing.parent.mkdir()
        existing.write_bytes(b"existing package")

        def fail(command, **kwargs):
            self.run_command(command, **kwargs)
            raise RuntimeError("failed " + self.key + " permanent-test-password")

        self.runner.side_effect = fail
        with self.assertRaises(release.ReleaseError) as caught:
            self.build()
        self.assertNotIn(self.key, str(caught.exception))
        self.assertNotIn("permanent-test-password", str(caught.exception))
        self.assertEqual(self.pubspec.read_text(), self.original)
        self.assertEqual(list(existing.parent.glob("*.apk")), [existing])
        self.assertEqual(existing.read_bytes(), b"existing package")
        self.assertEqual(list((self.root / "data").glob(".release-*")), [])

    def test_debug_or_missing_signing_is_rejected_before_data_rebuild(self):
        self.properties.write_text(self.properties.read_text().replace("seven-u-release", "androiddebugkey"))
        with self.assertRaisesRegex(release.ReleaseError, "debug"):
            self.build()
        self.builder.assert_not_called()
        self.properties.unlink()
        with self.assertRaisesRegex(release.ReleaseError, "永久签名"):
            self.build()
        self.runner.assert_not_called()

    def test_debug_certificate_and_stale_apk_are_not_published(self):
        for failure in ("debug", "stale"):
            with self.subTest(failure=failure):

                def fake(command, **kwargs):
                    result = self.run_command(command, **kwargs)
                    if failure == "debug" and command[0] == str(self.apksigner):
                        return result.replace("CN=7u", "CN=Android Debug")
                    if failure == "stale" and Path(command[0]).name.startswith("aapt"):
                        return result.replace("versionCode='3'", "versionCode='2'")
                    return result

                self.runner.side_effect = fake
                with self.assertRaises(release.ReleaseError):
                    self.build()
                self.assertEqual(self.pubspec.read_text(), self.original)
                self.assertEqual(list((self.root / "releases").glob("*.apk")), [])

    def test_final_publish_race_cannot_overwrite_existing_apk(self):
        competitor = self.root / "releases/7u-1.0.240102+3.apk"

        def race(command, **kwargs):
            result = self.run_command(command, **kwargs)
            if command[0] == str(self.flutter):
                competitor.parent.mkdir(exist_ok=True)
                competitor.write_bytes(b"other completed package")
            return result

        self.runner.side_effect = race
        with self.assertRaises(release.ReleaseError):
            self.build()
        self.assertEqual(competitor.read_bytes(), b"other completed package")
        self.assertEqual(self.pubspec.read_text(), self.original)
        self.assertEqual(list(competitor.parent.glob("*.pending")), [])

    def test_common_maintenance_lock_blocks_publish_and_releases_after_failure(self):
        with process_lock(self.root / "data/maintenance.lock"):
            with self.assertRaisesRegex(release.ReleaseError, "维护或发布正在运行"):
                self.build()
        self.runner.assert_not_called()
        self.assertEqual(self.build()["status"], "released")

    def test_internal_packaging_uses_coordinator_lock_without_reentering(self):
        with process_lock(self.root / "data/maintenance.lock"):
            result = release.build_release(self.root, _maintenance_lock_held=True, emit=self.log.append)
        self.assertEqual(result["status"], "released")

    def test_version_write_failure_keeps_valid_artifact_and_next_code_advances(self):
        with patch.object(release, "_save_version", side_effect=OSError("read only")):
            first = self.build()
        self.assertFalse(first["pubspec_updated"])
        self.assertTrue(Path(first["apk"]).is_file())
        self.assertEqual(self.pubspec.read_text(), self.original)
        second = self.build()
        self.assertEqual(second["version_code"], 4)

    def test_new_certificate_cannot_replace_existing_release_identity(self):
        first = self.build()
        original_bytes = Path(first["apk"]).read_bytes()
        previous_pubspec = self.pubspec.read_text()
        original_runner = self.run_command

        def changed_certificate(command, **kwargs):
            result = original_runner(command, **kwargs)
            if command[0] == str(self.apksigner) and Path(command[-1]).parent != self.root / "releases":
                return result.replace("a" * 64, "b" * 64)
            return result

        self.runner.side_effect = changed_certificate
        with self.assertRaisesRegex(release.ReleaseError, "永久发布证书不一致"):
            self.build()
        self.assertEqual(Path(first["apk"]).read_bytes(), original_bytes)
        self.assertEqual(self.pubspec.read_text(), previous_pubspec)
        self.assertEqual(len(list((self.root / "releases").glob("*.apk"))), 1)

    def test_first_release_must_match_the_configured_keystore(self):
        self.certificate.return_value = "b" * 64
        with self.assertRaisesRegex(release.ReleaseError, "永久发布证书不一致"):
            self.build()
        self.assertEqual(self.pubspec.read_text(), self.original)
        self.assertEqual(list((self.root / "releases").glob("*.apk")), [])

    def test_history_version_is_read_from_apk_not_its_filename(self):
        existing = self.root / "releases/7u-9.0.230101+999.apk"
        existing.parent.mkdir()
        existing.write_bytes(b"fixture whose real version is 5")
        original_runner = self.run_command

        def real_metadata(command, **kwargs):
            result = original_runner(command, **kwargs)
            if Path(command[0]).name.startswith("aapt") and Path(command[-1]) == existing:
                return "package: name='com.sevenu.app' versionCode='5' versionName='1.0.230101'"
            return result

        self.runner.side_effect = real_metadata
        result = self.build()
        self.assertEqual(result["version_code"], 6)
        self.certificate.assert_not_called()


class ReleaseCommandTests(unittest.TestCase):
    def test_initial_certificate_password_is_only_passed_through_environment(self):
        completed = Mock(returncode=0, stdout=b"public certificate DER", stderr=b"")
        signing = {"keyAlias": "seven-u-release", "storePassword": "secret-test-password"}
        with (
            patch.object(release.shutil, "which", return_value="keytool"),
            patch.object(release.subprocess, "run", return_value=completed) as runner,
        ):
            certificate = release._configured_certificate(
                Path("release.p12"), signing, app=Path("app"), env={}
            )
        self.assertEqual(certificate, hashlib.sha256(completed.stdout).hexdigest())
        self.assertNotIn(signing["storePassword"], " ".join(runner.call_args.args[0]))
        self.assertEqual(
            runner.call_args.kwargs["env"]["SEVENU_CERTIFICATE_PASSWORD"], signing["storePassword"]
        )

    def test_tool_execution_passes_argument_list_without_shell(self):
        process = Mock(stdout=io.StringIO("stdout and stderr merged\n"))
        process.wait.return_value = 0
        lines = []
        with patch.object(release.subprocess, "Popen", return_value=process) as popen:
            release._run(
                ["D:/flutter sdk/flutter.bat", "build", "apk"], cwd=Path("app"), env={}, emit=lines.append
            )
        self.assertEqual(popen.call_args.args[0], ["D:/flutter sdk/flutter.bat", "build", "apk"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["stderr"], release.subprocess.STDOUT)
        self.assertEqual(lines, ["stdout and stderr merged"])

    def test_local_properties_flutter_fallback_and_newest_android_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / "app"
            (app / "android").mkdir(parents=True)
            flutter = root / "flutter sdk/bin" / ("flutter.bat" if os.name == "nt" else "flutter")
            flutter.parent.mkdir(parents=True)
            flutter.touch()
            sdk = root / "android sdk"
            for version in ("9.0.0", "37.0.0"):
                signer = sdk / "build-tools" / version / ("apksigner.bat" if os.name == "nt" else "apksigner")
                signer.parent.mkdir(parents=True)
                signer.touch()
            (app / "android/local.properties").write_text(
                f"flutter.sdk={flutter.parent.parent.as_posix()}\nsdk.dir={sdk.as_posix()}\n"
            )
            with patch.object(release.shutil, "which", return_value=None):
                actual_flutter, actual_signer = release._tool_paths(app)
            self.assertEqual(actual_flutter, flutter)
            self.assertEqual(actual_signer.parent.name, "37.0.0")


if __name__ == "__main__":
    unittest.main()
