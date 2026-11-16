"""Explicit listening review ledger. ASR agreement never calls this automatically.

The caller must obtain a real listener decision. This module records provenance,
guards stale edits, and updates the working transcript atomically; it cannot
prove that a person listened. Original text is retained in every review event.
"""

import hashlib
import time
from pathlib import Path


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS voice_reviews (
        review_id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL,
        decision TEXT NOT NULL, reviewer TEXT NOT NULL, rationale TEXT NOT NULL,
        audio_sha256 TEXT NOT NULL, previous_text TEXT, reviewed_text TEXT,
        previous_state TEXT, created_at INTEGER NOT NULL,
        review_mode TEXT NOT NULL DEFAULT 'assisted',
        candidate_model TEXT, candidate_parameters TEXT, candidate_text TEXT)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(voice_reviews)")}
    if "review_mode" not in columns:
        db.execute("ALTER TABLE voice_reviews ADD COLUMN review_mode TEXT NOT NULL DEFAULT 'assisted'")
    for name in ("candidate_model", "candidate_parameters", "candidate_text"):
        if name not in columns:
            db.execute(f"ALTER TABLE voice_reviews ADD COLUMN {name} TEXT")


def audio_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_review(
    db,
    msg_id,
    *,
    decision,
    reviewer,
    rationale,
    expected_text,
    expected_audio_sha256,
    transcript=None,
    listened=False,
    review_mode="assisted",
    candidate_model=None,
    candidate_parameters=None,
    expected_candidate=None,
):
    if decision not in ("verified", "rejected"):
        raise ValueError("Choose verified or rejected after listening")
    if listened is not True or not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Explicit listening confirmation and reviewer required")
    if review_mode not in ("blind", "assisted"):
        raise ValueError("Review mode must be blind or assisted")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("Review explanation required")
    if decision == "verified" and (not isinstance(transcript, str) or not transcript.strip()):
        raise ValueError("A verified transcript must be nonempty")
    if decision == "rejected" and transcript is not None:
        raise ValueError("Rejected audio cannot replace the transcript")
    # All validation precedes mutation; the source text is checked again in SQL.
    row = db.execute(
        """SELECT m.text,p.audio_path,p.voice_state,p.raw_msg_type
                      FROM messages m JOIN message_provenance p USING(msg_id)
                      WHERE m.msg_id=?""",
        (msg_id,),
    ).fetchone()
    if row is None or row[3] != 6:
        raise ValueError("Voice source not found")
    if row[0] != expected_text:
        raise ValueError("Transcript changed since review started")
    if not row[1] or not Path(row[1]).is_file():
        raise ValueError("Cannot verify missing audio")
    digest = audio_digest(row[1])
    if digest != expected_audio_sha256:
        raise ValueError("Audio changed since review started")
    candidate_snapshot = None
    provided = (candidate_model, candidate_parameters, expected_candidate)
    if any(value is not None for value in provided):
        if not all(isinstance(value, str) for value in provided):
            raise ValueError("Complete candidate proof required")
        candidate_snapshot = db.execute(
            """SELECT candidate FROM voice_candidates
            WHERE msg_id=? AND model=? AND parameters=?""",
            (msg_id, candidate_model, candidate_parameters),
        ).fetchone()
        if candidate_snapshot is None or candidate_snapshot[0] != expected_candidate:
            raise ValueError("ASR candidate changed since review started")
    with db:
        if decision == "verified":
            updated = db.execute(
                "UPDATE messages SET text=? WHERE msg_id=? AND text IS ?", (transcript, msg_id, expected_text)
            )
        else:
            updated = db.execute(
                "UPDATE messages SET text=text WHERE msg_id=? AND text IS ?", (msg_id, expected_text)
            )
        if updated.rowcount != 1:
            raise ValueError("Concurrent transcript change")
        db.execute(
            """INSERT INTO voice_reviews
            (msg_id,decision,reviewer,rationale,audio_sha256,previous_text,reviewed_text,
             previous_state,created_at,review_mode,candidate_model,candidate_parameters,candidate_text)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg_id,
                decision,
                reviewer.strip(),
                rationale.strip(),
                digest,
                row[0],
                transcript,
                row[2],
                int(time.time()),
                review_mode,
                candidate_model,
                candidate_parameters,
                candidate_snapshot[0] if candidate_snapshot else None,
            ),
        )
        db.execute("UPDATE message_provenance SET voice_state=? WHERE msg_id=?", (decision, msg_id))


def record_administrative_discard(db, msg_id, *, reviewer, rationale):
    """Record an explicit owner discard without claiming that audio was heard."""
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Discard reviewer required")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("Discard rationale required")
    initialize(db)
    row = db.execute(
        """SELECT m.text,p.audio_path,p.voice_state,p.raw_msg_type
        FROM messages m JOIN message_provenance p USING(msg_id)
        WHERE m.msg_id=?""",
        (msg_id,),
    ).fetchone()
    if row is None or row[3] != 6:
        raise ValueError("Voice source not found")
    digest = audio_digest(row[1]) if row[1] and Path(row[1]).is_file() else hashlib.sha256(b"").hexdigest()
    with db:
        db.execute(
            """INSERT INTO voice_reviews
            (msg_id,decision,reviewer,rationale,audio_sha256,previous_text,reviewed_text,
             previous_state,created_at,review_mode,candidate_model,candidate_parameters,candidate_text)
            VALUES (?,?,?,?,?,?,?,?,?,'administrative',NULL,NULL,NULL)""",
            (
                msg_id,
                "rejected",
                reviewer.strip(),
                rationale.strip(),
                digest,
                row[0],
                None,
                row[2],
                int(time.time()),
            ),
        )
        db.execute("UPDATE messages SET text=NULL WHERE msg_id=?", (msg_id,))
        db.execute("UPDATE message_provenance SET voice_state='rejected' WHERE msg_id=?", (msg_id,))
