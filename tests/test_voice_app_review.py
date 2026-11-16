import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from maintenance.voice.app_review import item_by_id, submit


class VoiceAppReviewTests(unittest.TestCase):
    def test_id_lookup_is_sanitized_and_explicit_listening_is_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "chat.db"
            audio = root / "maintenance.voice.silk"
            audio.write_bytes(b"audio fixture")
            digest = hashlib.sha256(audio.read_bytes()).hexdigest()
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,text TEXT)")
                db.execute(
                    "CREATE TABLE message_provenance(msg_id INTEGER PRIMARY KEY,audio_path TEXT,voice_state TEXT,raw_msg_type INTEGER)"
                )
                db.execute(
                    "CREATE TABLE normalized_messages(msg_id INTEGER,ts INTEGER,speaker TEXT,text TEXT,voice_state TEXT,audio_path TEXT,normalized_kind TEXT)"
                )
                db.execute(
                    "CREATE TABLE voice_candidates(msg_id INTEGER,model TEXT,parameters TEXT,candidate TEXT,metrics TEXT,created_at INTEGER)"
                )
                db.execute("""CREATE TABLE voice_secondary_candidates(msg_id INTEGER,provider TEXT,
                    engine TEXT,candidate TEXT,audio_sha256 TEXT,request_id TEXT,created_at INTEGER)""")
                db.execute("INSERT INTO messages VALUES(1,'旧稿')")
                db.execute("INSERT INTO message_provenance VALUES(1,?,'pending',6)", (str(audio),))
                db.execute(
                    "INSERT INTO normalized_messages VALUES(1,1789142400,'partner','旧稿','pending',?,'voice')",
                    (str(audio),),
                )
                db.execute(
                    "INSERT INTO voice_candidates VALUES(?,?,?,?,?,?)",
                    (1, "model", "{}", "候选稿", json.dumps({"audio_sha256": digest, "duration": 1}), 1),
                )
                db.execute(
                    "INSERT INTO voice_secondary_candidates VALUES(1,'tencent_cloud_asr',"
                    "'16k_zh','腾讯参考',?,'r',2)",
                    (digest,),
                )
            direct = item_by_id(db_path, "1")
            self.assertEqual(direct["status"], "pending", direct)
            self.assertEqual(direct["item"]["msg_id"], "1")
            self.assertEqual(direct["item"]["candidate"], "旧稿")
            self.assertEqual(direct["item"]["secondary_candidate"], "腾讯参考")
            self.assertEqual(direct["item"]["current_text"], "旧稿")
            self.assertEqual(direct["item"]["occurred_at_beijing"], "2026-09-12 00:00")
            self.assertNotIn(str(audio), json.dumps(direct, ensure_ascii=False))
            request = {
                **{
                    key: direct["item"][key]
                    for key in (
                        "msg_id",
                        "expected_text",
                        "expected_audio_sha256",
                        "candidate_model",
                        "candidate_parameters",
                        "expected_candidate",
                    )
                },
                "decision": "verified",
                "transcript": "人工核对稿",
                "listened": False,
            }
            with self.assertRaises(ValueError):
                submit(db_path, request)
            request["listened"] = True
            with self.assertRaises(ValueError):
                submit(db_path, request | {"expected_candidate": "被替换的候选"})
            self.assertEqual(submit(db_path, request)["decision"], "verified")
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute("SELECT text FROM messages").fetchone()[0], "人工核对稿")
                self.assertEqual(
                    db.execute("SELECT voice_state FROM message_provenance").fetchone()[0], "verified"
                )
                self.assertEqual(
                    db.execute("SELECT reviewer FROM voice_reviews").fetchone()[0], "local_app_user"
                )
                self.assertEqual(
                    db.execute(
                        "SELECT candidate_model,candidate_parameters,candidate_text FROM voice_reviews"
                    ).fetchone(),
                    ("model", "{}", "候选稿"),
                )

    def test_request_shape_and_invalid_id_fail_closed(self):
        for identifier in ("bad", "0", 1, True, None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                item_by_id("missing", identifier)
        with self.assertRaises(ValueError):
            submit("missing", {})

    def test_id_lookup_ignores_candidates_for_different_audio_or_invalid_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "chat.db"
            audio = root / "maintenance.voice.silk"
            audio.write_bytes(b"current audio")
            digest = hashlib.sha256(audio.read_bytes()).hexdigest()
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute(
                    "CREATE TABLE normalized_messages(msg_id INTEGER,ts INTEGER,speaker TEXT,text TEXT,voice_state TEXT,audio_path TEXT,normalized_kind TEXT)"
                )
                db.execute(
                    "CREATE TABLE voice_candidates(msg_id INTEGER,model TEXT,parameters TEXT,candidate TEXT,metrics TEXT,created_at INTEGER)"
                )
                db.execute("""CREATE TABLE voice_secondary_candidates(msg_id INTEGER,provider TEXT,
                    engine TEXT,candidate TEXT,audio_sha256 TEXT,request_id TEXT,created_at INTEGER)""")
                db.execute(
                    "INSERT INTO normalized_messages VALUES(1,1789142400,'partner',NULL,'pending',?,'voice')",
                    (str(audio),),
                )
                db.executemany(
                    "INSERT INTO voice_candidates VALUES(?,?,?,?,?,?)",
                    [
                        (1, "bad-json", "{}", "不可用", "not-json", 4),
                        (1, "bad-shape", "{}", "不可用", "[]", 3),
                        (1, "stale", "{}", "旧音频候选", json.dumps({"audio_sha256": "0" * 64}), 2),
                    ],
                )
                db.execute(
                    "INSERT INTO voice_secondary_candidates VALUES(1,'tencent_cloud_asr',"
                    "'16k_zh','当前腾讯稿',?,'r',1)",
                    (digest,),
                )
            item = item_by_id(db_path, "1")["item"]
            self.assertEqual(item["candidate"], "当前腾讯稿")
            self.assertEqual(item["expected_audio_sha256"], digest)
            self.assertNotIn("candidate_model", item)
            with self.assertRaisesRegex(ValueError, "没有找到"):
                item_by_id(db_path, "2")

    def test_discarded_voice_can_be_repaired_without_candidate_or_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "chat.db"
            audio = root / "maintenance.voice.silk"
            audio.write_bytes(b"audio fixture")
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,ts INTEGER,text TEXT)")
                db.execute(
                    "CREATE TABLE message_provenance(msg_id INTEGER PRIMARY KEY,audio_path TEXT,voice_state TEXT,raw_msg_type INTEGER,speaker TEXT)"
                )
                db.execute("INSERT INTO messages VALUES(1,1789142400,NULL)")
                db.execute("INSERT INTO message_provenance VALUES(1,?,'discarded',6,'self')", (str(audio),))
                db.execute(
                    "CREATE VIEW normalized_messages AS SELECT m.*,p.audio_path,p.voice_state,p.speaker,'voice' AS normalized_kind FROM messages m JOIN message_provenance p USING(msg_id)"
                )
            item = item_by_id(db_path, "1")["item"]
            self.assertEqual(item["candidate"], "")
            self.assertNotIn("candidate_model", item)
            request = {key: item[key] for key in ("msg_id", "expected_text", "expected_audio_sha256")}
            request.update(decision="verified", transcript="重新听写的文字。", listened=True)
            self.assertEqual(submit(db_path, request)["decision"], "verified")
            repaired = item_by_id(db_path, "1")["item"]
            self.assertEqual(repaired["candidate"], "重新听写的文字。")
            self.assertEqual(repaired["voice_state"], "verified")
            audio.unlink()
            with self.assertRaisesRegex(ValueError, "原音不在本机"):
                item_by_id(db_path, "1")
