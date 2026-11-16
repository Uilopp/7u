import sqlite3
import unittest

from maintenance.core import references


class ReferencesTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,text TEXT)")
        self.db.execute("CREATE VIEW normalized_messages AS SELECT * FROM messages")
        self.db.executemany(
            "INSERT INTO messages VALUES (?,?)", [(1, "reply"), (7512497993392950692, "original")]
        )
        references.initialize(self.db)

    def tearDown(self):
        self.db.close()

    def test_exact_ids_and_missing_links_remain_separate_from_body(self):
        reply = {
            "text": "reply",
            "references": [{"msg_id": i, "summary": "quoted"} for i in ("7512497993392950692", "999", None)],
            "other_element_types": [],
        }
        with self.db:
            references.save(self.db, 1, "fingerprint", reply)
            references.save(self.db, 1, "fingerprint", reply)
        links = references.archive_references(self.db, 1)
        self.assertEqual([r["status"] for r in links], ["available", "missing_record", "missing_identifier"])
        self.assertEqual(links[0]["msg_id"], "7512497993392950692")
        self.assertEqual(self.db.execute("SELECT text FROM messages WHERE msg_id=1").fetchone()[0], "reply")
        self.assertEqual(self.db.execute("SELECT count(*) FROM message_references").fetchone()[0], 1)

    def test_mismatched_body_is_rejected(self):
        with self.assertRaises(ValueError):
            references.save(self.db, 1, "hash", {"text": "wrong", "references": []})
        self.assertEqual(references.archive_references(self.db, 1), [])
