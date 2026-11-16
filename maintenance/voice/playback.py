"""Bounded, content-addressed playback preparation from canonical message IDs."""

import hashlib
import tempfile
import threading
import wave
from pathlib import Path

from maintenance.voice.codec import decode

_decoders = threading.BoundedSemaphore(2)


class AudioUnavailable(ValueError):
    pass


def prepare(db, message_id, cache_dir, decoder=decode):
    try:
        identifier = int(message_id)
    except TypeError, ValueError:
        raise AudioUnavailable("无效语音消息 ID") from None
    row = db.execute(
        "SELECT audio_path,raw_msg_type FROM message_provenance WHERE msg_id=?", (identifier,)
    ).fetchone()
    if row is None or row[1] != 6:
        raise AudioUnavailable("这条消息不是可播放语音")
    if not row[0] or not Path(row[0]).is_file():
        raise AudioUnavailable("原始语音文件缺失，暂时无法播放")
    source = Path(row[0])
    if source.stat().st_size > 8 * 1024 * 1024:
        raise AudioUnavailable("音频超过当前播放大小限制")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    folder = Path(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{digest}_pcm24k_v1.wav"
    with _decoders:
        if not destination.exists():
            try:
                with tempfile.TemporaryDirectory(dir=folder) as temporary:
                    # Decode the exact bytes hashed above, not a mutable source path.
                    copied = Path(temporary) / "source.amr"
                    copied.write_bytes(raw)
                    output = Path(temporary) / "audio.wav"
                    decoder(copied, output)
                    with wave.open(str(output), "rb") as wav:
                        duration = wav.getnframes() / wav.getframerate()
                        if (
                            not 0 < duration <= 600
                            or wav.getnchannels() != 1
                            or wav.getsampwidth() != 2
                            or wav.getframerate() != 24000
                        ):
                            raise ValueError("Invalid decoded audio")
                    output.replace(destination)
            except Exception:
                raise AudioUnavailable("语音解码失败或超出播放限制，原文件未修改") from None
    return destination
