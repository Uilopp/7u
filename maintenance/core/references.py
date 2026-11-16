"""Derived reply metadata, separate from message bodies and model evidence."""

import json


def effective_target(reference):
    if reference.get("resolution_status") in ("conflicting_explicit_id", "ambiguous"):
        return None
    if reference.get("msg_id") is not None:
        return reference["msg_id"]
    if (
        reference.get("resolution_status") == "unique"
        and reference.get("resolution_method") == "conversation_uid_sequence_exact_timestamp_v1"
    ):
        return reference.get("resolved_msg_id")
    return None


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS message_references (
        msg_id INTEGER PRIMARY KEY REFERENCES messages(msg_id),
        raw_sha256 TEXT NOT NULL, payload TEXT NOT NULL)""")


def save(db, msg_id, raw_sha256, reply):
    """Caller owns transaction. Reject stale/incorrect body extraction."""
    row = db.execute("SELECT text FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
    if row is None or row[0] != reply["text"]:
        raise ValueError("Reply metadata does not match stored authored text")
    db.execute(
        """INSERT INTO message_references VALUES (?,?,?)
        ON CONFLICT(msg_id) DO UPDATE SET raw_sha256=excluded.raw_sha256,payload=excluded.payload
        WHERE raw_sha256<>excluded.raw_sha256 OR payload<>excluded.payload""",
        (msg_id, raw_sha256, json.dumps(reply, ensure_ascii=False, sort_keys=True)),
    )


def archive_references(db, msg_id):
    """Archive-only metadata. Availability is checked now, never guessed.

    Do not send this unfiltered archive payload directly to a model. Model-side
    reference retrieval must separately apply privacy/voice/source eligibility.
    """
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_references'"
    ).fetchone():
        return []
    row = db.execute("SELECT payload FROM message_references WHERE msg_id=?", (msg_id,)).fetchone()
    if row is None:
        return []
    result = []
    for reference in json.loads(row[0])["references"]:
        identifier = effective_target(reference)
        found = (
            identifier is not None
            and db.execute("SELECT 1 FROM normalized_messages WHERE msg_id=?", (identifier,)).fetchone()
        )
        result.append(
            {
                **reference,
                "original_msg_id": reference.get("msg_id"),
                "msg_id": identifier,
                "status": "unresolved_conflict"
                if reference.get("resolution_status") in ("conflicting_explicit_id", "ambiguous")
                else "available"
                if found
                else "missing_record"
                if identifier
                else "missing_identifier",
            }
        )
    return result
