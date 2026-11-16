"""Windows-account-bound storage for the PC-side model configuration."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from maintenance.core.credentials import protect, unprotect


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"


def validate_model_config(source) -> ModelConfig:
    if isinstance(source, ModelConfig):
        source = asdict(source)
    if not isinstance(source, dict) or set(source) != {"api_key", "base_url", "model"}:
        raise ValueError("Invalid model configuration fields")
    key, base_url, model = source["api_key"], source["base_url"], source["model"]
    if (
        not isinstance(key, str)
        or not 16 <= len(key) <= 512
        or not key.isascii()
        or any(character.isspace() for character in key)
    ):
        raise ValueError("Invalid model API key")
    parsed = urlparse(base_url) if isinstance(base_url, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Model URL must be a direct HTTPS origin or path")
    if (
        not isinstance(model, str)
        or not 1 <= len(model.strip()) <= 100
        or any(ord(character) < 32 for character in model)
    ):
        raise ValueError("Invalid model name")
    return ModelConfig(key, base_url.rstrip("/"), model.strip())


def save_model_config(
    path: str | Path, source, *, protector: Callable[[bytes], bytes] | None = None
) -> ModelConfig:
    config = validate_model_config(source)
    payload = json.dumps(
        {"version": 1, **asdict(config)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    encrypted = (protector or protect)(payload)
    if not isinstance(encrypted, bytes) or not encrypted:
        raise ValueError("Model configuration encryption failed")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
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


def load_model_config(
    path: str | Path, *, unprotector: Callable[[bytes], bytes] | None = None
) -> ModelConfig | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        decoded = (unprotector or unprotect)(source.read_bytes())
        value = json.loads(decoded)
        if not isinstance(value, dict) or value.pop("version", None) != 1:
            raise ValueError("Unsupported secure model configuration")
        return validate_model_config(value)
    except (UnicodeError, json.JSONDecodeError, TypeError, OSError) as exc:
        raise ValueError("Secure model configuration cannot be read") from exc
