import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance.ingest.snapshot import _prefix_digest, copy_snapshot, fingerprint


class SnapshotTests(unittest.TestCase):
    def test_copy_strips_header_and_keeps_source_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "encrypted.db"
            raw = b"h" * 1024 + b"database payload"
            source.write_bytes(raw)
            Path(str(source) + "-wal").write_bytes(b"wal payload")
            output = root / "copy"
            output.mkdir()
            result = copy_snapshot(source, output)
            self.assertEqual(result.read_bytes(), b"database payload")
            self.assertEqual(Path(str(result) + "-wal").read_bytes(), b"wal payload")
            self.assertEqual(source.read_bytes(), raw)
            with self.assertRaises(FileExistsError):
                copy_snapshot(source, output)

    def test_changed_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "encrypted.db"
            source.write_bytes(b"x" * 1024)
            output = root / "copy"
            output.mkdir()
            initial = fingerprint([source, Path(str(source) + "-wal")])
            changed = [(initial[0][0], initial[0][1], initial[0][2] + 1), initial[1]]
            with patch("maintenance.ingest.snapshot.fingerprint", side_effect=[initial, changed]):
                with self.assertRaises(RuntimeError):
                    copy_snapshot(source, output)

    def test_wal_appends_are_not_a_mixed_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "encrypted.db"
            wal = Path(str(source) + "-wal")
            source.write_bytes(b"h" * 1024 + b"database payload")
            original = b"w" * 32 + b"committed prefix"
            wal.write_bytes(original)
            output = root / "copy"
            output.mkdir()
            original_copy = shutil.copyfileobj

            def copy_and_append(reader, writer, length):
                original_copy(reader, writer, length)
                with wal.open("ab") as stream:
                    stream.write(b"later transaction")

            with patch("maintenance.ingest.snapshot.shutil.copyfileobj", side_effect=copy_and_append):
                result = copy_snapshot(source, output)
            self.assertEqual(Path(str(result) + "-wal").read_bytes(), original)
            self.assertEqual(wal.read_bytes(), original + b"later transaction")

    def test_wal_reset_is_rejected_even_when_size_does_not_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "encrypted.db"
            wal = Path(str(source) + "-wal")
            source.write_bytes(b"h" * 1024 + b"database payload")
            wal.write_bytes(b"w" * 64)
            output = root / "copy"
            output.mkdir()
            original_copy = shutil.copyfileobj

            def copy_and_reset(reader, writer, length):
                original_copy(reader, writer, length)
                wal.write_bytes(b"r" * 64)

            with patch("maintenance.ingest.snapshot.shutil.copyfileobj", side_effect=copy_and_reset):
                with self.assertRaisesRegex(RuntimeError, "changed during snapshot"):
                    copy_snapshot(source, output)

    def test_wal_prefix_overwrite_after_copy_is_rejected_even_with_same_header(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "encrypted.db"
            wal = Path(str(source) + "-wal")
            source.write_bytes(b"h" * 1024 + b"database payload")
            wal.write_bytes(b"w" * 64)
            output = root / "copy"
            output.mkdir()

            def digest_then_modify(path, size, destination=None):
                digest = _prefix_digest(path, size, destination)
                if destination is not None:
                    with wal.open("r+b") as stream:
                        stream.seek(48)
                        stream.write(b"changed tail")
                return digest

            with patch("maintenance.ingest.snapshot._prefix_digest", side_effect=digest_then_modify):
                with self.assertRaisesRegex(RuntimeError, "changed during snapshot"):
                    copy_snapshot(source, output)

    def test_wal_truncation_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "encrypted.db"
            wal = Path(str(source) + "-wal")
            source.write_bytes(b"h" * 1024 + b"database payload")
            wal.write_bytes(b"w" * 64)
            output = root / "copy"
            output.mkdir()
            original_copy = shutil.copyfileobj

            def copy_and_truncate(reader, writer, length):
                original_copy(reader, writer, length)
                wal.write_bytes(b"w" * 32)

            with patch("maintenance.ingest.snapshot.shutil.copyfileobj", side_effect=copy_and_truncate):
                with self.assertRaisesRegex(RuntimeError, "changed during snapshot"):
                    copy_snapshot(source, output)

    def test_real_sqlite_wal_snapshot_ignores_commit_appended_during_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = root / "live.db"
            source = root / "encrypted.db"
            db = sqlite3.connect(live)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("CREATE TABLE entries(value INTEGER)")
                db.commit()
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                source.write_bytes(b"h" * 1024 + live.read_bytes())
                db.execute("INSERT INTO entries VALUES(1)")
                db.commit()
                os.link(Path(str(live) + "-wal"), Path(str(source) + "-wal"))
                output = root / "copy"
                output.mkdir()
                original_copy = shutil.copyfileobj

                def copy_and_commit(reader, writer, length):
                    original_copy(reader, writer, length)
                    db.execute("INSERT INTO entries VALUES(2)")
                    db.commit()

                with patch("maintenance.ingest.snapshot.shutil.copyfileobj", side_effect=copy_and_commit):
                    result = copy_snapshot(source, output)
                captured = sqlite3.connect(result)
                try:
                    self.assertEqual(captured.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(captured.execute("SELECT value FROM entries").fetchall(), [(1,)])
                finally:
                    captured.close()
                self.assertEqual(db.execute("SELECT value FROM entries").fetchall(), [(1,), (2,)])
            finally:
                db.close()
