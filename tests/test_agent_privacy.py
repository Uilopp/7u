import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from maintenance.archive.profile import CanonicalStore, Store


def initialize(db):
    db.executescript("""
      CREATE TABLE memory_candidates(
        fact_id TEXT PRIMARY KEY,subject TEXT,category TEXT,claim TEXT,
        confidence REAL,evidence TEXT,source TEXT,created_at INTEGER,
        status TEXT,updated_at INTEGER);
      CREATE TABLE memory_decisions(
        decision_id INTEGER PRIMARY KEY AUTOINCREMENT,fact_id TEXT,
        decision TEXT,rationale TEXT,reviewer TEXT,evidence TEXT,created_at INTEGER);
    """)


def decide(db, fact_id, decision, rationale, reviewer, evidence):
    db.execute("UPDATE memory_candidates SET status=? WHERE fact_id=?", (decision, fact_id))
    db.execute(
        "INSERT INTO memory_decisions(fact_id,decision,rationale,reviewer,evidence,created_at) "
        "VALUES (?,?,?,?,?,0)",
        (fact_id, decision, rationale, reviewer, json.dumps(evidence, ensure_ascii=False)),
    )


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "chat.db"
        self.profile = Path(self.folder.name) / "profile.json"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TABLE normalized_messages(msg_id INTEGER,ts INTEGER,speaker TEXT,text TEXT,normalized_kind TEXT,voice_state TEXT)"
            )
            db.executemany(
                "INSERT INTO normalized_messages VALUES(?,?,?,?,?,?)",
                [
                    (1, 100, "self", "我喜欢电影", "text", None),
                    (2, 101, "partner", "秘密地点", "text", None),
                    (3, 102, "partner", "喜欢电影", "voice", "legacy_unreviewed"),
                    (99, 10000, "partner", "新模型语音", "voice", "asr_accepted"),
                ],
            )
            db.commit()
        self.fact = {
            "id": "f",
            "subject": "self",
            "category": "preference",
            "claim": "喜欢电影",
            "evidence": [{"msg_id": "1", "quote": "我喜欢电影"}],
            "review": {"rationale": "秘密地点在其他未引用的上下文中"},
            "unexpected": "秘密地点",
        }

    def tearDown(self):
        self.folder.cleanup()

    def store(self):
        self.profile.write_text(json.dumps({"facts": [self.fact]}), encoding="utf-8")
        with closing(sqlite3.connect(self.path)) as db:
            initialize(db)
            db.execute(
                "INSERT OR REPLACE INTO memory_candidates VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "f",
                    self.fact["subject"],
                    self.fact["category"],
                    self.fact["claim"],
                    0.9,
                    json.dumps(self.fact["evidence"]),
                    "fixture",
                    0,
                    "candidate",
                    0,
                ),
            )
            decide(db, "f", "accepted", "Fixture proof", "test", self.fact["evidence"])
            db.commit()
        return Store(self.path, profile_path=self.profile, blocked_keywords=["秘密地点"])

    def test_accepted_profile_can_use_explicitly_adopted_asr(self):
        self.fact["evidence"] = [{"msg_id": "99", "quote": "新模型语音"}]
        store = self.store()
        result = store.profile()
        citation = result["facts"][0]["evidence"][0]
        self.assertEqual(citation["source_type"], "voice_transcript")
        self.assertEqual(citation["transcript_origin"], "automatic_asr")
        self.assertEqual(store.evidence["99"]["speaker"], "partner")

    def test_review_metadata_never_bypasses_source_privacy(self):
        self.fact["evidence"][0]["internal_context"] = "秘密地点"
        result = self.store().profile()
        self.assertEqual(len(result["facts"]), 1)
        self.assertNotIn("秘密地点", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("review", result["facts"][0])

    def test_canonical_profile_flips_subject_and_role_tokens_once_without_editing_quotes(self):
        quote = "我说 self 和 partner 都是标签。"
        self.fact["claim"] = (
            "self 与 partner 讨论 selfhood、partnership、self_name；self喜欢电影，SELF和Partner也是角色。"
        )
        self.fact["evidence"] = [{"msg_id": "1", "quote": quote}]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE normalized_messages SET text=? WHERE msg_id=1", (quote,))
        original = self.store()
        canonical = CanonicalStore(self.path, profile_path=self.profile)
        first = canonical.profile()
        second = canonical.profile()
        fact = first["facts"][0]
        self.assertEqual(fact["subject"], "partner")
        self.assertEqual(
            fact["claim"],
            "partner 与 self 讨论 selfhood、partnership、self_name；partner喜欢电影，partner和self也是角色。",
        )
        self.assertEqual(fact["evidence"][0]["quote"], quote)
        self.assertEqual(canonical.evidence["1"]["text"], quote)
        self.assertEqual(canonical.evidence["1"]["speaker"], "partner")
        self.assertEqual(second, first)
        self.assertEqual(original.profile()["facts"][0]["claim"], self.fact["claim"])
        self.assertEqual(original.profile()["facts"][0]["subject"], "self")
        self.assertNotIn("retain the original archive-owner perspective", first["scope"])

    def test_stale_export_cannot_survive_revoked_latest_decision(self):
        store = self.store()
        self.assertEqual(len(store.profile()["facts"]), 1)
        with closing(sqlite3.connect(self.path)) as db:
            decide(db, "f", "contested", "Later review", "test", self.fact["evidence"])
            # Even an inconsistent candidate status must not override the ledger.
            db.execute("UPDATE memory_candidates SET status='accepted' WHERE fact_id='f'")
            db.commit()
        self.assertEqual(store.profile()["facts"], [])

    def test_current_review_proof_obeys_privacy_even_when_fact_proof_is_visible(self):
        store = self.store()
        with closing(sqlite3.connect(self.path)) as db:
            decide(
                db, "f", "accepted", "Uses additional context", "test", [{"msg_id": "2", "quote": "秘密地点"}]
            )
            db.commit()
        self.assertEqual(store.profile()["facts"], [])

    def test_current_review_source_change_invalidates_old_export(self):
        store = self.store()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO normalized_messages VALUES (4,103,'partner','确实如此','text',NULL)")
            decide(db, "f", "accepted", "Context supported", "test", [{"msg_id": "4", "quote": "确实如此"}])
            db.commit()
        self.assertEqual(len(store.profile()["facts"]), 1)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE normalized_messages SET text='更正' WHERE msg_id=4")
            db.commit()
        self.assertEqual(store.profile()["facts"], [])

    def test_export_cannot_rewrite_accepted_claim_or_assert_current_validity(self):
        store = self.store()
        self.fact["current_validity"] = "established"
        self.profile.write_text(json.dumps({"facts": [self.fact]}), encoding="utf-8")
        self.assertEqual(store.profile()["facts"][0]["current_validity"], "not_established")
        self.fact["claim"] = "喜欢看电影所以现在需要电影票"
        self.profile.write_text(json.dumps({"facts": [self.fact]}), encoding="utf-8")
        self.assertEqual(store.profile()["facts"], [])

    def test_export_without_authoritative_ledger_is_not_memory(self):
        self.profile.write_text(json.dumps({"facts": [self.fact]}), encoding="utf-8")
        store = Store(self.path, profile_path=self.profile)
        self.assertEqual(store.profile()["facts"], [])

    def test_derived_claim_is_filtered_even_when_cited_message_is_allowed(self):
        self.fact["claim"] = "喜欢在秘密地点看电影"
        self.assertEqual(self.store().profile()["facts"], [])

    def test_invalid_privacy_configuration_is_rejected_not_ignored(self):
        for kwargs in (
            {"blocked_keywords": "secret"},
            {"blocked_keywords": [2]},
            {"max_history_days": -1},
            {"max_history_days": True},
            {"start_ts": True},
            {"start_ts": 100, "end_ts": 100},
            {"viewer": "unknown"},
        ):
            with self.assertRaises(ValueError):
                Store(self.path, **kwargs)

    def test_profile_rechecks_sources_after_revocation(self):
        store = self.store()
        for changed in (
            ("秘密地点", "text", None),
            ("我喜欢电影", "voice", "legacy_unreviewed"),
            ("我喜欢电影", "unknown", None),
        ):
            with self.subTest(changed=changed):
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute(
                        "UPDATE normalized_messages SET text='我喜欢电影', normalized_kind='text', voice_state=NULL WHERE msg_id=1"
                    )
                    db.commit()
                self.assertEqual(len(store.profile()["facts"]), 1)
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute(
                        "UPDATE normalized_messages SET text=?, normalized_kind=?, voice_state=? WHERE msg_id=1",
                        changed,
                    )
                    db.commit()
                self.assertEqual(store.profile()["facts"], [])

    def test_profile_applies_time_and_exclusion_filters(self):
        self.store()
        for restriction in ("since", "until", "excluded"):
            store = Store(self.path, profile_path=self.profile)
            if restriction == "since":
                store.since = 101
            elif restriction == "until":
                store.until = 100
            else:
                store.excluded_ids = frozenset({"1"})
            with self.subTest(restriction=restriction):
                self.assertEqual(store.profile()["facts"], [])
