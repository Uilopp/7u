import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from maintenance import daily_memories, release, sync
from maintenance.core.process_lock import AlreadyRunning, process_lock
from maintenance.core.schema import initialize_archive, reject_synthetic_database


class FirstArchiveInitializationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / "data/couple_chat.db"
        self.source = self.root / "qq-source.db"
        self.key = self.root / "synthetic-db-key.txt"
        self.source.write_bytes(b"synthetic source; poll is mocked")
        self.key.write_text("synthetic test key")
        self.config = {
            "database": str(self.target),
            "self_qq": 1,
            "partner_qq": 2,
            "source_encrypted_database": str(self.source),
            "database_key_file": str(self.key),
            "audio_directory": str(self.root),
        }
        self.args = {
            "config": self.config,
            "db_path": self.target,
            "lock_path": self.root / "data/maintenance.lock",
        }

    def test_first_run_initializes_complete_archive_under_both_locks_before_poll(self):
        def imported(config):
            for lock in (self.args["lock_path"], self.target.with_suffix(".db.ingest.lock")):
                with self.assertRaises(AlreadyRunning), process_lock(lock):
                    self.fail("A maintenance lock was released before first import")
            with closing(sqlite3.connect(self.target)) as db, db:
                self.assertEqual(db.execute("SELECT count(*) FROM normalized_messages").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM message_references").fetchone()[0], 0)
                db.execute("INSERT INTO messages VALUES(1,100,1,1,'text','synthetic import','{}')")
                db.execute("INSERT INTO message_provenance VALUES(1,1,'self',1,2,NULL,NULL,NULL)")
            return {"added": 1}

        with (
            patch.object(sync, "poll", side_effect=imported) as poll,
            patch.object(sync, "transcribe") as transcribe,
        ):
            result = sync.run_sync(**self.args)
        self.assertTrue(result["initialized_archive"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["before"]["messages"], 0)
        self.assertEqual(result["after"]["messages"], 1)
        self.assertEqual(poll.call_count, 1)
        transcribe.assert_not_called()
        with closing(sqlite3.connect(result["backup_path"])) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_existing_archive_and_its_backup_are_not_reinitialized(self):
        self.assertTrue(initialize_archive(self.target))
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute("INSERT INTO messages VALUES(9,100,1,1,'text','keep existing','{}')")
        before = self.target.read_bytes()
        with patch.object(sync, "poll", return_value={"added": 0}):
            result = sync.run_sync(**self.args)
        self.assertFalse(result["initialized_archive"])
        self.assertEqual(self.target.read_bytes(), before)
        with closing(sqlite3.connect(result["backup_path"])) as db:
            self.assertEqual(db.execute("SELECT text FROM messages").fetchone()[0], "keep existing")

    def test_existing_unknown_file_is_never_replaced(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"not a database; preserve it")
        self.assertFalse(initialize_archive(self.target))
        self.assertEqual(self.target.read_bytes(), b"not a database; preserve it")

    def test_bad_configuration_cannot_create_an_empty_archive(self):
        cases = [
            dict(self.config, self_qq=0),
            dict(self.config, partner_qq=1),
            dict(self.config, database_key_file=""),
            dict(self.config, source_encrypted_database="missing"),
            {"database": str(self.target)},
        ]
        with patch.object(sync, "poll") as poll:
            for config in cases:
                with self.subTest(fields=sorted(config)):
                    with self.assertRaises(ValueError):
                        sync.run_sync(**{**self.args, "config": config})
                    self.assertFalse(self.target.exists())
                    self.assertFalse((self.root / "data/backups").exists())
        poll.assert_not_called()

    def test_lock_prevents_initialization_not_only_import(self):
        with process_lock(self.args["lock_path"]), patch.object(sync, "poll") as poll:
            with self.assertRaises(AlreadyRunning):
                sync.run_sync(**self.args)
        self.assertFalse(self.target.exists())
        poll.assert_not_called()

    def test_failed_schema_creation_never_publishes_partial_database(self):
        with patch(
            "maintenance.core.schema.refresh_normalized_view", side_effect=RuntimeError("synthetic failure")
        ):
            with self.assertRaises(RuntimeError):
                initialize_archive(self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".new-archive-*")), [])

    def test_exclusive_publish_keeps_a_racing_existing_file(self):
        def competitor(source, destination):
            Path(destination).write_bytes(b"concurrent winner")
            raise FileExistsError("already exists")

        with patch("maintenance.core.schema.os.link", side_effect=competitor):
            self.assertFalse(initialize_archive(self.target))
        self.assertEqual(self.target.read_bytes(), b"concurrent winner")
        self.assertEqual(list(self.target.parent.glob(".new-archive-*")), [])


class SyntheticReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def database(self, relative, marker="true"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT INTO metadata VALUES(?,?)", ("is_synthetic", marker))
        return path

    def test_marked_source_or_memory_is_rejected_before_key_model_or_export(self):
        for relative in ("data/couple_chat.db", "app/assets/memory/memory_pack.db"):
            with self.subTest(path=relative):
                fixture = self.database(relative)
                original = fixture.read_bytes()
                with (
                    patch.object(release, "load_model_config") as key,
                    patch.object(release, "build_memory_pack") as memory,
                    patch.object(release, "build_daily_pool") as daily,
                ):
                    with self.assertRaisesRegex(release.ReleaseError, "合成测试数据库"):
                        release.build_release(self.root, emit=lambda _: None)
                key.assert_not_called()
                memory.assert_not_called()
                daily.assert_not_called()
                self.assertEqual(fixture.read_bytes(), original)
                fixture.unlink()

    def test_both_daily_fixture_markers_are_rejected_before_publish(self):
        path = self.root / "app/assets/memory/daily_memories.json"
        path.parent.mkdir(parents=True)
        for marker in ({"is_synthetic": True}, {"generator_version": "synthetic-fixture-v1"}):
            with self.subTest(marker=marker):
                path.write_text(json.dumps({"version": 1, "items": [], **marker}))
                original = path.read_bytes()
                with patch.object(release, "load_model_config") as key:
                    with self.assertRaisesRegex(release.ReleaseError, "合成测试短句池"):
                        release.build_release(self.root, emit=lambda _: None)
                key.assert_not_called()
                with self.assertRaisesRegex(daily_memories.DailyMemoryError, "合成测试短句池"):
                    daily_memories.validate_prepared_pool(self.root / "missing.db", path)
                self.assertEqual(path.read_bytes(), original)

    def test_daily_generation_refuses_synthetic_source_before_provider(self):
        source = self.database("source.db")
        with patch.object(daily_memories.provider, "complete") as complete:
            with self.assertRaisesRegex(ValueError, "合成测试数据库"):
                daily_memories._source_records(source)
        complete.assert_not_called()

    def test_guard_does_not_create_missing_files_or_reject_false_marker(self):
        missing = self.root / "not-created.db"
        reject_synthetic_database(missing)
        self.assertFalse(missing.exists())
        path = self.database("real.db", "false")
        before = path.read_bytes()
        reject_synthetic_database(path)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
