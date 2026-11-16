"""Promote raw Tencent ASR output as the sole automatic voice transcript."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from maintenance.paths import ROOT
from maintenance.voice.tencent_asr import ENGINE, PROVIDER

MODEL = f"{PROVIDER}/{ENGINE}"
PARAMETERS = json.dumps(
    {
        "EngSerViceType": ENGINE,
        "FilterPunc": 0,
        "SourceType": 1,
        "punctuation": "tencent_asr_result",
    },
    ensure_ascii=False,
    sort_keys=True,
)


def plan(db):
    rows = db.execute(
        """SELECT m.msg_id,p.audio_path,p.voice_state,c.metrics,
        s.candidate,s.audio_sha256
        FROM messages m JOIN message_provenance p USING(msg_id)
        LEFT JOIN voice_candidates c USING(msg_id)
        LEFT JOIN voice_secondary_candidates s ON s.msg_id=m.msg_id
            AND s.provider=? AND s.engine=?
        WHERE p.raw_msg_type=6 ORDER BY m.msg_id""",
        (PROVIDER, ENGINE),
    ).fetchall()
    changes, candidates = [], []
    counts = {
        "voices": len(rows),
        "adopted": 0,
        "discarded": 0,
        "missing_audio": 0,
        "human_decisions_preserved": 0,
        "empty_tencent_candidates": 0,
        "awaiting_tencent": 0,
    }
    now = int(time.time())
    for msg_id, audio_path, state, old_metrics_json, text, stored_digest in rows:
        if state in {"verified", "rejected"}:
            counts["human_decisions_preserved"] += 1
            continue
        audio = Path(audio_path or "")
        if not audio.is_file():
            changes.append((None, "missing_audio", msg_id))
            counts["missing_audio"] += 1
            continue
        digest = hashlib.sha256(audio.read_bytes()).hexdigest()
        try:
            duration = json.loads(old_metrics_json or "{}").get("duration")
        except json.JSONDecodeError:
            duration = None
        if text is None or stored_digest != digest:
            if state == "pending":
                # A transport failure is not a silent recording. Keep it retryable.
                counts["awaiting_tencent"] += 1
                continue
            changes.append((None, "discarded", msg_id))
            counts["discarded"] += 1
            counts["empty_tencent_candidates"] += 1
            continue
        text = text.strip()
        if not text:
            changes.append((None, "discarded", msg_id))
            counts["discarded"] += 1
            counts["empty_tencent_candidates"] += 1
            continue
        metrics = {
            "audio_sha256": digest,
            "duration": duration,
            "segments": [],
            "provider": PROVIDER,
            "punctuation_source": "tencent_asr_result",
        }
        changes.append((text, "asr_accepted", msg_id))
        candidates.append(
            (msg_id, MODEL, PARAMETERS, text, json.dumps(metrics, ensure_ascii=False, sort_keys=True), now)
        )
        counts["adopted"] += 1
    return changes, candidates, counts


def adopt(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        changes, candidates, counts = plan(db)
        with db:
            db.executemany(
                "UPDATE messages SET text=? WHERE msg_id=?",
                [(text, msg_id) for text, state, msg_id in changes],
            )
            db.executemany(
                "UPDATE message_provenance SET voice_state=? WHERE msg_id=?",
                [(state, msg_id) for text, state, msg_id in changes],
            )
            counts["retired_voice_candidates_removed"] = db.execute(
                "DELETE FROM voice_candidates WHERE model<>?", (MODEL,)
            ).rowcount
            db.executemany(
                "DELETE FROM voice_candidates WHERE msg_id=?", [(msg_id,) for text, state, msg_id in changes]
            )
            db.executemany("INSERT INTO voice_candidates VALUES(?,?,?,?,?,?)", candidates)
            counts["postprocessed_punctuation_removed"] = 0
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_punctuation'"
            ).fetchone():
                counts["postprocessed_punctuation_removed"] = db.execute(
                    "SELECT count(*) FROM voice_punctuation"
                ).fetchone()[0]
                db.execute("DELETE FROM voice_punctuation")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return counts


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/couple_chat.db")
    args = parser.parse_args(argv)
    print(json.dumps(adopt(args.db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
