"""Build the privacy-filtered, app-bundled 7u memory database.

The source archive never ships as-is. This exporter keeps only model-visible
authored text, accepted voice transcripts, exact reply links between exported
messages, and reviewed profile facts. Speaker names are converted once here to
the gift recipient's perspective: self=her, partner=giver.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path

from maintenance.archive.profile import CanonicalStore
from maintenance.core.messages import PLACEHOLDERS
from maintenance.core.model_privacy import contains_secret, load_exclusions
from maintenance.core.references import effective_target
from maintenance.core.voice_text import USABLE_VOICE_STATES
from maintenance.paths import ROOT

DEFAULT_SOURCE = ROOT / "data" / "couple_chat.db"
DEFAULT_OUTPUT = ROOT / "app" / "assets" / "memory" / "memory_pack.db"


def is_eligible(row: sqlite3.Row, excluded_ids: set[str]) -> bool:
    text = row["text"]
    return bool(
        text
        and row["speaker"] in ("self", "partner")
        and row["normalized_kind"] not in ("sys", "unknown")
        and (row["normalized_kind"] != "voice" or row["voice_state"] in USABLE_VOICE_STATES)
        and str(row["msg_id"]) not in excluded_ids
        and text not in PLACEHOLDERS
        and not contains_secret(text)
    )


def build(source: Path, output: Path, *, sync_prompt: bool = True) -> dict[str, int | str | None]:
    source = source.resolve()
    output = output.resolve()
    if source == output:
        raise ValueError("Source archive and output memory pack must differ")
    if not source.is_file():
        raise FileNotFoundError(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".building.db")
    temporary.unlink(missing_ok=True)

    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(temporary)
    excluded_ids = load_exclusions(source.parent / "model_exclusions.json")
    exported_ids: set[int] = set()
    try:
        dst.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE messages(
              msg_id INTEGER PRIMARY KEY,
              ts INTEGER NOT NULL,
              speaker TEXT NOT NULL CHECK(speaker IN ('self','partner')),
              text TEXT NOT NULL,
              source_type TEXT NOT NULL,
              transcript_origin TEXT
            );
            CREATE INDEX messages_time ON messages(ts,msg_id);
            CREATE INDEX messages_speaker_time ON messages(speaker,ts,msg_id);
            CREATE TABLE message_links(
              msg_id INTEGER NOT NULL,
              target_msg_id INTEGER NOT NULL,
              PRIMARY KEY(msg_id,target_msg_id)
            );
            CREATE TABLE events(
              msg_id INTEGER PRIMARY KEY,
              ts INTEGER NOT NULL,
              speaker TEXT NOT NULL,
              kind TEXT NOT NULL,
              text_available INTEGER NOT NULL
            );
            CREATE INDEX events_time ON events(ts,msg_id);
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """
        )
        rows = src.execute(
            """SELECT msg_id,ts,speaker,text,normalized_kind,voice_state
               FROM normalized_messages ORDER BY ts,msg_id"""
        )
        latest = None
        voice_count = 0
        for row in rows:
            if row["speaker"] in ("self", "partner"):
                event_speaker = "partner" if row["speaker"] == "self" else "self"
                dst.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?)",
                    (
                        int(row["msg_id"]),
                        int(row["ts"]),
                        event_speaker,
                        row["normalized_kind"],
                        int(is_eligible(row, excluded_ids)),
                    ),
                )
            if not is_eligible(row, excluded_ids):
                continue
            identifier = int(row["msg_id"])
            # The archive is stored from the giver's perspective. Convert once.
            speaker = "partner" if row["speaker"] == "self" else "self"
            is_voice = row["normalized_kind"] == "voice"
            origin = None
            if is_voice:
                voice_count += 1
                origin = "human_verified" if row["voice_state"] == "verified" else "automatic_asr"
            dst.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    int(row["ts"]),
                    speaker,
                    row["text"],
                    "voice_transcript" if is_voice else "message_text",
                    origin,
                ),
            )
            exported_ids.add(identifier)
            latest = max(latest or int(row["ts"]), int(row["ts"]))

        for row in src.execute("SELECT msg_id,payload FROM message_references"):
            anchor = int(row["msg_id"])
            if anchor not in exported_ids:
                continue
            try:
                payload = json.loads(row["payload"])
            except TypeError, json.JSONDecodeError:
                continue
            for reference in payload.get("references", [])[:10]:
                target = effective_target(reference)
                if target is not None and int(target) in exported_ids:
                    dst.execute(
                        "INSERT OR IGNORE INTO message_links VALUES(?,?)",
                        (anchor, int(target)),
                    )

        # CanonicalStore applies the same privacy and accepted-review policy.
        profile = CanonicalStore(source, profile_path=source.parent / "profile.json").profile()
        metadata = {
            "schema_version": "1",
            "built_at": str(int(time.time())),
            "latest_message_ts": str(latest or ""),
            "profile": json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
        }
        dst.executemany("INSERT INTO metadata VALUES(?,?)", metadata.items())
        dst.commit()
        dst.execute("VACUUM")
        link_count = dst.execute("SELECT count(*) FROM message_links").fetchone()[0]
    finally:
        src.close()
        dst.close()
    temporary.replace(output)
    if sync_prompt:
        prompt_output = ROOT / "app" / "assets" / "agent" / "prompt.md"
        prompt_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "agent" / "prompt.md", prompt_output)
    return {
        "output": str(output),
        "messages": len(exported_ids),
        "voice_transcripts": voice_count,
        "links": link_count,
        "latest_message_ts": latest,
        "bytes": output.stat().st_size,
        "prompt_synced": sync_prompt,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
