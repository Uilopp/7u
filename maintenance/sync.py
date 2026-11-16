"""One-click local QQ import and automatic Tencent transcription.

No listening decision is invented: automatic text remains ``asr_accepted``;
human corrections and exclusions are preserved by the existing adoption policy.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance.config import load_config
from maintenance.core.artifacts import publish_json
from maintenance.core.process_lock import AlreadyRunning, process_lock
from maintenance.core.schema import initialize_archive
from maintenance.ingest.watcher import poll
from maintenance.paths import ROOT
from maintenance.voice.adopt_tencent import adopt
from maintenance.voice.tencent_asr import run as transcribe


def _counts(db_path):
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        total, latest = db.execute("SELECT count(*),max(ts) FROM messages").fetchone()
        states = dict(
            db.execute("""SELECT voice_state,count(*)
            FROM message_provenance WHERE raw_msg_type=6 GROUP BY voice_state""")
        )
    return {
        "messages": total,
        "latest_beijing": datetime.fromtimestamp(latest, timezone(timedelta(hours=8))).isoformat()
        if latest
        else None,
        "voice_states": states,
        "usable_voice": states.get("asr_accepted", 0) + states.get("verified", 0),
        "missing_audio": states.get("missing_audio", 0) + states.get("missing_reference", 0),
    }


def _backup(db_path, directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (
        "couple_chat-before-sync-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db"
    )
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup validation failed; import was not started")
    return destination


def _validate_initial_import_config(config):
    """Fail before creating a fresh archive when local import settings are absent."""
    for field in ("self_qq", "partner_qq"):
        if type(config.get(field)) is not int or config[field] <= 0:
            raise ValueError(f"首次导入前请配置正整数 {field}。")
    if config["self_qq"] == config["partner_qq"]:
        raise ValueError("首次导入的两个账号必须不同。")
    for field in ("source_encrypted_database", "database_key_file", "audio_directory"):
        value = config.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"首次导入前请配置 {field}。")
    for field in ("source_encrypted_database", "database_key_file"):
        if not (ROOT / config[field]).is_file():
            raise ValueError(f"首次导入所需的 {field} 文件不可用。")


def run_sync(
    *,
    config=None,
    db_path=None,
    tencent_config_path=None,
    progress_path=None,
    backup_directory=None,
    lock_path=None,
    batch_size=3205,
    workers=4,
    callback=None,
    _maintenance_lock_held=False,
):
    """Import, transcribe only pending audio, and adopt without a review gate.

    Missing originals are reported as gaps, not errors. Service failures retain
    pending records for the next run. Optional paths support local test fixtures.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 3205:
        raise ValueError("batch_size must be between 1 and 3205")
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    cfg = dict(load_config() if config is None else config)
    target = Path(db_path or ROOT / cfg["database"]).resolve()
    cfg["database"] = str(target)
    if not target.exists():
        _validate_initial_import_config(cfg)
    credentials = Path(tencent_config_path or ROOT / "data/tencent_asr_config.dpapi")
    progress = Path(progress_path or target.parent / "sync_progress.json")
    backups = Path(backup_directory or target.parent / "backups")
    lock = Path(lock_path or ROOT / "data/maintenance.lock")
    report = {
        "status": "running",
        "stage": "backup",
        "voice_completed": 0,
        "voice_reused": 0,
        "voice_failed": 0,
        "voice_empty_discarded": 0,
        "errors": [],
        "correctness_verified": False,
    }

    def stage(name, message):
        report["stage"] = name
        publish_json(progress, report)
        if callback:
            callback(message)

    # Only the in-process build coordinator may already own this outer lock.
    # The importer lock remains mandatory, including in that coordinated path.
    maintenance_lock = nullcontext() if _maintenance_lock_held else process_lock(lock)
    with maintenance_lock, process_lock(target.with_suffix(target.suffix + ".ingest.lock")):
        report["initialized_archive"] = initialize_archive(target)
        stage("backup", "正在备份本地资料…")
        report["backup_path"] = str(_backup(target, backups))
        report["before"] = _counts(target)
        stage("import", "正在增量导入 QQ 记录…")
        try:
            for attempt in range(3):
                try:
                    report["import"] = poll(cfg)
                    break
                except RuntimeError as exc:
                    if "QQ database changed during snapshot" not in str(exc) or attempt == 2:
                        raise
                    stage("import", "QQ 正在写入，正在重试稳定快照…")
                    time.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            report.update(status="failed", error_code="import_failed", error_type=type(exc).__name__)
            report["after"] = _counts(target)
            stage("finished", "导入未完成，已有记录保留；请稍后重试。")
            return report

        while True:
            pending_before = _counts(target)["voice_states"].get("pending", 0)
            if not pending_before:
                break
            stage("transcribe", "正在用腾讯补齐新语音，自动保留标点…")
            try:
                result = transcribe(
                    target,
                    credentials,
                    selection="pending",
                    limit=batch_size,
                    workers=workers,
                    progress_path=target.parent / "tencent_asr_progress.json",
                )
            except Exception as exc:
                report.update(
                    status="partial", error_code="transcription_unavailable", error_type=type(exc).__name__
                )
                break
            report["voice_completed"] += result["completed"]
            report["voice_reused"] += result["reused"]
            report["voice_failed"] += result["failed"]
            report["errors"].extend(result.get("errors", []))
            stage("adopt", "正在采纳腾讯文字，保留已有人工修正和排除…")
            try:
                adopt(target)
            except Exception as exc:
                report.update(status="partial", error_code="adoption_failed", error_type=type(exc).__name__)
                break
            pending_after = _counts(target)["voice_states"].get("pending", 0)
            if result["failed"] or pending_after >= pending_before:
                report.update(status="partial", error_code="voice_retry_needed")
                break

        report["after"] = _counts(target)
        report["voice_empty_discarded"] = max(
            0,
            report["after"]["voice_states"].get("discarded", 0)
            - report["before"]["voice_states"].get("discarded", 0),
        )
        if report["status"] == "running":
            report["status"] = "completed"
        stage(
            "finished",
            "同步完成。"
            if report["status"] == "completed"
            else "聊天文字已更新；部分语音未完成，保留待重试。",
        )
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        result = run_sync(workers=args.workers, callback=lambda message: print(message, flush=True))
    except AlreadyRunning:
        print("另一个维护操作正在运行，请等它完成后再试。", flush=True)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error_code": "sync_unavailable", "error_type": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
