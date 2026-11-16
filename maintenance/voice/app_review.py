"""Local, message-ID-based voice repair with listening and stale-edit guards."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance.core.identifiers import message_id
from maintenance.voice.review import initialize, record_review

BEIJING = timezone(timedelta(hours=8))


def _beijing_datetime(value):
    timestamp = int(value)
    if abs(timestamp) > 100_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, BEIJING).strftime("%Y-%m-%d %H:%M")


def _secondary_candidate(db, identifier, audio_sha256):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_secondary_candidates'"
    ).fetchone():
        return None
    row = db.execute(
        """SELECT candidate FROM voice_secondary_candidates
        WHERE msg_id=? AND provider='tencent_cloud_asr' AND audio_sha256=?
        ORDER BY created_at DESC LIMIT 1""",
        (int(identifier), audio_sha256),
    ).fetchone()
    return row[0] if row else None


def item_by_id(db_path, identifier):
    identifier = message_id(identifier)
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            """SELECT msg_id,ts,speaker,text,audio_path,voice_state
            FROM normalized_messages WHERE normalized_kind='voice' AND msg_id=?""",
            (int(identifier),),
        ).fetchone()
        if row is None:
            raise ValueError("没有找到这个消息 ID 对应的语音")
        path = Path(row["audio_path"] or "")
        if not path.is_file():
            raise ValueError("这条语音的原音不在本机，请先在 QQ 下载原音并重新同步")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        secondary = _secondary_candidate(db, identifier, digest)
        candidate = None
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_candidates'"
        ).fetchone():
            for option in db.execute(
                """SELECT model,parameters,candidate,metrics
                    FROM voice_candidates WHERE msg_id=? ORDER BY created_at DESC""",
                (int(identifier),),
            ):
                try:
                    proof = json.loads(option["metrics"])
                    if not isinstance(proof, dict) or proof.get("audio_sha256") != digest:
                        continue
                except TypeError, ValueError:
                    continue
                candidate = option
                break
    item = {
        "msg_id": str(row["msg_id"]),
        "speaker": row["speaker"],
        "current_text": row["text"],
        "voice_state": row["voice_state"],
        "candidate": row["text"] or (candidate["candidate"] if candidate else "") or secondary or "",
        "occurred_at_beijing": _beijing_datetime(row["ts"]),
        "expected_text": row["text"],
        "expected_audio_sha256": digest,
        "secondary_candidate": secondary,
    }
    if candidate is not None:
        item.update(
            candidate_model=candidate["model"],
            candidate_parameters=candidate["parameters"],
            expected_candidate=candidate["candidate"],
        )
    return {"status": "pending", "item": item}


def submit(db_path, body):
    required = {"msg_id", "decision", "expected_text", "expected_audio_sha256", "transcript", "listened"}
    candidate_proof = {"candidate_model", "candidate_parameters", "expected_candidate"}
    if (
        not isinstance(body, dict)
        or not required.issubset(body)
        or set(body) - required - {"review_mode"} - candidate_proof
        or bool(candidate_proof & set(body)) != candidate_proof.issubset(body)
    ):
        raise ValueError("Invalid voice review request")
    identifier = message_id(body["msg_id"])
    if (
        not isinstance(body["expected_text"], (str, type(None)))
        or not isinstance(body["expected_audio_sha256"], str)
        or len(body["expected_audio_sha256"]) != 64
        or not isinstance(body["listened"], bool)
    ):
        raise ValueError("Invalid voice review proof")
    transcript = body["transcript"]
    review_mode = body.get("review_mode", "assisted")
    if review_mode not in ("blind", "assisted"):
        raise ValueError("Invalid voice review mode")
    if transcript is not None and (not isinstance(transcript, str) or len(transcript) > 8000):
        raise ValueError("Invalid reviewed transcript")
    with closing(sqlite3.connect(db_path)) as db:
        initialize(db)
        record_review(
            db,
            int(identifier),
            decision=body["decision"],
            reviewer="local_app_user",
            rationale="在 App 中播放原音后人工核对",
            expected_text=body["expected_text"],
            expected_audio_sha256=body["expected_audio_sha256"],
            transcript=transcript,
            listened=body["listened"],
            review_mode=review_mode,
            candidate_model=body.get("candidate_model"),
            candidate_parameters=body.get("candidate_parameters"),
            expected_candidate=body.get("expected_candidate"),
        )
    return {"status": "recorded", "msg_id": identifier, "decision": body["decision"]}
