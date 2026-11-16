"""Silk framing validation and PCM WAV decoding, independent of ASR models."""

import tempfile
import wave
from pathlib import Path

from maintenance.voice.silk import inspect_packets


def decode(path, target):
    import pilk

    raw = Path(path).read_bytes()
    inspect_packets(raw)
    if raw.startswith(b"\x02"):
        raw = raw[1:]
    if not raw.startswith(b"#!SILK_V3"):
        raise ValueError("Unsupported audio header")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as folder:
        silk, pcm = Path(folder) / "audio.silk", Path(folder) / "audio.pcm"
        silk.write_bytes(raw)
        pilk.decode(str(silk), str(pcm), pcm_rate=24000)
        frames = pcm.read_bytes()
        with wave.open(str(target), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(frames)
    return len(frames) / 48000
