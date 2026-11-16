"""Conservative model-input exclusions, separate from local archive browsing.

Patterns are not a complete secret detector. Unlabelled credentials require an
explicit message-ID exclusion; never mutate the canonical text to redact it.
"""

import json
import re
from pathlib import Path

SECRET = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:密码|口令|验证码|api[_ -]?key|password|access[_ -]?token)\s*[:：=]\s*\S+",
    re.IGNORECASE,
)


def contains_secret(text):
    return bool(SECRET.search(text or ""))


def load_exclusions(path):
    path = Path(path)
    if not path.exists():
        return frozenset()
    record = json.loads(path.read_text(encoding="utf-8"))
    entries = record.get("message_ids") if isinstance(record, dict) else None
    if (
        not isinstance(entries, list)
        or len(entries) > 10000
        or any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdigit()
            or not 0 < int(value) <= 2**63 - 1
            for value in entries
        )
    ):
        raise ValueError("Invalid model exclusion registry")
    return frozenset(entries)
