import sqlite3
import tempfile
import unittest
from pathlib import Path

from maintenance.voice.review import audio_digest, initialize, record_administrative_discard, record_review


class VoiceReviewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.audio = Path(self.folder.name) / "sample.amr"
        self.audio.write_bytes(b"test audio identity, not playable sound")
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,text TEXT)")
        self.db.execute(
            "CREATE TABLE message_provenance(msg_id INTEGER PRIMARY KEY,audio_path TEXT,voice_state TEXT,raw_msg_type INTEGER)"
        )
        self.db.execute("INSERT INTO messages VALUES(1,'旧转写')")
        self.db.execute(
            "INSERT INTO message_provenance VALUES(1,?,'legacy_unreviewed',6)", (str(self.audio),)
        )
        initialize(self.db)
        self.db.commit()
        self.args = dict(
            decision="verified",
            reviewer="test fixture only",
            rationale="test",
            expected_text="旧转写",
            expected_audio_sha256=audio_digest(self.audio),
            transcript="核对文字",
            listened=True,
        )

    def test_owner_can_discard_without_fabricating_a_listening_review(self):
        record_administrative_discard(
            self.db, 1, reviewer="owner", rationale="腾讯返回空文本，用户明确要求丢弃"
        )
        self.assertEqual(self.db.execute("SELECT text FROM messages WHERE msg_id=1").fetchone()[0], None)
        self.assertEqual(
            self.db.execute("SELECT voice_state FROM message_provenance WHERE msg_id=1").fetchone()[0],
            "rejected",
        )
        self.assertEqual(
            self.db.execute("SELECT decision,review_mode FROM voice_reviews").fetchone(),
            ("rejected", "administrative"),
        )

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def test_verified_edit_retains_original_and_audit_identity(self):
        record_review(self.db, 1, **self.args)
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], "核对文字")
        event = self.db.execute(
            "SELECT previous_text,reviewed_text,audio_sha256 FROM voice_reviews"
        ).fetchone()
        self.assertEqual(event, ("旧转写", "核对文字", self.args["expected_audio_sha256"]))

    def test_blind_review_mode_is_auditable(self):
        record_review(self.db, 1, **(self.args | {"review_mode": "blind"}))
        self.assertEqual(self.db.execute("SELECT review_mode FROM voice_reviews").fetchone()[0], "blind")

    def test_stale_audio_or_text_and_unconfirmed_review_do_not_mutate(self):
        for changes in ({"expected_text": "stale"}, {"expected_audio_sha256": "bad"}, {"listened": False}):
            with self.assertRaises(ValueError):
                record_review(self.db, 1, **(self.args | changes))
        self.assertEqual(self.db.execute("SELECT count(*) FROM voice_reviews").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], "旧转写")

    def test_rejection_excludes_text_without_destroying_it(self):
        record_review(self.db, 1, **(self.args | {"decision": "rejected", "transcript": None}))
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], "旧转写")
        self.assertEqual(
            self.db.execute("SELECT voice_state FROM message_provenance").fetchone()[0], "rejected"
        )

    def test_audit_write_failure_rolls_back_transcript_change(self):
        self.db.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON voice_reviews BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            record_review(self.db, 1, **self.args)
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], "旧转写")
        self.assertEqual(
            self.db.execute("SELECT voice_state FROM message_provenance").fetchone()[0], "legacy_unreviewed"
        )
