"""Windows-account-bound Tencent ASR credentials for the PC maintenance tool."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from maintenance.core.credentials import protect, unprotect

ENTROPY = b"7u:tencent-asr-config:v1"


@dataclass(frozen=True)
class TencentAsrConfig:
    secret_id: str
    secret_key: str
    region: str = "ap-shanghai"


def validate(source):
    if isinstance(source, TencentAsrConfig):
        source = asdict(source)
    if not isinstance(source, dict) or set(source) != {"secret_id", "secret_key", "region"}:
        raise ValueError("Invalid Tencent ASR configuration fields")
    secret_id, secret_key, region = (source["secret_id"], source["secret_key"], source["region"])
    for label, value in (("SecretId", secret_id), ("SecretKey", secret_key)):
        if (
            not isinstance(value, str)
            or not 8 <= len(value) <= 256
            or not value.isascii()
            or any(character.isspace() for character in value)
        ):
            raise ValueError(f"Invalid Tencent {label}")
    if (
        not isinstance(region, str)
        or not 3 <= len(region) <= 40
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in region)
    ):
        raise ValueError("Invalid Tencent region")
    return TencentAsrConfig(secret_id, secret_key, region)


def save(path, source, *, protector=None):
    config = validate(source)
    raw = json.dumps({"version": 1, **asdict(config)}, sort_keys=True, separators=(",", ":")).encode()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep this payload separate by using different DPAPI entropy.
    import os
    import tempfile

    encrypted = (
        protector
        or (
            lambda value: protect(
                value, entropy_value=ENTROPY, description_value="7u Tencent ASR configuration"
            )
        )
    )(raw)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return config


def load(path, *, unprotector=None):
    source = Path(path)
    if not source.exists():
        return None
    try:
        raw = (unprotector or (lambda value: unprotect(value, entropy_value=ENTROPY)))(source.read_bytes())
        value = json.loads(raw)
        if not isinstance(value, dict) or value.pop("version", None) != 1:
            raise ValueError("Unsupported Tencent ASR configuration")
        return validate(value)
    except (UnicodeError, json.JSONDecodeError, TypeError, OSError) as exc:
        raise ValueError("Tencent ASR configuration cannot be read") from exc
