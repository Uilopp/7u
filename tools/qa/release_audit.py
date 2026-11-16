"""Read-only APK audit. Reports contain counts, hashes and checks, never chat text.

Run ``python -m tools.qa.release_audit --apk releases/<package>.apk``.
The archive, credentials and expected assets come from this repository only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from maintenance.archive.memory_pack import is_eligible
from maintenance.archive.profile import CanonicalStore, Store
from maintenance.core.artifacts import publish_json
from maintenance.core.model_privacy import contains_secret, load_exclusions
from maintenance.core.references import effective_target
from maintenance.model_config import load_model_config
from maintenance.paths import ROOT
from maintenance.release import _configured_certificate, _inspect_apk, _properties, _tool_paths
from maintenance.tencent_asr_config import load as load_tencent

MEMORY_ASSET = "assets/flutter_assets/assets/memory/memory_pack.db"
PROMPT_ASSET = "assets/flutter_assets/assets/agent/prompt.md"
ROLE = {"self": "partner", "partner": "self"}


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def connect_bytes(payload):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(payload)
        db.row_factory = sqlite3.Row
        return db
    except Exception:
        db.close()
        raise


def tables(db):
    result = {}
    for name, schema in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Unexpected SQLite table name")
        columns = list(db.execute(f"PRAGMA table_info({name})"))
        order = ",".join(str(i + 1) for i in range(len(columns)))
        checksum, count = hashlib.sha256(), 0
        for record in db.execute(f"SELECT * FROM {name} ORDER BY {order}"):
            if name == "metadata" and record[0] == "built_at":
                continue
            checksum.update(json.dumps(tuple(record), ensure_ascii=False, separators=(",", ":")).encode())
            checksum.update(b"\n")
            count += 1
        result[name] = {
            "rows_excluding_built_at": count,
            "logical_sha256": checksum.hexdigest(),
            "table_schema_sha256": digest(schema.encode()),
        }
    return result


def _records(db, table, fields):
    return {
        int(row["msg_id"]): tuple(row[key] for key in fields) for row in db.execute(f"SELECT * FROM {table}")
    }


def audit_profile(profile, source, source_by_id):
    """Use the production accepted-profile export, independently check role/quotes."""
    canonical_store = CanonicalStore(source, profile_path=source.parent / "profile.json")
    canonical = canonical_store.profile()
    original = Store(source, profile_path=source.parent / "profile.json").profile()
    original_facts = {fact["id"]: fact for fact in original["facts"]}
    issues = Counter({"subject": 0, "claim": 0, "quote": 0, "source_role": 0, "missing_fact": 0})
    citations = 0
    for fact in profile.get("facts", []):
        raw = original_facts.get(fact.get("id"))
        if raw is None:
            issues["missing_fact"] += 1
            continue
        issues["subject"] += fact.get("subject") != ROLE.get(raw["subject"], raw["subject"])
        claim = re.sub(
            r"(?<![A-Za-z0-9_])(self|partner)(?![A-Za-z0-9_])",
            lambda match: ROLE[match[0].lower()],
            raw["claim"],
            flags=re.I,
        )
        issues["claim"] += fact.get("claim") != claim
        originals = {str(c["msg_id"]): c for c in raw["evidence"]}
        for citation in fact.get("evidence", []):
            citations += 1
            identifier = str(citation.get("msg_id", ""))
            row = source_by_id.get(int(identifier)) if identifier.isascii() and identifier.isdigit() else None
            proof = originals.get(identifier)
            evidence = canonical_store.evidence.get(identifier)
            issues["quote"] += (
                proof is None
                or citation.get("quote") != proof["quote"]
                or row is None
                or not isinstance(citation.get("quote"), str)
                or citation["quote"] not in (row["text"] or "")
            )
            issues["source_role"] += (
                evidence is None
                or row is None
                or evidence["speaker"] != ROLE.get(row["speaker"])
                or evidence["text"] != row["text"]
            )
    return {
        "facts": len(profile.get("facts", [])),
        "canonical_exact": profile == canonical,
        "evidence_citations": citations,
        "role_quote_issues": dict(issues),
    }


def audit_pack(payload, source):
    """Compare every shipped record/link/event with the current filtered archive."""
    source = Path(source).resolve()
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(connect_bytes(payload)) as db,
    ):
        src.row_factory = sqlite3.Row
        src.execute("BEGIN")
        rows = list(
            src.execute("SELECT msg_id,ts,speaker,text,normalized_kind,voice_state FROM normalized_messages")
        )
        source_by_id = {int(row["msg_id"]): row for row in rows}
        exclusions = load_exclusions(source.parent / "model_exclusions.json")
        expected, expected_events = {}, {}
        for row in rows:
            allowed = is_eligible(row, exclusions)
            if row["speaker"] in ROLE:
                expected_events[int(row["msg_id"])] = (
                    row["ts"],
                    ROLE[row["speaker"]],
                    row["normalized_kind"],
                    int(allowed),
                )
            if allowed:
                voice = row["normalized_kind"] == "voice"
                expected[int(row["msg_id"])] = (
                    row["ts"],
                    ROLE[row["speaker"]],
                    row["text"],
                    "voice_transcript" if voice else "message_text",
                    ("human_verified" if row["voice_state"] == "verified" else "automatic_asr")
                    if voice
                    else None,
                )
        actual = _records(db, "messages", ("ts", "speaker", "text", "source_type", "transcript_origin"))
        events = _records(db, "events", ("ts", "speaker", "kind", "text_available"))
        ids, expected_links = set(actual), set()
        for row in src.execute("SELECT msg_id,payload FROM message_references"):
            anchor = int(row["msg_id"])
            if anchor not in ids:
                continue
            try:
                refs = json.loads(row["payload"])
            except TypeError, json.JSONDecodeError:
                continue
            for reference in refs.get("references", [])[:10]:
                target = effective_target(reference)
                if target is not None and int(target) in ids:
                    expected_links.add((anchor, int(target)))
        links = set(map(tuple, db.execute("SELECT msg_id,target_msg_id FROM message_links")))
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        profile = audit_profile(json.loads(metadata["profile"]), source, source_by_id)
        source_integrity = src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        bundle_integrity = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        fingerprints = tables(db)
        report = {
            "source": {"integrity_ok": source_integrity, "messages": len(rows)},
            "profile": profile,
            "bundle": {
                "integrity_ok": bundle_integrity,
                "tables": sorted(fingerprints),
                "messages": len(actual),
                "expected_messages": len(expected),
                "missing_ids": len(set(expected) - ids),
                "unexpected_ids": len(ids - set(expected)),
                "record_mismatches": sum(actual[k] != expected[k] for k in ids & set(expected)),
                "events": len(events),
                "events_exact": events == expected_events,
                "links": len(links),
                "links_exact": links == expected_links,
                "dangling_links": sum(a not in ids or b not in ids for a, b in links),
                "excluded_text_ids": len(ids & {int(value) for value in exclusions}),
                "unusable_voice_ids": sum(
                    int(row["msg_id"]) in ids
                    and row["normalized_kind"] == "voice"
                    and row["voice_state"] not in ("verified", "asr_accepted")
                    for row in rows
                ),
                "secret_text_rows": sum(contains_secret(value[2]) for value in actual.values()),
                "voice_origins": dict(
                    db.execute(
                        "SELECT transcript_origin,count(*) FROM messages WHERE source_type='voice_transcript' GROUP BY transcript_origin"
                    )
                ),
                "speaker_counts": dict(db.execute("SELECT speaker,count(*) FROM messages GROUP BY speaker")),
            },
        }
        checks = {
            "source_integrity": source_integrity,
            "bundle_integrity": bundle_integrity,
            "only_expected_tables": sorted(fingerprints)
            == ["events", "message_links", "messages", "metadata"],
            "messages_match_source_exactly": actual == expected,
            "events_match_source_exactly": events == expected_events,
            "references_match_source_exactly": links == expected_links,
            "profile_exact_and_roles_correct": profile["canonical_exact"]
            and not any(profile["role_quote_issues"].values()),
            "no_secret_text": report["bundle"]["secret_text_rows"] == 0,
            "latest_message_metadata_exact": metadata.get("latest_message_ts")
            == str(max((row[0] for row in expected.values()), default="")),
        }
        return report, checks, fingerprints


def audit_privacy(archive, *, model_key, forbidden_values, forbidden_blobs):
    variants = {
        label: {
            value.encode(),
            value.encode("utf-16le"),
            json.dumps(value)[1:-1].encode(),
            quote(value, safe="").encode(),
            base64.b64encode(value.encode()),
        }
        for label, value in forbidden_values.items()
        if value
    }
    found = dict.fromkeys(variants, False)
    blobs = dict.fromkeys(forbidden_blobs, False)
    markers = dict.fromkeys(
        ("release-audit-draft", "test-only-key-never-sent", "model.invalid", "LIVE_FOLLOWUP_TEST"), False
    )
    forbidden_count, model_present = 0, False
    for name in archive.namelist():
        lower = name.lower()
        if (
            re.search(r"(^|/)(tests?|logs?|traces?|maintenance|voice|ingest)(/|$)", lower)
            or lower.endswith(
                (
                    ".dpapi",
                    ".jks",
                    ".keystore",
                    ".p12",
                    ".pfx",
                    ".pem",
                    ".key",
                    ".silk",
                    ".slk",
                    ".amr",
                    ".wav",
                    ".mp3",
                    ".m4a",
                    ".pcm",
                    ".ogg",
                    ".log",
                    ".jsonl",
                )
            )
            or any(
                value in lower
                for value in (
                    "couple_chat.db",
                    "7u_local.db",
                    "conversation.db",
                    "key.properties",
                    "live-followup",
                    "live_followup",
                    "release-audit",
                )
            )
        ):
            forbidden_count += 1
        payload = archive.read(name)
        for label, forms in variants.items():
            found[label] |= any(value in payload for value in forms)
        for label, value in forbidden_blobs.items():
            blobs[label] |= bool(value) and value in payload
        for label in markers:
            markers[label] |= label.encode() in payload
        model_present |= bool(model_key) and model_key.encode() in payload
    return {
        "forbidden_entry_count": forbidden_count,
        "forbidden_credential_forms_found": found,
        "pc_encrypted_config_bytes_found": blobs,
        "test_markers_found": markers,
        "intentional_model_key_present": model_present,
    }


def finish_report(report):
    report["passed"] = all(report["checks"].values())
    report["failed_checks"] = [name for name, success in report["checks"].items() if not success]
    return report


def android_output(tool, *arguments):
    result = subprocess.run(
        [str(tool), *map(str, arguments)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError("Android inspection tool failed")
    return result.stdout


def audit_release(
    apk, *, previous=None, expected_version=None, expected_code=None, require_unchanged=False, root=ROOT
):
    root, apk = Path(root).resolve(), Path(apk).resolve()
    previous = Path(previous).resolve() if previous is not None else None
    if require_unchanged and previous is None:
        raise ValueError("An unchanged-data audit requires --previous")
    if previous == apk:
        raise ValueError("The previous APK must differ from the APK under review")
    app, source = root / "app", root / "data/couple_chat.db"
    version = re.search(
        r"(?m)^version:\s*([^+\s]+)\+([0-9]+)", (app / "pubspec.yaml").read_text(encoding="utf-8")
    )
    if version is None:
        raise ValueError("Cannot determine expected version from pubspec.yaml")
    expected_version = expected_version if expected_version is not None else version[1]
    expected_code = expected_code if expected_code is not None else int(version[2])
    if type(expected_code) is not int or expected_code <= 0:
        raise ValueError("Expected version code must be a positive integer")
    config = load_model_config(root / "data/model_config.dpapi")
    if config is None:
        raise ValueError("Model configuration is required to verify the intentional embedded key")
    signing = _properties(app / "android/key.properties")
    forbidden = {f"signing_{field}": signing[field] for field in ("storePassword", "keyPassword")}
    tencent = load_tencent(root / "data/tencent_asr_config.dpapi")
    if tencent is not None:
        forbidden.update(tencent_secret_id=tencent.secret_id, tencent_secret_key=tencent.secret_key)
    blobs = {
        name: (root / "data" / name).read_bytes()
        for name in ("model_config.dpapi", "tencent_asr_config.dpapi")
        if (root / "data" / name).is_file()
    }
    with zipfile.ZipFile(apk) as archive:
        memory, prompt = archive.read(MEMORY_ASSET), archive.read(PROMPT_ASSET)
        report, checks, current_tables = audit_pack(memory, source)
        local_memory = (app / "assets/memory/memory_pack.db").read_bytes()
        local_prompt = (app / "assets/agent/prompt.md").read_bytes()
        canonical_prompt = (root / "agent/prompt.md").read_bytes()
        report.update(apk=apk.name, bytes=apk.stat().st_size, sha256=digest(apk.read_bytes()))
        report["assets"] = {
            "memory_sha256": digest(memory),
            "local_memory_sha256": digest(local_memory),
            "prompt_sha256": digest(prompt),
            "local_prompt_sha256": digest(local_prompt),
            "canonical_prompt_sha256": digest(canonical_prompt),
        }
        privacy = audit_privacy(
            archive, model_key=config.api_key, forbidden_values=forbidden, forbidden_blobs=blobs
        )
        report["privacy"] = privacy
        checks.update(
            memory_asset_exact=memory == local_memory,
            prompt_asset_exact=prompt == local_prompt == canonical_prompt,
            no_private_entries=privacy["forbidden_entry_count"] == 0,
            no_pc_credentials=not any(privacy["forbidden_credential_forms_found"].values())
            and not any(privacy["pc_encrypted_config_bytes_found"].values()),
            no_test_markers=not any(privacy["test_markers_found"].values()),
            intentional_model_key_present=privacy["intentional_model_key_present"],
        )
    if previous is not None:
        with (
            zipfile.ZipFile(previous) as archive,
            closing(connect_bytes(archive.read(MEMORY_ASSET))) as prior,
        ):
            old_tables = tables(prior)
        report["versus_previous_pack"] = {
            "previous_apk": previous.name,
            "current_tables": current_tables,
            "previous_tables": old_tables,
            "require_unchanged": require_unchanged,
            "all_tables_logically_identical_except_built_at": current_tables == old_tables,
        }
        if require_unchanged:
            checks["logical_data_same_as_previous"] = current_tables == old_tables
    _, signer = _tool_paths(app)
    suffix = ".exe" if os.name == "nt" else ""
    aapt, zipalign = signer.with_name("aapt" + suffix), signer.with_name("zipalign" + suffix)
    env = dict(os.environ)
    current = _inspect_apk(apk, aapt, signer, app=app, env=env, emit=lambda _: None)
    certificate = _configured_certificate(
        (app / "android/app" / signing["storeFile"]).resolve(), signing, app=app, env=env
    )
    badging = android_output(aapt, "dump", "badging", apk)
    alignment = subprocess.run(
        [str(zipalign), "-c", "-P", "16", "-v", "4", str(apk)],
        capture_output=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    package = re.search(r"package: name='([^']+)'", badging)
    report["package"] = {
        **current,
        "package_name": package[1] if package else None,
        "debuggable": "application-debuggable" in badging,
        "same_certificate_as_configured": current["certificate_sha256"] == certificate,
        "zipalign_16kb_exit_code": alignment.returncode,
    }
    checks.update(
        package_identity=bool(
            package
            and package[1] == "com.sevenu.app"
            and current["version_code"] == expected_code
            and current["version_name"] == expected_version
        ),
        non_debuggable=not report["package"]["debuggable"],
        permanent_signature=current["certificate_sha256"] == certificate,
        zipalign_16kb=alignment.returncode == 0,
    )
    if previous is not None:
        old = _inspect_apk(previous, aapt, signer, app=app, env=env, emit=lambda _: None)
        checks["signature_continuity"] = current["certificate_sha256"] == old["certificate_sha256"]
        checks["upgrade_version_code_increased"] = current["version_code"] > old["version_code"]
    report["checks"] = checks
    return finish_report(report)


def run_cli(auditor, argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-code", type=int)
    parser.add_argument("--require-unchanged", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = auditor(
            args.apk,
            previous=args.previous,
            expected_version=args.expected_version,
            expected_code=args.expected_code,
            require_unchanged=args.require_unchanged,
        )
        destination = ROOT / "data/qa" / (args.apk.stem + "-audit.json")
        publish_json(destination, report)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "failed_checks": report["failed_checks"],
                    "sha256": report["sha256"],
                    "report": destination.name,
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["passed"] else 1
    except Exception as error:
        # Tool/library errors can contain argv, credentials or private source paths.
        print(
            json.dumps(
                {
                    "passed": False,
                    "error_type": type(error).__name__,
                    "message": "审计未完成；请检查文件、配置和 Android 工具。",
                },
                ensure_ascii=False,
            )
        )
        return 1


def main(argv=None):
    return run_cli(audit_release, argv)


if __name__ == "__main__":
    raise SystemExit(main())
