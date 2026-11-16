"""Read-only check of files Git would include; never stages or prints secrets."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from maintenance.paths import ROOT

PRIVATE_EXTENSIONS = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".apk",
    ".aab",
    ".dpapi",
    ".jks",
    ".p12",
    ".keystore",
    ".key",
    ".pem",
    ".rar",
    ".7z",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
}
PRIVATE_PARTS = {
    "data",
    "releases",
    ".venv",
    "__pycache__",
    ".dart_tool",
    ".idea",
    ".gradle",
    "build",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    "local-only",
    ".local",
    "secrets",
    "credentials",
}
SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{24,}|AKID[A-Za-z0-9]{20,})\b")
ASSIGNED_SECRET = re.compile(
    r"(?i)\b(?:api[_-]?key|secret[_-]?id|secret[_-]?key)\b\s*[:=]\s*"
    r"[\"']?[A-Za-z0-9_+/=-]{16,}"
)
TEXT_EXTENSIONS = {
    ".py",
    ".dart",
    ".kt",
    ".kts",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cmd",
    ".cjs",
    ".properties",
    ".xml",
    ".proto",
    ".txt",
}


def candidate_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted(set(result.stdout.decode("utf-8").rstrip("\0").split("\0")) - {""})


def check(root: Path, files: list[str] | None = None) -> list[str]:
    root = Path(root).resolve()
    issues = []
    for name in files if files is not None else candidate_files(root):
        relative = Path(name)
        path = root / relative
        parts = set(relative.parts)
        if (
            parts & PRIVATE_PARTS
            or path.suffix.lower() in PRIVATE_EXTENSIONS
            or path.name in {"key.properties", "local.properties", ".env"}
            or (path.name.startswith(".env.") and path.name != ".env.example")
            or name.replace("\\", "/")
            in {"config/local.json", "app/assets/agent/prompt.md", "app/assets/memory/daily_memories.json"}
        ):
            issues.append(f"{name}: private/generated file would enter Git")
        for parent in (path, *path.parents):
            if parent == root:
                break
            if (parent / ".git").exists():
                issues.append(f"{name}: nested Git repository")
                break
        is_test = (
            "tests" in parts
            or "test" in parts
            or "integration_test" in parts
            or path.name.startswith("test_")
        )
        if path.is_file() and not is_test and path.suffix.lower() in TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8")
            if SECRET.search(text) or ASSIGNED_SECRET.search(text):
                issues.append(f"{name}: possible credential (value hidden)")
    return sorted(set(issues))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    files = candidate_files(ROOT)
    issues = check(ROOT, files)
    for issue in issues:
        print(issue)
    print(f"{len(files)} candidate files; {len(issues)} issues. No Git index changes.")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
