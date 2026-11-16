"""Private, read-only package inspection for the background/reminder release."""

import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance.daily_memories import validate_prepared_pool
from tools.qa import release_audit as prior


def audit_daily_pool(payload, db):
    """Independently inspect the actual APK bytes; never report private quotes."""
    try:
        pool = json.loads(payload)
    except UnicodeError, json.JSONDecodeError:
        return {
            "items": 0,
            "kinds": {},
            "invalid_ids": ["invalid_json"],
            "schema_valid": False,
            "text_max_utf16": 0,
            "source_anchors_exact": False,
        }
    if (
        not isinstance(pool, dict)
        or type(pool.get("version")) is not int
        or pool.get("version") != 1
        or not isinstance(pool.get("items"), list)
    ):
        return {
            "items": 0,
            "kinds": {},
            "invalid_ids": ["invalid_schema"],
            "schema_valid": False,
            "text_max_utf16": 0,
            "source_anchors_exact": False,
        }
    items = pool["items"]
    errors, seen_ids, seen_messages, seen_quotes, seen_texts = [], set(), set(), set(), set()
    kinds, max_length = Counter(), 0
    for index, item in enumerate(items):
        required = ("id", "kind", "text", "date", "speaker", "quote", "msgId", "anchor")
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in required):
            errors.append(f"item_{index}_schema")
            continue
        kinds[item["kind"]] += 1
        text, quote, anchor = item["text"], item["quote"], item["anchor"]
        try:
            size, quote_size = len(text.encode("utf-16-le")) // 2, len(quote.encode("utf-16-le")) // 2
        except UnicodeError:
            errors.append(f"item_{index}_encoding")
            continue
        max_length = max(max_length, size)
        row = db.execute("SELECT * FROM messages WHERE msg_id=?", (item["msgId"],)).fetchone()
        quote_key = re.sub(r"[\W_]+", "", quote).casefold()
        text_key = re.sub(r"[\W_]+", "", text).casefold()
        expected_id = prior.digest(f"{item['msgId']}\n{quote}\n{item['kind']}\n{text}\n{anchor}".encode())[
            :24
        ]
        if (
            row is None
            or not re.fullmatch(r"[0-9]{1,19}", item["msgId"])
            or item["kind"] not in ("question", "statement", "memory")
            or not 2 <= size <= 15
            or text != text.strip()
            or any(c in text for c in "\n\r\t")
            or not 6 <= quote_size <= 28
            or quote != quote.strip()
            or any(c in quote for c in "\n\r\t")
            or not re.fullmatch(r"[\u3400-\u9fff]{2,6}", anchor)
            or anchor not in quote
            or anchor not in text
            or item["id"] != expected_id
            or item["id"] in seen_ids
            or item["msgId"] in seen_messages
            or quote_key in seen_quotes
            or text_key in seen_texts
            or (
                row is not None
                and (
                    quote not in row["text"]
                    or item["speaker"] != row["speaker"]
                    or row["source_type"] != "message_text"
                    or datetime.fromtimestamp(row["ts"], timezone(timedelta(hours=8))).date().isoformat()
                    != item["date"]
                )
            )
        ):
            errors.append(f"item_{index}")
        seen_ids.add(item["id"])
        seen_messages.add(item["msgId"])
        seen_quotes.add(quote_key)
        seen_texts.add(text_key)
    return {
        "items": len(items),
        "kinds": dict(kinds),
        "invalid_ids": errors,
        "schema_valid": True,
        "text_max_utf16": max_length,
        "source_anchors_exact": not errors,
    }


def daily_size_checks(daily):
    kinds = daily["kinds"]
    return {
        "daily_count_15_to_60": 15 <= daily["items"] <= 60,
        "daily_each_direction_3_to_20": (
            set(kinds) == {"question", "statement", "memory"}
            and all(3 <= count <= 20 for count in kinds.values())
            and sum(kinds.values()) == daily["items"]
        ),
    }


def audit_release(
    apk, *, previous=None, expected_version=None, expected_code=None, require_unchanged=False, root=prior.ROOT
):
    root, apk = Path(root).resolve(), Path(apk).resolve()
    report = prior.audit_release(
        apk,
        previous=previous,
        expected_version=expected_version,
        expected_code=expected_code,
        require_unchanged=require_unchanged,
        root=root,
    )
    checks = report["checks"]
    with zipfile.ZipFile(apk) as archive:
        payload = archive.read("assets/flutter_assets/assets/memory/daily_memories.json")
        db = prior.connect_bytes(archive.read(prior.MEMORY_ASSET))
        try:
            daily = audit_daily_pool(payload, db)
        finally:
            db.close()
        report["daily_memories"] = {
            **daily,
            "asset_sha256": prior.digest(payload),
            "local_asset_exact": payload == (root / "app/assets/memory/daily_memories.json").read_bytes(),
        }
        checks["daily_evidence_and_identity"] = daily["schema_valid"] and daily["source_anchors_exact"]
        checks.update(daily_size_checks(daily))
        checks["daily_max_15_utf16"] = 2 <= daily["text_max_utf16"] <= 15
        checks["daily_asset_exact"] = report["daily_memories"]["local_asset_exact"]
    try:
        prepared = validate_prepared_pool(
            root / "data/couple_chat.db", root / "app/assets/memory/daily_memories.json"
        )
        checks["daily_archive_fingerprint_and_prompt_current"] = (
            prepared["sha256"] == report["daily_memories"]["asset_sha256"]
        )
    except Exception as exc:
        checks["daily_archive_fingerprint_and_prompt_current"] = False
        report["daily_memories"]["prepared_validation_error_type"] = type(exc).__name__
    _, signer = prior._tool_paths(root / "app")
    aapt = signer.with_name("aapt.exe" if prior.os.name == "nt" else "aapt")
    permissions = prior.android_output(aapt, "dump", "permissions", apk)
    actual = sorted(set(re.findall(r"uses-permission: name='([^']+)'", permissions)))
    expected = sorted(
        "android.permission." + p
        for p in [
            "INTERNET",
            "POST_NOTIFICATIONS",
            "FOREGROUND_SERVICE",
            "FOREGROUND_SERVICE_DATA_SYNC",
            "WAKE_LOCK",
            "RECEIVE_BOOT_COMPLETED",
        ]
    )
    expected = sorted(expected + ["com.sevenu.app.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"])
    report["android_permissions"] = actual
    checks["only_expected_permissions"] = actual == expected
    checks["no_exact_alarm_or_battery_exemption"] = not any(
        "EXACT_ALARM" in p or "IGNORE_BATTERY_OPTIMIZATIONS" in p for p in actual
    )
    manifest = prior.android_output(aapt, "dump", "xmltree", apk, "AndroidManifest.xml")
    report["android_components"] = {
        "application_owned_engine": "SevenUApplication" in manifest,
        "foreground_answer_service": "AnswerForegroundService" in manifest,
        "daily_receiver": "DailyMemoryReceiver" in manifest,
    }
    checks["background_components_present"] = all(report["android_components"].values())
    return prior.finish_report(report)


def main(argv=None):
    return prior.run_cli(audit_release, argv)


if __name__ == "__main__":
    raise SystemExit(main())
