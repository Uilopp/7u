import copy
import hashlib
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.qa import background_release as audit


def set_identifier(item):
    item["id"] = hashlib.sha256(
        f"{item['msgId']}\n{item['quote']}\n{item['kind']}\n{item['text']}\n{item['anchor']}".encode()
    ).hexdigest()[:24]


class FinalBackgroundAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE messages(msg_id TEXT PRIMARY KEY,ts INTEGER,speaker TEXT,text TEXT,source_type TEXT)"
        )
        timestamp = int(datetime(2026, 9, 27, 13, tzinfo=timezone(timedelta(hours=8))).timestamp())
        self.items = []
        for i in range(60):
            item = {
                "kind": ("question", "statement", "memory")[i % 3],
                "text": f"试试{i}号面包？",
                "date": "2026-09-27",
                "speaker": "partner",
                "quote": f"{i}号面包真的好吃呀",
                "msgId": str(i + 1),
                "anchor": "面包",
            }
            set_identifier(item)
            self.items.append(item)
            self.db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?)",
                (item["msgId"], timestamp, item["speaker"], "以前说过" + item["quote"], "message_text"),
            )

    def check(self, items=None):
        return audit.audit_daily_pool(
            json.dumps({"version": 1, "items": self.items if items is None else items}).encode(), self.db
        )

    def test_exact_balanced_pool_and_source_anchors_are_accepted(self):
        result = self.check()
        self.assertEqual(result["items"], 60)
        self.assertEqual(result["kinds"], dict.fromkeys(("question", "statement", "memory"), 20))
        self.assertTrue(result["source_anchors_exact"])
        self.assertEqual(result["invalid_ids"], [])

    def test_fewer_than_target_60_can_pass_without_relaxing_three_directions(self):
        for items in (self.items[:15], self.items[:20], self.items):
            with self.subTest(total=len(items)):
                result = self.check(items)
                self.assertTrue(result["source_anchors_exact"])
                self.assertTrue(all(audit.daily_size_checks(result).values()))

    def test_pool_size_and_direction_boundaries_still_reject_insufficient_or_oversized_sets(self):
        cases = [
            {"items": 14, "kinds": {"question": 5, "statement": 5, "memory": 4}},
            {"items": 61, "kinds": {"question": 21, "statement": 20, "memory": 20}},
            {"items": 20, "kinds": {"question": 9, "statement": 9, "memory": 2}},
            {"items": 30, "kinds": {"question": 21, "statement": 6, "memory": 3}},
            {"items": 20, "kinds": {"question": 10, "statement": 10}},
            {"items": 20, "kinds": {"question": 6, "statement": 6, "memory": 7, "other": 1}},
            {"items": 20, "kinds": {"question": 5, "statement": 5, "memory": 5}},
        ]
        for daily in cases:
            with self.subTest(daily=daily):
                self.assertFalse(all(audit.daily_size_checks(daily).values()))

    def test_utf16_length_rejects_non_bmp_text_even_when_python_length_is_15_or_less(self):
        items = copy.deepcopy(self.items)
        items[0]["text"] = "面包" + "呀" * 10 + "🫶🫶"
        self.assertLessEqual(len(items[0]["text"]), 15)
        set_identifier(items[0])
        result = self.check(items)
        self.assertGreater(result["text_max_utf16"], 15)
        self.assertFalse(result["source_anchors_exact"])

    def test_wrong_speaker_date_quote_kind_or_anchor_cannot_pass(self):
        for field, value in (
            ("speaker", "self"),
            ("date", "2026-09-26"),
            ("quote", "没有说过的原话"),
            ("kind", "unknown"),
            ("anchor", "蛋糕"),
        ):
            with self.subTest(field=field):
                items = copy.deepcopy(self.items)
                items[0][field] = value
                set_identifier(items[0])
                self.assertFalse(self.check(items)["source_anchors_exact"])

    def test_voice_source_is_not_allowed_in_unsolicited_daily_notification(self):
        self.db.execute("UPDATE messages SET source_type='voice_transcript' WHERE msg_id='1'")
        self.assertFalse(self.check()["source_anchors_exact"])

    def test_duplicate_or_malformed_items_fail_without_crashing(self):
        for change in (
            lambda items: items.append(items[0]),
            lambda items: items.append(None),
            lambda items: items[0].pop("anchor"),
        ):
            items = copy.deepcopy(self.items)
            change(items)
            self.assertFalse(self.check(items)["source_anchors_exact"])
        for payload in (b"not json", b"[]", b'{"version":true,"items":[]}'):
            self.assertFalse(audit.audit_daily_pool(payload, self.db)["schema_valid"])

    def test_cli_forwards_explicit_package_and_optional_comparison(self):
        with patch.object(audit.prior, "run_cli", return_value=1) as command:
            args = ["--apk", "releases/example.apk", "--previous", "releases/previous.apk"]
            self.assertEqual(audit.main(args), 1)
        command.assert_called_once_with(audit.audit_release, args)


if __name__ == "__main__":
    unittest.main()
