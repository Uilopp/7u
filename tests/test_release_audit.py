import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from maintenance.archive.memory_pack import build
from maintenance.model_config import ModelConfig
from tools.qa import release_audit as audit


class ReleasePackAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.pack = self.root / "archive.db", self.root / "pack.db"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executescript("""
                CREATE TABLE normalized_messages(msg_id INTEGER PRIMARY KEY,ts INTEGER,
                    speaker TEXT,text TEXT,normalized_kind TEXT,voice_state TEXT);
                CREATE TABLE message_references(msg_id INTEGER PRIMARY KEY,payload TEXT);
                INSERT INTO normalized_messages VALUES(1,100,'self','合成的面包记录。','text',NULL);
                INSERT INTO normalized_messages VALUES(2,101,'partner','合成的语音记录。','voice','verified');
                INSERT INTO normalized_messages VALUES(3,102,'partner',NULL,'voice','pending');
            """)
        build(self.source, self.pack, sync_prompt=False)

    def test_synthetic_pack_passes_without_a_private_profile_or_fixed_fact_count(self):
        report, checks, hashes = audit.audit_pack(self.pack.read_bytes(), self.source)
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual(report["profile"]["facts"], 0)
        self.assertEqual(report["bundle"]["messages"], 2)
        self.assertEqual(report["bundle"]["events"], 3)
        self.assertEqual(set(hashes), {"events", "message_links", "messages", "metadata"})
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("合成的面包记录", serialized)
        self.assertNotIn("合成的语音记录", serialized)

    def test_wrong_speaker_text_and_voice_origin_each_fail_exact_source_check(self):
        for column, value in (
            ("speaker", "partner"),
            ("text", "invented"),
            ("transcript_origin", "automatic_asr"),
        ):
            with self.subTest(column=column), closing(audit.connect_bytes(self.pack.read_bytes())) as db:
                db.execute(f"UPDATE messages SET {column}=? WHERE msg_id=2", (value,))
                db.commit()
                _, checks, _ = audit.audit_pack(db.serialize(), self.source)
                self.assertFalse(checks["messages_match_source_exactly"])

    def test_dangling_reference_and_event_tampering_are_independent_failures(self):
        with closing(audit.connect_bytes(self.pack.read_bytes())) as db:
            db.execute("INSERT INTO message_links VALUES(1,999)")
            db.execute("UPDATE events SET text_available=1 WHERE msg_id=3")
            db.commit()
            report, checks, _ = audit.audit_pack(db.serialize(), self.source)
        self.assertFalse(checks["references_match_source_exactly"])
        self.assertFalse(checks["events_match_source_exactly"])
        self.assertEqual(report["bundle"]["dangling_links"], 1)

    def test_logical_comparison_ignores_only_generated_build_timestamp(self):
        with closing(audit.connect_bytes(self.pack.read_bytes())) as db:
            before = audit.tables(db)
            db.execute("UPDATE metadata SET value='999999' WHERE key='built_at'")
            self.assertEqual(audit.tables(db), before)
            db.execute("UPDATE metadata SET value='200' WHERE key='latest_message_ts'")
            db.commit()
            self.assertNotEqual(audit.tables(db), before)
            _, checks, _ = audit.audit_pack(db.serialize(), self.source)
            self.assertFalse(checks["latest_message_metadata_exact"])

    def test_full_package_uses_configured_version_and_signature_without_private_facts(self):
        app = self.root / "app"
        (app / "assets/memory").mkdir(parents=True)
        (app / "assets/agent").mkdir()
        (self.root / "agent").mkdir()
        (self.root / "data").mkdir()
        (self.root / "data/couple_chat.db").write_bytes(self.source.read_bytes())
        (app / "assets/memory/memory_pack.db").write_bytes(self.pack.read_bytes())
        for path in (app / "assets/agent/prompt.md", self.root / "agent/prompt.md"):
            path.write_bytes(b"synthetic prompt")
        (app / "pubspec.yaml").write_text("version: 2.3.270101+42\n")
        package = self.root / "custom.apk"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(audit.MEMORY_ASSET, self.pack.read_bytes())
            archive.writestr(audit.PROMPT_ASSET, b"synthetic prompt")
            archive.writestr("lib/arm64-v8a/libapp.so", b"synthetic-model-key")
        signing = {
            "storePassword": "synthetic-password",
            "keyPassword": "synthetic-password",
            "storeFile": "release.p12",
            "keyAlias": "synthetic",
        }
        metadata = {"version_name": "2.3.270101", "version_code": 42, "certificate_sha256": "a" * 64}
        with (
            patch.object(audit, "load_model_config", return_value=ModelConfig("synthetic-model-key")),
            patch.object(audit, "load_tencent", return_value=None),
            patch.object(audit, "_properties", return_value=signing),
            patch.object(audit, "_tool_paths", return_value=(Path("flutter"), Path("apksigner"))),
            patch.object(audit, "_configured_certificate", return_value="a" * 64),
            patch.object(audit, "_inspect_apk", return_value=metadata),
            patch.object(audit, "android_output", return_value="package: name='com.sevenu.app'"),
            patch.object(audit.subprocess, "run", return_value=Mock(returncode=0)),
        ):
            report = audit.audit_release(package, root=self.root)
            self.assertTrue(report["passed"], report["failed_checks"])
            metadata["certificate_sha256"] = "b" * 64
            failed = audit.audit_release(package, root=self.root)
            self.assertFalse(failed["checks"]["permanent_signature"])
        self.assertNotIn("versus_previous_pack", report)


class ReleasePrivacyAuditTests(unittest.TestCase):
    def test_private_credentials_and_test_files_are_detected_without_printing_values(self):
        stream = io.BytesIO()
        private = "synthetic-signing-password"
        intentional = "synthetic-model-key"
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("lib/arm64-v8a/libapp.so", intentional.encode() + private.encode("utf-16le"))
            archive.writestr("assets/private.dpapi", b"synthetic-encrypted-config")
            archive.writestr("assets/tests/probe.txt", "test-only-key-never-sent")
        with zipfile.ZipFile(stream) as archive:
            result = audit.audit_privacy(
                archive,
                model_key=intentional,
                forbidden_values={"signing": private},
                forbidden_blobs={"config": b"synthetic-encrypted-config"},
            )
        self.assertEqual(result["forbidden_entry_count"], 2)
        self.assertTrue(result["forbidden_credential_forms_found"]["signing"])
        self.assertTrue(result["pc_encrypted_config_bytes_found"]["config"])
        self.assertTrue(result["intentional_model_key_present"])
        self.assertTrue(result["test_markers_found"]["test-only-key-never-sent"])
        serialized = json.dumps(result)
        self.assertNotIn(private, serialized)
        self.assertNotIn(intentional, serialized)
        self.assertNotIn("synthetic-encrypted-config", serialized)

    def test_cli_requires_an_explicit_apk_without_loading_private_inputs(self):
        with patch.object(audit, "audit_release") as perform, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as stopped:
                audit.main([])
        self.assertEqual(stopped.exception.code, 2)
        perform.assert_not_called()

    def test_cli_library_error_never_prints_credential_or_source_details(self):
        output = io.StringIO()
        with (
            patch.object(audit, "audit_release", side_effect=RuntimeError("synthetic-secret /private/path")),
            redirect_stdout(output),
        ):
            self.assertEqual(audit.main(["--apk", "a.apk"]), 1)
        message = json.loads(output.getvalue())
        self.assertEqual(message["error_type"], "RuntimeError")
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn("/private/path", output.getvalue())

    def test_cli_passes_explicit_versions_and_optional_previous_without_fixed_release_names(self):
        report = {"passed": True, "failed_checks": [], "sha256": "a" * 64}
        with (
            patch.object(audit, "audit_release", return_value=report) as perform,
            patch.object(audit, "publish_json") as publish,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                audit.main(
                    ["--apk", "custom.apk", "--expected-version", "2.3.270101", "--expected-code", "42"]
                ),
                0,
            )
        perform.assert_called_once_with(
            Path("custom.apk"),
            previous=None,
            expected_version="2.3.270101",
            expected_code=42,
            require_unchanged=False,
        )
        self.assertEqual(publish.call_args.args[0].parent, audit.ROOT / "data/qa")

    def test_unchanged_mode_and_self_comparison_fail_before_private_io(self):
        with patch.object(audit, "load_model_config") as load:
            with self.assertRaises(ValueError):
                audit.audit_release("a.apk", require_unchanged=True)
            with self.assertRaises(ValueError):
                audit.audit_release("a.apk", previous="a.apk")
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
