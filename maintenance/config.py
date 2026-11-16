"""Load public defaults with private, machine-local import settings."""

import json
from pathlib import Path

from maintenance.paths import ROOT


def load_config(root: Path = ROOT) -> dict:
    directory = Path(root) / "config"
    result = json.loads((directory / "default.json").read_text(encoding="utf-8"))
    local = directory / "local.json"
    if local.is_file():
        result.update(json.loads(local.read_text(encoding="utf-8")))
    for key in ("database", "source_encrypted_database", "database_key_file", "audio_directory"):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"请在 config/local.json 配置 {key}。")
        path = Path(value)
        result[key] = str(path if path.is_absolute() else Path(root) / path)
    for key in ("self_qq", "partner_qq"):
        if type(result.get(key)) is not int or result[key] <= 0:
            raise ValueError(f"请在 config/local.json 配置正整数 {key}。")
    if result["self_qq"] == result["partner_qq"]:
        raise ValueError("self_qq 和 partner_qq 必须是不同账号。")
    return result
