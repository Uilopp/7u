"""Tencent Cloud ASR candidates with Tencent-provided punctuation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from maintenance.core.artifacts import publish_json
from maintenance.paths import ROOT
from maintenance.tencent_asr_config import load
from maintenance.voice.codec import decode

HOST = "asr.tencentcloudapi.com"
SERVICE = "asr"
ACTION = "SentenceRecognition"
VERSION = "2019-06-14"
PROVIDER = "tencent_cloud_asr"
ENGINE = "16k_zh"
SUPPORTED_FORMATS = {
    ".amr": "amr",
    ".silk": "silk",
    ".slk": "silk",
    ".wav": "wav",
}


def detect_voice_format(audio, raw):
    """Prefer the container signature because QQ commonly mislabels SILK as .amr."""
    if raw.startswith(b"#!AMR"):
        return "amr"
    if b"#!SILK_V3" in raw[:16]:
        return "silk"
    voice_format = SUPPORTED_FORMATS.get(Path(audio).suffix.lower())
    if voice_format is None:
        raise ValueError(f"Unsupported Tencent short-audio format: {Path(audio).suffix or 'none'}")
    return voice_format


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS voice_secondary_candidates (
        msg_id INTEGER NOT NULL, provider TEXT NOT NULL, engine TEXT NOT NULL,
        candidate TEXT NOT NULL, audio_sha256 TEXT NOT NULL,
        request_id TEXT, created_at INTEGER NOT NULL,
        PRIMARY KEY(msg_id,provider,engine,audio_sha256))""")
    db.execute("""CREATE TABLE IF NOT EXISTS voice_candidates (
        msg_id INTEGER, model TEXT, parameters TEXT, candidate TEXT,
        metrics TEXT, created_at INTEGER, PRIMARY KEY(msg_id,model,parameters))""")


def audio_metrics(audio):
    """Measure new QQ audio without requiring a retired ASR candidate first."""
    raw = audio.read_bytes()
    voice_format = detect_voice_format(audio, raw)
    if voice_format == "silk":
        with tempfile.TemporaryDirectory(prefix="7u-voice-metrics-") as folder:
            duration = decode(audio, Path(folder) / "decoded.wav")
    elif voice_format == "wav":
        with wave.open(str(audio), "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
    else:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        duration = float(result.stdout.strip())
    if duration <= 0:
        raise ValueError("Original audio contains no decodable samples")
    return {
        "audio_sha256": hashlib.sha256(raw).hexdigest(),
        "duration": duration,
        "segments": [],
        "provider": PROVIDER,
    }


def _signature(secret_key, date, string_to_sign):
    secret_date = hmac.new(("TC3" + secret_key).encode(), date.encode(), hashlib.sha256).digest()
    secret_service = hmac.new(secret_date, SERVICE.encode(), hashlib.sha256).digest()
    secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
    return hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()


def signed_request(config, payload, timestamp=None):
    timestamp = int(time.time() if timestamp is None else timestamp)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    canonical_headers = (
        f"content-type:application/json; charset=utf-8\nhost:{HOST}\nx-tc-action:{ACTION.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = (
        "POST\n/\n\n" + canonical_headers + "\n" + signed_headers + "\n" + hashlib.sha256(body).hexdigest()
    )
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = (
        "TC3-HMAC-SHA256\n"
        + str(timestamp)
        + "\n"
        + scope
        + "\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )
    authorization = (
        "TC3-HMAC-SHA256 "
        + f"Credential={config.secret_id}/{scope}, "
        + f"SignedHeaders={signed_headers}, "
        + f"Signature={_signature(config.secret_key, date, string_to_sign)}"
    )
    return urllib.request.Request(
        f"https://{HOST}",
        data=body,
        method="POST",
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": HOST,
            "X-TC-Action": ACTION,
            "X-TC-Version": VERSION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": config.region,
        },
    )


def recognize(config, audio, identifier, *, opener=urllib.request.urlopen):
    audio = Path(audio)
    raw = audio.read_bytes()
    voice_format = detect_voice_format(audio, raw)
    if not raw or len(raw) > 3 * 1024 * 1024:
        raise ValueError("Tencent short-audio API requires a non-empty file no larger than 3 MB")
    payload = {
        "ProjectId": 0,
        "SubServiceType": 2,
        "EngSerViceType": ENGINE,
        "SourceType": 1,
        "VoiceFormat": voice_format,
        "UsrAudioKey": str(identifier),
        "Data": base64.b64encode(raw).decode(),
        "DataLen": len(raw),
        # Tencent emits punctuation when FilterPunc is 0. No punctuation
        # postprocessor is applied to the returned Result.
        "FilterDirty": 0,
        "FilterModal": 0,
        "FilterPunc": 0,
        "ConvertNumMode": 1,
    }
    with opener(signed_request(config, payload), timeout=30) as response:
        result = json.load(response).get("Response", {})
    if "Error" in result:
        error = result["Error"]
        raise RuntimeError(f"Tencent ASR {error.get('Code', 'unknown')}")
    text = result.get("Result")
    if not isinstance(text, str):
        raise ValueError("Tencent ASR returned an invalid transcript")
    return text.strip(), result.get("RequestId")


def recognize_audio(
    config, audio, identifier, duration, *, opener=urllib.request.urlopen, runner=subprocess.run
):
    """Use Tencent for every segment; local ffmpeg only handles the 60s boundary."""
    if not isinstance(duration, (int, float)) or duration <= 60:
        return recognize(config, audio, identifier, opener=opener)
    with tempfile.TemporaryDirectory(prefix="7u-tencent-asr-") as folder:
        decoded = Path(folder) / "decoded.wav"
        decode(audio, decoded)
        pattern = Path(folder) / "segment-%03d.wav"
        result = runner(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(decoded),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "segment",
                "-segment_time",
                "50",
                str(pattern),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError("Unable to split long audio for Tencent ASR")
        transcripts, request_ids = [], []
        for index, segment in enumerate(sorted(Path(folder).glob("segment-*.wav"))):
            text, request_id = recognize(config, segment, f"{identifier}-{index}", opener=opener)
            transcripts.append(text)
            if request_id:
                request_ids.append(request_id)
        if not transcripts:
            raise ValueError("Long audio produced no Tencent ASR segments")
        return "".join(transcripts), ",".join(request_ids) or None


def _select_rows(db, selection, limit, seed):
    if selection == "pending":
        states = ("pending",)
    elif selection == "random":
        states = ("asr_accepted",)
    elif selection == "all":
        states = ("asr_accepted", "pending")
    elif selection == "verified":
        states = ("verified",)
    else:
        raise ValueError(f"Unknown Tencent ASR selection: {selection}")
    placeholders = ",".join("?" for _ in states)
    rows = db.execute(
        f"""SELECT m.msg_id,p.audio_path,c.metrics
        FROM messages m JOIN message_provenance p USING(msg_id)
        LEFT JOIN voice_candidates c USING(msg_id)
        WHERE p.raw_msg_type=6 AND p.voice_state IN ({placeholders})
        ORDER BY CASE WHEN p.voice_state='pending' THEN 0 ELSE 1 END,m.ts,m.msg_id""",
        states,
    ).fetchall()
    if selection == "random":
        rows.sort(key=lambda row: hashlib.sha256(f"{seed}:{row[0]}".encode()).digest())
    return rows[:limit]


def run(
    db_path,
    config_path,
    *,
    limit=89,
    selection="pending",
    seed="7u-quality-v1",
    workers=1,
    opener=urllib.request.urlopen,
    progress_path=None,
):
    if not isinstance(limit, int) or not 1 <= limit <= 3205:
        raise ValueError("Tencent ASR limit must be between 1 and 3205")
    if not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("Tencent ASR workers must be between 1 and 8")
    config = load(config_path)
    if config is None:
        raise ValueError("Tencent ASR is not configured")
    report = {
        "status": "running",
        "selected": 0,
        "completed": 0,
        "reused": 0,
        "failed": 0,
        "errors": [],
        "updated_at": int(time.time()),
    }
    with closing(sqlite3.connect(db_path)) as db:
        initialize(db)
        rows = _select_rows(db, selection, limit, seed)
        report["selected"] = len(rows)
        jobs = []
        for msg_id, audio_path, metrics_json in rows:
            try:
                audio = Path(audio_path or "")
                if not audio.is_file():
                    raise ValueError("Original audio is missing")
                if metrics_json is None:
                    metrics = audio_metrics(audio)
                    with db:
                        db.execute(
                            "INSERT INTO voice_candidates VALUES(?,?,?,?,?,?)",
                            (
                                msg_id,
                                f"{PROVIDER}/{ENGINE}",
                                '{"preparation":true}',
                                "",
                                json.dumps(metrics, sort_keys=True),
                                int(time.time()),
                            ),
                        )
                else:
                    metrics = json.loads(metrics_json)
                digest = metrics.get("audio_sha256")
                if (
                    not audio.is_file()
                    or not isinstance(digest, str)
                    or hashlib.sha256(audio.read_bytes()).hexdigest() != digest
                ):
                    raise ValueError("Original audio is missing or changed")
                if db.execute(
                    """SELECT 1 FROM voice_secondary_candidates
                        WHERE msg_id=? AND provider=? AND engine=? AND audio_sha256=?""",
                    (msg_id, PROVIDER, ENGINE, digest),
                ).fetchone():
                    report["reused"] += 1
                    continue
                duration = metrics.get("duration")
                jobs.append((msg_id, audio, digest, duration))
            except Exception as exc:
                report["failed"] += 1
                report["errors"].append(
                    {"msg_id": str(msg_id), "error": type(exc).__name__, "detail": str(exc)[:160]}
                )
            report["updated_at"] = int(time.time())
            if progress_path:
                publish_json(progress_path, report)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(recognize_audio, config, audio, msg_id, duration, opener=opener): (msg_id, digest)
                for msg_id, audio, digest, duration in jobs
            }
            for future in as_completed(futures):
                msg_id, digest = futures[future]
                try:
                    text, request_id = future.result()
                    with db:
                        db.execute(
                            """INSERT INTO voice_secondary_candidates
                            VALUES(?,?,?,?,?,?,?)""",
                            (msg_id, PROVIDER, ENGINE, text, digest, request_id, int(time.time())),
                        )
                    report["completed"] += 1
                except Exception as exc:
                    report["failed"] += 1
                    report["errors"].append(
                        {"msg_id": str(msg_id), "error": type(exc).__name__, "detail": str(exc)[:160]}
                    )
                report["updated_at"] = int(time.time())
                if progress_path:
                    publish_json(progress_path, report)
    report["status"] = "completed" if not report["failed"] else "completed_with_failures"
    report["updated_at"] = int(time.time())
    if progress_path:
        publish_json(progress_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/couple_chat.db")
    parser.add_argument("--config", type=Path, default=ROOT / "data/tencent_asr_config.dpapi")
    parser.add_argument("--progress", type=Path, default=ROOT / "data/tencent_asr_progress.json")
    parser.add_argument("--limit", type=int, default=89)
    parser.add_argument("--selection", choices=("pending", "random", "all", "verified"), default="pending")
    parser.add_argument("--seed", default="7u-quality-v1")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--adopt", action="store_true", help="Promote completed Tencent output, preserving human decisions."
    )
    args = parser.parse_args(argv)
    report = run(
        args.db,
        args.config,
        limit=args.limit,
        selection=args.selection,
        seed=args.seed,
        workers=args.workers,
        progress_path=args.progress,
    )
    if args.adopt:
        from maintenance.voice.adopt_tencent import adopt

        report["adoption"] = adopt(args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
