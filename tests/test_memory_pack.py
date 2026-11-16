import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from maintenance.archive.memory_pack import build


class MemoryPackTest(unittest.TestCase):
    def test_exports_canonical_roles_and_only_accepted_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.db"
            output = Path(folder) / "pack.db"
            db = sqlite3.connect(source)
            db.executescript("""
                CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,ts INTEGER,direction INTEGER,
                    content_type INTEGER,msg_kind TEXT,text TEXT,content TEXT);
                CREATE TABLE message_provenance(msg_id INTEGER PRIMARY KEY,sender_qq INTEGER,
                    speaker TEXT,raw_direction INTEGER,raw_msg_type INTEGER,audio_md5 TEXT,
                    audio_path TEXT,voice_state TEXT);
                CREATE VIEW normalized_messages AS SELECT m.*,p.sender_qq,p.speaker,p.raw_msg_type,
                    CASE WHEN p.raw_msg_type=6 THEN 'voice' ELSE m.msg_kind END normalized_kind,
                    p.audio_path,p.voice_state FROM messages m JOIN message_provenance p USING(msg_id);
                CREATE TABLE message_references(msg_id INTEGER PRIMARY KEY,raw_sha256 TEXT,payload TEXT);
            """)
            rows = [
                (1, 100, "self", 1, "text", "我说的话", None),
                (2, 101, "partner", 1, "text", "她说的话", None),
                (3, 102, "partner", 6, "voice", "带标点的语音。", "verified"),
                (4, 103, "partner", 6, "voice", "应排除", "rejected"),
            ]
            for identifier, ts, speaker, raw_type, kind, text, state in rows:
                db.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (identifier, ts, 0, 0, kind, text, "")
                )
                db.execute(
                    "INSERT INTO message_provenance VALUES(?,?,?,?,?,?,?,?)",
                    (identifier, None, speaker, 0, raw_type, None, None, state),
                )
            db.execute(
                "INSERT INTO message_references VALUES(?,?,?)",
                (
                    1,
                    "x",
                    json.dumps(
                        {"references": [{"msg_id": "2", "resolution_status": "unique"}], "text": "我说的话"}
                    ),
                ),
            )
            db.commit()
            db.close()

            report = build(source, output, sync_prompt=False)
            self.assertEqual(report["messages"], 3)
            packed = sqlite3.connect(output)
            packed.row_factory = sqlite3.Row
            values = {row["msg_id"]: dict(row) for row in packed.execute("SELECT * FROM messages")}
            self.assertEqual(values[1]["speaker"], "partner")
            self.assertEqual(values[2]["speaker"], "self")
            self.assertEqual(values[3]["source_type"], "voice_transcript")
            self.assertNotIn(4, values)
            self.assertEqual(packed.execute("SELECT count(*) FROM message_links").fetchone()[0], 1)
            packed.close()


if __name__ == "__main__":
    unittest.main()
