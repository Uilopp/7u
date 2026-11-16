import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from maintenance.voice.adopt_tencent import MODEL, adopt


class AdoptTencentTests(unittest.TestCase):
    def test_empty_success_is_discarded_but_failed_new_voice_stays_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "chat.db"
            audio = root / "a.amr"
            audio.write_bytes(b"voice")
            digest = hashlib.sha256(b"voice").hexdigest()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,text TEXT)")
                db.execute(
                    "CREATE TABLE message_provenance(msg_id INTEGER,audio_path TEXT,raw_msg_type INTEGER,voice_state TEXT)"
                )
                db.execute(
                    "CREATE TABLE voice_candidates(msg_id INTEGER,model TEXT,parameters TEXT,candidate TEXT,metrics TEXT,created_at INTEGER)"
                )
                db.execute(
                    "CREATE TABLE voice_secondary_candidates(msg_id INTEGER,provider TEXT,engine TEXT,candidate TEXT,audio_sha256 TEXT)"
                )
                for msg_id in (1, 2):
                    db.execute("INSERT INTO messages VALUES(?,NULL)", (msg_id,))
                    db.execute("INSERT INTO message_provenance VALUES(?,?,6,'pending')", (msg_id, str(audio)))
                    db.execute(
                        "INSERT INTO voice_candidates VALUES(?,?,?,?,?,0)",
                        (
                            msg_id,
                            MODEL,
                            '{"preparation":true}',
                            "",
                            json.dumps({"duration": 1, "audio_sha256": digest}),
                        ),
                    )
                db.execute(
                    "INSERT INTO voice_secondary_candidates VALUES(1,'tencent_cloud_asr','16k_zh','  ',?)",
                    (digest,),
                )
            result = adopt(path)
            self.assertEqual(result["discarded"], 1)
            self.assertEqual(result["awaiting_tencent"], 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT msg_id,voice_state FROM message_provenance ORDER BY msg_id"
                    ).fetchall(),
                    [(1, "discarded"), (2, "pending")],
                )
                self.assertEqual(db.execute("SELECT msg_id FROM voice_candidates").fetchall(), [(2,)])

    def test_promotes_raw_tencent_and_preserves_human_decisions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "chat.db"
            audio = root / "a.amr"
            audio.write_bytes(b"voice")
            digest = hashlib.sha256(b"voice").hexdigest()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,text TEXT)")
                db.execute(
                    "CREATE TABLE message_provenance(msg_id INTEGER,audio_path TEXT,raw_msg_type INTEGER,voice_state TEXT)"
                )
                db.execute(
                    "CREATE TABLE voice_candidates(msg_id INTEGER,model TEXT,parameters TEXT,candidate TEXT,metrics TEXT,created_at INTEGER)"
                )
                db.execute(
                    "CREATE TABLE voice_secondary_candidates(msg_id INTEGER,provider TEXT,engine TEXT,candidate TEXT,audio_sha256 TEXT)"
                )
                db.execute("CREATE TABLE voice_punctuation(msg_id INTEGER)")
                for msg_id, state, current in (
                    (1, "asr_accepted", "旧稿"),
                    (2, "verified", "人工稿"),
                    (3, "asr_accepted", "空结果旧稿"),
                ):
                    db.execute("INSERT INTO messages VALUES(?,?)", (msg_id, current))
                    db.execute("INSERT INTO message_provenance VALUES(?,?,6,?)", (msg_id, str(audio), state))
                    db.execute(
                        "INSERT INTO voice_candidates VALUES(?,?,?,?,?,0)",
                        (msg_id, "medium", "{}", "旧稿", json.dumps({"duration": 1, "audio_sha256": digest})),
                    )
                    if msg_id != 3:
                        db.execute(
                            "INSERT INTO voice_secondary_candidates VALUES(?,'tencent_cloud_asr','16k_zh','腾讯原稿。',?)",
                            (msg_id, digest),
                        )
                db.execute("INSERT INTO voice_punctuation VALUES(1)")
            result = adopt(path)
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute("SELECT msg_id,text FROM messages ORDER BY msg_id").fetchall()
                states = db.execute(
                    "SELECT msg_id,voice_state FROM message_provenance ORDER BY msg_id"
                ).fetchall()
                models = db.execute("SELECT msg_id,model,candidate FROM voice_candidates").fetchall()
                layers = db.execute("SELECT count(*) FROM voice_punctuation").fetchone()[0]
            self.assertEqual(rows, [(1, "腾讯原稿。"), (2, "人工稿"), (3, None)])
            self.assertEqual(states, [(1, "asr_accepted"), (2, "verified"), (3, "discarded")])
            self.assertEqual(models, [(1, MODEL, "腾讯原稿。")])
            self.assertEqual(layers, 0)
            self.assertEqual(result["human_decisions_preserved"], 1)
            self.assertEqual(result["discarded"], 1)


if __name__ == "__main__":
    unittest.main()
