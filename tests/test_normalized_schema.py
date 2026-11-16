import sqlite3
import unittest

from maintenance.core.schema import refresh_normalized_view


class NormalizedSchemaTests(unittest.TestCase):
    def test_gray_tips_do_not_become_authored_replies_and_raw_rows_survive(self):
        db = sqlite3.connect(":memory:")
        try:
            db.execute(
                "CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,msg_kind TEXT,text TEXT,content_type INTEGER)"
            )
            db.execute(
                "CREATE TABLE message_provenance(msg_id INTEGER,sender_qq INTEGER,speaker TEXT,raw_msg_type INTEGER,audio_path TEXT,voice_state TEXT)"
            )
            db.executemany(
                "INSERT INTO messages VALUES (?,?,?,?)",
                [
                    (1, "reply", None, 8),
                    (2, "contact", None, 8),
                    (3, "contact", "old voice", 4),
                    (4, "text", "authored", 1),
                    (5, "text", None, 6),
                    (6, "text", "preserve words", 6),
                ],
            )
            db.executemany(
                "INSERT INTO message_provenance VALUES (?,?,?,?,?,?)",
                [
                    (1, 1, "self", 5, None, None),
                    (2, 2, "partner", 5, None, None),
                    (3, 1, "self", 6, "audio", "verified"),
                    (4, 2, "partner", 2, None, None),
                ],
            )
            db.executemany(
                "INSERT INTO message_provenance VALUES (?,?,?,?,?,?)",
                [(5, 1, "self", 2, None, None), (6, 1, "self", 2, None, None)],
            )
            before = db.execute("SELECT * FROM messages").fetchall()
            with db:
                refresh_normalized_view(db)
                refresh_normalized_view(db)
            self.assertEqual(
                db.execute("SELECT normalized_kind FROM normalized_messages ORDER BY msg_id").fetchall(),
                [("sys",), ("sys",), ("voice",), ("text",), ("sticker",), ("text",)],
            )
            self.assertEqual(db.execute("SELECT * FROM messages").fetchall(), before)
        finally:
            db.close()
