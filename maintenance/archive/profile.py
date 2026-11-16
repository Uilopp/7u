"""Read-only, privacy-filtered profile export for the bundled memory database."""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance.archive.authority import accepted_review_evidence
from maintenance.core.messages import PLACEHOLDERS
from maintenance.core.model_privacy import contains_secret, load_exclusions
from maintenance.core.voice_text import USABLE_VOICE_STATES
from maintenance.paths import ROOT

TZ = timezone(timedelta(hours=8))


class Store:
    """Validate an accepted profile against its current archive and review ledger."""

    def __init__(
        self,
        db=None,
        *,
        blocked_keywords=(),
        max_history_days=None,
        viewer="self",
        profile_path=None,
        start_ts=None,
        end_ts=None,
    ):
        self.path = Path(db or ROOT / "data/couple_chat.db")
        self.profile_path = Path(profile_path) if profile_path else ROOT / "data/profile.json"
        self.excluded_ids = load_exclusions(self.path.parent / "model_exclusions.json")
        if not isinstance(blocked_keywords, (list, tuple)) or any(
            not isinstance(k, str) for k in blocked_keywords
        ):
            raise ValueError("Blocked keywords must be a list of strings")
        self.blocked = tuple(k.strip() for k in blocked_keywords if k.strip())
        if len(self.blocked) > 100 or any(len(k) > 200 for k in self.blocked):
            raise ValueError("Too many or overly long blocked keywords")
        if max_history_days is not None and (
            type(max_history_days) is not int or not 1 <= max_history_days <= 36500
        ):
            raise ValueError("History days must be a positive integer")
        self.since = int(time.time()) - max_history_days * 86400 if max_history_days is not None else 0
        for bound in (start_ts, end_ts):
            if bound is not None and (type(bound) is not int or not 0 <= bound <= 253402300799):
                raise ValueError("Time bounds must be nonnegative integer Unix seconds")
        if start_ts is not None and end_ts is not None and start_ts >= end_ts:
            raise ValueError("Time range end must be exclusive and later than start")
        self.since = max(self.since, start_ts or 0)
        self.until = end_ts
        if viewer not in ("self", "partner"):
            raise ValueError("Unknown viewer")
        self.viewer = viewer
        # Release auditing compares this exact proof with the exported database.
        self.evidence = {}

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def profile(self):
        """Export only accepted facts whose complete proof still passes privacy."""
        if not self.profile_path.exists():
            return {"status": "not_consolidated", "facts": []}
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        safe = []
        with self.connect() as conn:
            # Freeze ledger decisions and source checks to one read snapshot.
            conn.execute("BEGIN")
            for fact in profile.get("facts", []):
                if not isinstance(fact, dict):
                    continue
                if (
                    not isinstance(fact.get("claim"), str)
                    or contains_secret(fact["claim"])
                    or any(w in fact["claim"] for w in self.blocked)
                ):
                    continue
                citations = fact.get("evidence", [])
                if not citations:
                    continue
                review_proof = accepted_review_evidence(conn, fact)
                if review_proof is None:
                    continue
                accepted = True
                source_times = []
                for citation in citations + review_proof:
                    row = conn.execute(
                        "SELECT * FROM normalized_messages WHERE msg_id=?", (citation["msg_id"],)
                    ).fetchone()
                    if (
                        row is None
                        or row["ts"] < self.since
                        or (self.until is not None and row["ts"] >= self.until)
                        or str(citation["msg_id"]) in self.excluded_ids
                        or contains_secret(row["text"])
                        or any(w in (row["text"] or "") for w in self.blocked)
                        or row["speaker"] not in ("self", "partner")
                        or row["text"] in PLACEHOLDERS
                        or row["normalized_kind"] in ("sys", "unknown")
                        or (
                            row["normalized_kind"] == "voice"
                            and row["voice_state"] not in USABLE_VOICE_STATES
                        )
                        or not citation.get("quote")
                        or citation["quote"] not in (row["text"] or "")
                    ):
                        accepted = False
                        break
                    if citation in citations:
                        source_times.append(row["ts"])
                if not accepted:
                    continue
                # Internal review rationale may contain additional private context.
                allowed = (
                    "id",
                    "subject",
                    "category",
                    "claim",
                    "confidence",
                    "evidence",
                    "first_evidence_ts",
                    "last_evidence_ts",
                    "temporal_scope",
                    "current_validity",
                )
                fact = {key: fact[key] for key in allowed if key in fact}
                fact["temporal_scope"] = "historical_evidence_only"
                fact["current_validity"] = "not_established"
                fact["first_evidence_ts"] = min(source_times)
                fact["last_evidence_ts"] = max(source_times)
                fact_evidence = []
                for citation in citations:
                    source_row = conn.execute(
                        """SELECT normalized_kind,voice_state
                        FROM normalized_messages WHERE msg_id=?""",
                        (citation["msg_id"],),
                    ).fetchone()
                    item = {"msg_id": str(citation["msg_id"]), "quote": citation["quote"]}
                    if source_row and source_row["normalized_kind"] == "voice":
                        item["source_type"] = "voice_transcript"
                        item["transcript_origin"] = (
                            "human_verified" if source_row["voice_state"] == "verified" else "automatic_asr"
                        )
                    fact_evidence.append(item)
                fact["evidence"] = fact_evidence
                if any(w in json.dumps(fact, ensure_ascii=False) for w in self.blocked):
                    continue
                if self.viewer == "partner" and fact.get("subject") in ("self", "partner"):
                    fact["subject"] = {"self": "partner", "partner": "self"}[fact["subject"]]
                if self.viewer == "partner":
                    # Original evidence stays verbatim; translate only narrative role tokens.
                    fact["claim"] = re.sub(
                        r"(?<![A-Za-z0-9_])(self|partner)(?![A-Za-z0-9_])",
                        lambda match: {"self": "partner", "partner": "self"}[match[0].lower()],
                        fact["claim"],
                        flags=re.IGNORECASE,
                    )
                safe.append(fact)
                for citation in citations:
                    row = conn.execute(
                        """SELECT text,speaker,ts,normalized_kind,voice_state
                        FROM normalized_messages WHERE msg_id=?""",
                        (citation["msg_id"],),
                    ).fetchone()
                    speaker = row["speaker"]
                    if self.viewer == "partner" and speaker in ("self", "partner"):
                        speaker = {"self": "partner", "partner": "self"}[speaker]
                    evidence = {
                        "text": row["text"],
                        "speaker": speaker,
                        "date": datetime.fromtimestamp(row["ts"], TZ).isoformat(),
                        "normalized_kind": row["normalized_kind"],
                        "voice_state": row["voice_state"],
                    }
                    if row["normalized_kind"] == "voice":
                        evidence["source_type"] = "voice_transcript"
                        evidence["transcript_origin"] = (
                            "human_verified" if row["voice_state"] == "verified" else "automatic_asr"
                        )
                    else:
                        evidence["source_type"] = "message_text"
                    self.evidence[str(citation["msg_id"])] = evidence
        # Review/export insertion order is not event recency.
        safe.sort(key=lambda fact: (-fact["last_evidence_ts"], -fact["first_evidence_ts"], fact["id"]))
        return {
            "status": "available",
            "facts": safe,
            "ordering": "last_evidence_desc",
            "scope": "Historical facts whose snapshot, latest accepted decision, and all supporting evidence pass current filters. Subject labels and standalone self/partner role words in claim text follow the current viewer. Evidence quotes and original message text remain verbatim speech: first-person pronouns in those quotes belong to the quoted speaker, not necessarily the current viewer. Empty results do not prove absence of preferences; current validity is not established.",
        }


class CanonicalStore(Store):
    """Translate imported giver perspective once: self=her, partner=giver."""

    def __init__(
        self,
        db=None,
        *,
        blocked_keywords=(),
        max_history_days=None,
        profile_path=None,
        start_ts=None,
        end_ts=None,
    ):
        super().__init__(
            db,
            blocked_keywords=blocked_keywords,
            max_history_days=max_history_days,
            viewer="partner",
            profile_path=profile_path,
            start_ts=start_ts,
            end_ts=end_ts,
        )
