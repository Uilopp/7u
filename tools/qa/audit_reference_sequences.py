"""Validate exact UID/sequence/timestamp keys against explicit QQ reference IDs."""

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from maintenance.config import load_config
from maintenance.ingest.reference_resolution import build_index, resolve_reference
from maintenance.ingest.snapshot import encrypted_snapshot
from maintenance.paths import ROOT
from tools.qa.audit_replies import audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new report path")
    config = load_config()
    counts = Counter()
    records = []
    with (
        encrypted_snapshot(config) as source,
        closing(sqlite3.connect((ROOT / config["database"]).as_uri() + "?mode=ro", uri=True)) as target,
    ):
        target.execute("BEGIN")
        report = audit(source, target, config["partner_qq"])
        index = build_index(
            source.execute(
                "SELECT [40001],[40020],[40005],[40050] FROM c2c_msg_table WHERE [40030]=?",
                (str(config["partner_qq"]),),
            )
        )
        for record in report["records"]:
            for ref in record.get("references", []):
                resolved = resolve_reference(ref, index)
                group = "explicit" if ref.get("msg_id") else "missing_id"
                counts[group + ":" + resolved["status"]] += 1
                if resolved["status"] == "unique":
                    exists = (
                        target.execute(
                            "SELECT 1 FROM messages WHERE msg_id=?", (resolved["msg_id"],)
                        ).fetchone()
                        is not None
                    )
                    resolved["in_target"] = exists
                    counts[group + ":in_target"] += exists
                records.append({"reply_id": record["msg_id"], "explicit_id": ref.get("msg_id"), **resolved})
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "counts": dict(counts),
                "records": records,
                "method": "Same conversation plus exact sender UID + raw 40005 sequence + exact timestamp. No text matching.",
                "mutated_message_or_reference_data": False,
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )
    print(json.dumps(dict(counts)))


if __name__ == "__main__":
    main()
