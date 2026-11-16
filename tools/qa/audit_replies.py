"""Read-only raw reply audit; never inserts quote text into message bodies."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from maintenance.config import load_config
from maintenance.ingest.replies import extract_reply
from maintenance.ingest.snapshot import encrypted_snapshot
from maintenance.paths import ROOT
from third_party.qq_parser.msgdb.proto.c2c_40800_parser import parse_40800


def audit(source, target, partner):
    counts = Counter()
    records = []
    for mid, blob in source.execute(
        "SELECT [40001],[40800] FROM c2c_msg_table WHERE [40030]=? AND [40011]=9 ORDER BY [40001]",
        (str(partner),),
    ):
        counts["raw_replies"] += 1
        parsed = parse_40800(blob)
        if parsed.status != "typed":
            counts["parse_failed"] += 1
            records.append({"msg_id": str(mid), "parse_status": str(parsed.status)})
            continue
        reply = extract_reply(parsed.contents)
        existing = target.execute("SELECT text FROM messages WHERE msg_id=?", (mid,)).fetchone()
        counts[
            "same_body" if existing and existing[0] == reply["text"] else "body_mismatch_or_missing_row"
        ] += 1
        counts["with_body" if reply["text"] else "without_text_body"] += 1
        if reply["other_element_types"]:
            counts["with_nontext_elements"] += 1
        for ref in reply["references"]:
            counts["reference_elements"] += 1
            if not ref["msg_id"]:
                counts["reference_id_missing"] += 1
            else:
                found = target.execute(
                    "SELECT text FROM messages WHERE msg_id=?", (ref["msg_id"],)
                ).fetchone()
                counts["reference_id_found" if found else "reference_id_not_found"] += 1
        records.append({"msg_id": str(mid), "raw_sha256": hashlib.sha256(blob).hexdigest(), **reply})
    return {
        "counts": dict(counts),
        "records": records,
        "scope": "Only type 9 in the supplied source snapshot; missing reference IDs are not guessed from nearby text.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--encrypted",
        action="store_true",
        help="Audit a fresh read-only copy of the configured encrypted QQ database",
    )
    args = parser.parse_args()
    config = load_config()
    if args.output.exists():
        parser.error("Output exists; choose a new evidence path")
    source_context = (
        encrypted_snapshot(config)
        if args.encrypted
        else closing(sqlite3.connect(Path(config["source_plain_database"]).as_uri() + "?mode=ro", uri=True))
    )
    with (
        source_context as source,
        closing(sqlite3.connect((ROOT / config["database"]).as_uri() + "?mode=ro", uri=True)) as target,
    ):
        target.execute("BEGIN")
        report = audit(source, target, config["partner_qq"])
        report["source_kind"] = "fresh_encrypted_snapshot" if args.encrypted else "older_plain_snapshot"
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report["counts"]))


if __name__ == "__main__":
    main()
