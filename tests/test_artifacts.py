import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance.core.artifacts import publish_json


class ArtifactTests(unittest.TestCase):
    def test_replace_failure_preserves_old_snapshot_and_cleans_only_own_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "statistics.json"
            publish_json(path, {"old": "完整统计"})
            unrelated = Path(folder) / "unrelated.tmp"
            unrelated.write_text("keep")

            def fail(source, destination):
                self.assertEqual(Path(source).parent, path.parent)
                self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), {"new": 2})
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": "完整统计"})
                raise PermissionError("Simulated replacement failure")

            with patch("maintenance.core.artifacts.os.replace", side_effect=fail):
                with self.assertRaises(PermissionError):
                    publish_json(path, {"new": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": "完整统计"})
            self.assertEqual(set(Path(folder).iterdir()), {path, unrelated})
            publish_json(path, {"new": 3})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": 3})

    def test_serialization_failure_does_not_touch_existing_report(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            publish_json(path, {"valid": True})
            for bad in ({"bad": object()}, {"bad": float("nan")}):
                with self.assertRaises((TypeError, ValueError)):
                    publish_json(path, bad)
                self.assertEqual(json.loads(path.read_text()), {"valid": True})
            self.assertEqual(list(Path(folder).iterdir()), [path])
