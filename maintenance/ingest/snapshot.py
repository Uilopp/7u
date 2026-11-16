"""Short-lived SQLCipher snapshot shared by importers and read-only audits."""

import hashlib
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from maintenance.paths import ROOT


def fingerprint(paths):
    return [
        (str(p), p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else (str(p), None, None)
        for p in paths
    ]


def copy_snapshot(source, folder):
    """Freeze a finite WAL prefix while permitting later append-only commits.

    The main file must remain unchanged throughout the copy. A WAL reset,
    truncation, or overwrite is rejected by header and full-prefix comparison.
    We never connect to, checkpoint, or modify QQ's own database/shm files.
    """
    paths = [source, Path(str(source) + "-wal")]
    before = fingerprint(paths)
    wal_size = before[1][1]
    wal_header = None
    if wal_size is not None:
        with paths[1].open("rb") as wal:
            wal_header = wal.read(min(32, wal_size))
    main = folder / "clear.db"
    with source.open("rb") as reader, main.open("xb") as writer:
        reader.seek(1024)
        shutil.copyfileobj(reader, writer, 16 * 1024 * 1024)
    if wal_size is not None:
        try:
            copied = _prefix_digest(paths[1], wal_size, Path(str(main) + "-wal"))
            checked = _prefix_digest(paths[1], wal_size)
            with paths[1].open("rb") as wal:
                final_header = wal.read(min(32, wal_size))
            if copied != checked or wal_header != final_header:
                raise RuntimeError("QQ database changed during snapshot; retry later")
        except OSError as exc:
            raise RuntimeError("QQ database changed during snapshot; retry later") from exc
    after = fingerprint(paths)
    if after[0] != before[0]:
        raise RuntimeError("QQ database changed during snapshot; retry later")
    if wal_size is not None and (after[1][1] is None or after[1][1] < wal_size):
        raise RuntimeError("QQ database changed during snapshot; retry later")
    return main


def _prefix_digest(path, size, destination=None):
    """Read exactly the frozen length; never chase an actively growing WAL."""
    from contextlib import nullcontext

    digest = hashlib.sha256()
    with path.open("rb") as reader, destination.open("xb") if destination else nullcontext() as writer:
        remaining = size
        while remaining:
            block = reader.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError("QQ database changed during snapshot; retry later")
            remaining -= len(block)
            digest.update(block)
            if writer is not None:
                writer.write(block)
    return digest.digest()


@contextmanager
def encrypted_snapshot(config):
    import sqlcipher3.dbapi2 as cipher

    temp = ROOT / "data/tmp"
    temp.mkdir(parents=True, exist_ok=True)
    key = Path(config["database_key_file"]).read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(dir=temp) as folder:
        path = copy_snapshot(Path(config["source_encrypted_database"]), Path(folder))
        source = cipher.connect(str(path))
        try:
            source.execute("PRAGMA cipher_page_size=4096")
            source.execute("PRAGMA key='" + key.replace("'", "''") + "'")
            source.execute("PRAGMA kdf_iter=4000")
            source.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA1")
            source.execute("PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512")
            source.execute("PRAGMA query_only=ON")
            source.execute("SELECT count(*) FROM sqlite_master").fetchone()
            yield source
        finally:
            source.close()
