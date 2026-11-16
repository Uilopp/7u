import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from maintenance.archive.memory_pack import build
from maintenance.core.model_privacy import contains_secret, load_exclusions


class ModelPrivacyTests(unittest.TestCase):
    def test_patterns_leave_ordinary_discussion_visible(self):
        for text in (
            "密码：test-only-example",
            "api_key=test-only-example",
            "sk-" + "a" * 32,
            "-----BEGIN PRIVATE KEY-----",
        ):
            self.assertTrue(contains_secret(text))
        for text in ("验证码收到了吗", "我忘记密码了", "喜欢画画", "我买了一个月订阅"):
            self.assertFalse(contains_secret(text))

    def test_registry_fails_closed_on_bad_structure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model_exclusions.json"
            self.assertEqual(load_exclusions(path), frozenset())
            for value in (
                {},
                {"message_ids": [True]},
                {"message_ids": ["-1"]},
                {"message_ids": ["１"]},
                {"message_ids": ["9" * 30]},
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_exclusions(path)

    def test_pack_export_keeps_excluded_text_out_without_changing_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "chat.db"
            (root / "model_exclusions.json").write_text(json.dumps({"message_ids": ["2"]}), encoding="utf-8")
            rows = [
                {
                    "msg_id": i,
                    "ts": i,
                    "speaker": "self",
                    "normalized_kind": "text",
                    "text": text,
                    "voice_state": None,
                }
                for i, text in enumerate(
                    ("普通聊天", "unlabelled-test-only", "密码：test-only-example", "另一个普通消息"), 1
                )
            ]
            with closing(sqlite3.connect(path)) as db:
                db.execute(
                    "CREATE TABLE normalized_messages(msg_id INTEGER,ts INTEGER,speaker TEXT,normalized_kind TEXT,text TEXT,voice_state TEXT)"
                )
                db.execute("CREATE TABLE message_references(msg_id INTEGER,payload TEXT)")
                db.executemany(
                    "INSERT INTO normalized_messages VALUES (?,?,?,?,?,?)",
                    [tuple(row.values()) for row in rows],
                )
                db.execute(
                    "INSERT INTO message_references VALUES (?,?)",
                    (
                        1,
                        json.dumps(
                            {
                                "text": "普通聊天",
                                "references": [
                                    {"msg_id": str(i), "resolution_status": "unique"} for i in (2, 3, 4)
                                ],
                            }
                        ),
                    ),
                )
                db.commit()
            output = root / "pack.db"
            build(path, output, sync_prompt=False)
            with closing(sqlite3.connect(output)) as packed:
                self.assertEqual({r[0] for r in packed.execute("SELECT msg_id FROM messages")}, {1, 4})
                self.assertEqual(
                    packed.execute("SELECT msg_id,target_msg_id FROM message_links").fetchall(), [(1, 4)]
                )
                self.assertEqual(
                    packed.execute('SELECT count(*) FROM messages WHERE speaker="partner"').fetchone()[0], 2
                )
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM normalized_messages").fetchone()[0], 4)
                self.assertEqual(
                    db.execute("SELECT text FROM normalized_messages WHERE msg_id=2").fetchone()[0],
                    "unlabelled-test-only",
                )
