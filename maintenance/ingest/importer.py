"""Idempotent import with sender provenance, one transaction, no msg_id monotonicity assumption."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from maintenance.core import references
from maintenance.ingest.reference_resolution import build_index, enrich_reply
from maintenance.ingest.replies import extract_reply

SELECT = """SELECT [40001] AS msg_id,[40050] AS timestamp,[40013] AS direction,
    [40020] AS sender_uid,[40033] AS sender_qq,[40021] AS peer_uid,
    [40030] AS peer_qq,[40011] AS msg_type,[40800] AS blob
    FROM c2c_msg_table WHERE [40030]=? ORDER BY [40001]"""


def import_connection(source, target, config, parser=None):
    if parser is None:
        from third_party.qq_parser.msgdb.c2c.parser import parse_row

        parser = parse_row
    known = {r[0] for r in target.execute("SELECT msg_id FROM messages")}
    missing_audio = {
        r[0]
        for r in target.execute(
            "SELECT msg_id FROM message_provenance WHERE voice_state IN ('missing_audio','missing_reference')"
        )
    }
    cursor = source.execute(SELECT, (str(config["partner_qq"]),))
    columns = [c[0] for c in cursor.description]
    assets = {p.name.lower(): str(p) for p in Path(config["audio_directory"]).rglob("*.amr")}
    new = []
    recovered_audio = []
    reply_metadata = []
    scanned = 0
    for raw in cursor:
        record = dict(zip(columns, raw))
        scanned += 1
        if record["msg_type"] == 9:
            from third_party.qq_parser.msgdb.proto.c2c_40800_parser import parse_40800

            parsed = parse_40800(record["blob"])
            if parsed.status == "typed":
                reply_metadata.append(
                    (
                        record["msg_id"],
                        hashlib.sha256(record["blob"]).hexdigest(),
                        extract_reply(parsed.contents),
                    )
                )
        if record["msg_id"] in known:
            if record["msg_id"] in missing_audio and record["msg_type"] == 6:
                match = re.search(rb"([0-9a-fA-F]{32})\.amr", bytes(record["blob"] or b""))
                md5 = match.group(1).decode().lower() if match else None
                path = assets.get(md5 + ".amr") if md5 else None
                if path:
                    recovered_audio.append((md5, path, record["msg_id"]))
            continue
        message = parser(record).to_db_row()
        content = json.loads(message["content"]) if message["content"] else {}
        kind = content.get("type", "text" if message["text"] else "unknown")
        sender = record["sender_qq"]
        speaker = {config["self_qq"]: "self", config["partner_qq"]: "partner"}.get(sender, "unknown")
        md5 = path = state = None
        if record["msg_type"] == 6:
            kind = "voice"
            match = re.search(rb"([0-9a-fA-F]{32})\.amr", bytes(record["blob"] or b""))
            md5 = match.group(1).decode().lower() if match else None
            path = assets.get(md5 + ".amr") if md5 else None
            state = "pending" if path else "missing_audio" if md5 else "missing_reference"
        new.append(
            (
                (
                    message["msg_id"],
                    message["timestamp"],
                    message["direction"],
                    message["content_type"],
                    kind,
                    message["text"],
                    message["content"],
                ),
                (
                    message["msg_id"],
                    sender,
                    speaker,
                    message["direction"],
                    record["msg_type"],
                    md5,
                    path,
                    state,
                ),
            )
        )
    if reply_metadata:
        reference_index = build_index(
            source.execute(
                "SELECT [40001],[40020],[40005],[40050] FROM c2c_msg_table WHERE [40030]=?",
                (str(config["partner_qq"]),),
            )
        )
        reply_metadata = [
            (identifier, digest, enrich_reply(reply, reference_index))
            for identifier, digest, reply in reply_metadata
        ]
    # Parsing completes before the transaction. Any parsing failure aborts the entire run.
    with target:
        references.initialize(target)
        target.execute(
            "CREATE TABLE IF NOT EXISTS ingest_runs(id INTEGER PRIMARY KEY,created_at INTEGER,scanned INTEGER,added INTEGER)"
        )
        added = 0
        for message, provenance in new:
            result = target.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)", message)
            if result.rowcount:
                target.execute("INSERT INTO message_provenance VALUES (?,?,?,?,?,?,?,?)", provenance)
                added += 1
        for identifier, digest, reply in reply_metadata:
            references.save(target, identifier, digest, reply)
        target.executemany(
            """UPDATE message_provenance
            SET audio_md5=?,audio_path=?,voice_state='pending'
            WHERE msg_id=? AND voice_state IN ('missing_audio','missing_reference')""",
            recovered_audio,
        )
        target.execute(
            "INSERT INTO ingest_runs(created_at,scanned,added) VALUES (?,?,?)",
            (int(time.time()), scanned, added),
        )
    return {
        "scanned": scanned,
        "added": added,
        "unchanged": scanned - added,
        "recovered_audio": len(recovered_audio),
    }
