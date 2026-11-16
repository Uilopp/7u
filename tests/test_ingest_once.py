import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from maintenance.ingest.watcher import poll


class OneShotImportTests(unittest.TestCase):
    def test_one_shot_import_passes_config_and_closes_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"database": str(Path(folder) / "target.db")}
            source = object()
            closed = []
            targets = []

            @contextmanager
            def snapshot(actual_config):
                self.assertIs(actual_config, config)
                try:
                    yield source
                finally:
                    closed.append(True)

            def import_rows(actual_source, target, actual_config):
                self.assertIs(actual_source, source)
                self.assertIs(actual_config, config)
                targets.append(target)
                return {"added": 2}

            with (
                patch("maintenance.ingest.watcher.encrypted_snapshot", snapshot),
                patch("maintenance.ingest.watcher.import_connection", import_rows),
            ):
                self.assertEqual(poll(config), {"added": 2})
            self.assertEqual(closed, [True])
            with self.assertRaises(sqlite3.ProgrammingError):
                targets[0].execute("SELECT 1")

    def test_import_failure_propagates_without_leaking_open_target(self):
        with tempfile.TemporaryDirectory() as folder:
            targets = []

            def fail_import(source, target, config):
                targets.append(target)
                raise ValueError("invalid source record")

            with (
                patch("maintenance.ingest.watcher.encrypted_snapshot") as snapshot,
                patch("maintenance.ingest.watcher.import_connection", fail_import),
            ):
                snapshot.return_value.__enter__.return_value = object()
                with self.assertRaisesRegex(ValueError, "invalid source record"):
                    poll({"database": str(Path(folder) / "target.db")})
                snapshot.return_value.__exit__.assert_called_once()
            with self.assertRaises(sqlite3.ProgrammingError):
                targets[0].execute("SELECT 1")
