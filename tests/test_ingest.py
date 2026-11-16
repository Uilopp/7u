import sqlite3
import tempfile
import unittest
from pathlib import Path

from maintenance.ingest.importer import import_connection


class Parsed:
    def __init__(self, row):
        self.row = row

    def to_db_row(self):
        return {**self.row, "content_type": 1, "content": '{"type":"text"}', "text": "new message"}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.source = sqlite3.connect(":memory:")
        self.target = sqlite3.connect(":memory:")
        self.source.execute(
            "CREATE TABLE c2c_msg_table([40001] INTEGER,[40050] INTEGER,[40013] INTEGER,[40020] TEXT,[40033] INTEGER,[40021] TEXT,[40030] TEXT,[40011] INTEGER,[40800] BLOB)"
        )
        self.target.execute(
            "CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,ts INTEGER,direction INTEGER,content_type INTEGER,msg_kind TEXT,text TEXT,content TEXT)"
        )
        self.target.execute(
            "CREATE TABLE message_provenance(msg_id INTEGER PRIMARY KEY,sender_qq INTEGER,speaker TEXT,raw_direction INTEGER,raw_msg_type INTEGER,audio_md5 TEXT,audio_path TEXT,voice_state TEXT)"
        )
        self.folder = tempfile.TemporaryDirectory()
        self.cfg = {"self_qq": 1, "partner_qq": 2, "audio_directory": self.folder.name}

    def tearDown(self):
        self.source.close()
        self.target.close()
        self.folder.cleanup()

    def insert(self, mid):
        self.source.execute("INSERT INTO c2c_msg_table VALUES (?,?,2,'u',1,'v','2',2,NULL)", (mid, 100))

    def test_late_low_id_message_not_lost_and_rerun_idempotent(self):
        self.target.execute("INSERT INTO messages VALUES (999,50,2,1,'voice','verified original',NULL)")
        self.insert(100)
        result = import_connection(self.source, self.target, self.cfg, Parsed)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.target.execute("SELECT speaker FROM message_provenance").fetchone()[0], "self")
        self.assertEqual(import_connection(self.source, self.target, self.cfg, Parsed)["added"], 0)
        self.assertEqual(
            self.target.execute("SELECT text FROM messages WHERE msg_id=999").fetchone()[0],
            "verified original",
        )

    def test_parse_failure_does_not_partially_commit(self):
        self.insert(100)
        self.insert(200)

        def fail(row):
            if row["msg_id"] == 200:
                raise ValueError("bad packet")
            return Parsed(row)

        with self.assertRaises(ValueError):
            import_connection(self.source, self.target, self.cfg, fail)
        self.assertEqual(self.target.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_later_audio_download_is_recovered_without_overriding_human_decisions(self):
        digest = "a" * 32
        for mid, state in ((1, "missing_audio"), (2, "verified"), (3, "rejected")):
            self.target.execute("INSERT INTO messages VALUES (?,50,2,1,'voice','existing',NULL)", (mid,))
            self.target.execute(
                "INSERT INTO message_provenance VALUES (?,1,'self',2,6,?,NULL,?)", (mid, digest, state)
            )
            self.source.execute(
                "INSERT INTO c2c_msg_table VALUES (?,100,2,'u',1,'v','2',6,?)",
                (mid, (digest + ".amr").encode()),
            )
        Path(self.folder.name, digest + ".amr").write_bytes(b"original")
        result = import_connection(self.source, self.target, self.cfg, Parsed)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["recovered_audio"], 1)
        self.assertEqual(
            self.target.execute("SELECT voice_state FROM message_provenance ORDER BY msg_id").fetchall(),
            [("pending",), ("verified",), ("rejected",)],
        )
        self.assertEqual(
            self.target.execute("SELECT text FROM messages ORDER BY msg_id").fetchall(),
            [("existing",), ("existing",), ("existing",)],
        )


if __name__ == "__main__":
    unittest.main()
