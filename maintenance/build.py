"""The sole public APK workflow: QQ, automatic voice, fresh daily pool, release."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from maintenance.core.process_lock import AlreadyRunning, process_lock
from maintenance.model_config import load_model_config
from maintenance.paths import ROOT
from maintenance.release import ReleaseError, _redact, build_release, validate_version_inputs
from maintenance.sync import run_sync
from maintenance.tencent_asr_config import load as load_tencent_asr


def _backup_assets(root: Path) -> Path:
    """Keep the previous prepared assets even if curation or packaging fails."""
    backups = root / "data/backups"
    backups.mkdir(parents=True, exist_ok=True)
    directory = Path(
        tempfile.mkdtemp(prefix="before-build-" + datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=backups)
    )
    for relative in (
        "app/assets/memory/memory_pack.db",
        "app/assets/memory/daily_memories.json",
        "app/pubspec.yaml",
    ):
        source = root / relative
        if source.is_file():
            shutil.copy2(source, directory / source.name)
    return directory


def run_build(root: Path = ROOT, *, version_name=None, software_version=None, emit=print) -> dict:
    """Hold one OS lock for the entire chain; no partial sync can be released.

    Completed import/transcription work is retained on a later failure so that
    a retry does not pay for the same audio twice. Old DB/assets are backed up;
    the release function only publishes a fully verified new signed APK.
    """
    validate_version_inputs(version_name=version_name, software_version=software_version)
    root = Path(root).resolve()
    data = root / "data"
    secrets = ()

    def output(message):
        return emit(_redact(message, secrets))

    try:
        with process_lock(data / "maintenance.lock"):
            config = load_model_config(data / "model_config.dpapi")
            if config is None:
                raise ReleaseError("请先在维护窗口安全保存模型 API Key。")
            secrets = (config.api_key,)
            # Tencent is optional for a text-only update; run_sync decides if
            # pending audio actually requires it. Never place credentials in argv.
            tencent = load_tencent_asr(data / "tencent_asr_config.dpapi")
            if tencent is not None:
                secrets += (tencent.secret_id, tencent.secret_key)
            output("[1/4] 备份资料并提取本机 QQ 新记录……")
            asset_backup = _backup_assets(root)
            voice_started = False

            def sync_progress(message):
                nonlocal voice_started
                if not voice_started and ("腾讯" in message or "采纳" in message):
                    voice_started = True
                    output("[2/4] 自动处理新增语音……")
                output(message)

            synced = run_sync(
                db_path=data / "couple_chat.db",
                tencent_config_path=data / "tencent_asr_config.dpapi",
                progress_path=data / "sync_progress.json",
                backup_directory=data / "backups",
                lock_path=data / "maintenance.lock",
                callback=sync_progress,
                _maintenance_lock_held=True,
            )
            if (
                synced.get("status") != "completed"
                or synced.get("voice_failed", 0)
                or synced.get("after", {}).get("voice_states", {}).get("pending", 0)
            ):
                code = synced.get("error_code", "sync_incomplete")
                raise ReleaseError(
                    f"聊天或语音更新未完成（{code}），已停止构建；已完成资料和备份保留，请重试。"
                )
            if not voice_started:
                output("[2/4] 语音检查完成，没有需要转写的新语音。")
            result = build_release(
                root,
                version_name=version_name,
                software_version=software_version,
                emit=output,
                _maintenance_lock_held=True,
            )
            result = {**result, "sync": synced, "asset_backup": str(asset_backup)}
            output("四个步骤全部完成；本次新安装包：" + result["apk"])
            return result
    except AlreadyRunning:
        raise ReleaseError("已有聊天维护或发布正在运行，请等待完成。") from None
    except Exception as exc:
        raise ReleaseError(_redact(exc, secrets)) from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument("--version-name", help="完整版本号，例如 0.1.260927；内部构建号自动递增")
    versions.add_argument("--software-version", help="软件版本，例如 0.1；日期按最新聊天自动填写")
    args = parser.parse_args(argv)
    try:
        result = run_build(
            version_name=args.version_name,
            software_version=args.software_version,
            emit=lambda message: print(message, flush=True),
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "apk": result["apk"],
                    "version_name": result["version_name"],
                    "version_code": result["version_code"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except ReleaseError as exc:
        print(f"构建未完成：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
