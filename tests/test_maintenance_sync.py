import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from maintenance.core.process_lock import AlreadyRunning, process_lock
from maintenance.sync import run_sync


class MaintenanceSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.db_path = self.root / "chat.db"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,ts INTEGER,text TEXT)")
            db.execute(
                "CREATE TABLE message_provenance(msg_id INTEGER PRIMARY KEY,raw_msg_type INTEGER,voice_state TEXT)"
            )
            db.execute("INSERT INTO messages VALUES(1,100,'existing')")
            db.execute("INSERT INTO message_provenance VALUES(1,6,'verified')")
        self.args = dict(
            config={"database": str(self.db_path)},
            db_path=self.db_path,
            tencent_config_path=self.root / "credentials",
            progress_path=self.root / "progress.json",
            lock_path=self.root / "maintenance.lock",
        )

    def _add(self, identifier, state):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO messages VALUES(?,?,NULL)", (identifier, identifier + 100))
            db.execute("INSERT INTO message_provenance VALUES(?,6,?)", (identifier, state))

    def test_text_only_sync_skips_asr_and_keeps_missing_audio_as_a_gap(self):
        self._add(2, "missing_audio")
        with (
            patch("maintenance.sync.poll", return_value={"added": 0}),
            patch("maintenance.sync.transcribe") as asr,
        ):
            result = run_sync(**self.args)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["after"]["missing_audio"], 1)
        self.assertEqual(result["after"]["usable_voice"], 1)
        self.assertTrue(Path(result["backup_path"]).is_file())
        asr.assert_not_called()

    def test_coordinator_can_hold_maintenance_lock_but_importer_lock_is_still_required(self):
        with process_lock(self.args["lock_path"]), patch("maintenance.sync.poll", return_value={"added": 0}):
            result = run_sync(**self.args, _maintenance_lock_held=True)
            self.assertEqual(result["status"], "completed")
            with process_lock(self.db_path.with_suffix(".db.ingest.lock")):
                with self.assertRaises(AlreadyRunning):
                    run_sync(**self.args, _maintenance_lock_held=True)

    def test_new_voices_are_automatically_adopted_without_human_review(self):
        def imported(config):
            self._add(2, "pending")
            return {"added": 1}

        def adopted(path):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE message_provenance SET voice_state='asr_accepted' WHERE msg_id=2")
            return {"empty_tencent_candidates": 0}

        with (
            patch("maintenance.sync.poll", side_effect=imported),
            patch(
                "maintenance.sync.transcribe",
                return_value={"completed": 1, "reused": 0, "failed": 0, "errors": []},
            ) as asr,
            patch("maintenance.sync.adopt", side_effect=adopted),
        ):
            result = run_sync(**self.args)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["voice_completed"], 1)
        self.assertEqual(asr.call_args.kwargs["selection"], "pending")
        self.assertFalse(result["correctness_verified"])
        with closing(sqlite3.connect(result["backup_path"])) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(result["after"]["voice_states"], {"asr_accepted": 1, "verified": 1})

    def test_failed_voice_stays_pending_and_does_not_loop_forever(self):
        self._add(2, "pending")
        with (
            patch("maintenance.sync.poll", return_value={"added": 0}),
            patch(
                "maintenance.sync.transcribe",
                return_value={
                    "completed": 0,
                    "reused": 0,
                    "failed": 1,
                    "errors": [{"msg_id": "2", "error": "TimeoutError"}],
                },
            ) as asr,
            patch("maintenance.sync.adopt", return_value={"empty_tencent_candidates": 0}),
        ):
            result = run_sync(**self.args)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["after"]["voice_states"]["pending"], 1)
        self.assertEqual(asr.call_count, 1)

    def test_snapshot_retry_is_bounded_and_success_continues(self):
        with (
            patch(
                "maintenance.sync.poll",
                side_effect=[RuntimeError("QQ database changed during snapshot; retry later"), {"added": 0}],
            ) as importer,
            patch("maintenance.sync.time.sleep") as pause,
        ):
            result = run_sync(**self.args)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(importer.call_count, 2)
        pause.assert_called_once_with(1.5)

    def test_more_than_one_batch_is_finished_and_empty_count_is_not_duplicated(self):
        self._add(2, "pending")
        self._add(3, "pending")
        self._add(4, "discarded")

        def adopted(path):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "UPDATE message_provenance SET voice_state='discarded' WHERE msg_id=(SELECT min(msg_id) FROM message_provenance WHERE voice_state='pending')"
                )
            return {"empty_tencent_candidates": 99}

        with (
            patch("maintenance.sync.poll", return_value={"added": 0}),
            patch(
                "maintenance.sync.transcribe",
                return_value={"completed": 1, "reused": 0, "failed": 0, "errors": []},
            ) as asr,
            patch("maintenance.sync.adopt", side_effect=adopted),
        ):
            result = run_sync(**self.args, batch_size=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(asr.call_count, 2)
        self.assertEqual(result["voice_empty_discarded"], 2)

    def test_release_lock_prevents_import_before_any_database_change(self):
        with process_lock(self.args["lock_path"]), patch("maintenance.sync.poll") as importer:
            with self.assertRaises(AlreadyRunning):
                run_sync(**self.args)
        importer.assert_not_called()
        self.assertFalse((self.root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
