import json
import tempfile
import unittest
from pathlib import Path

from maintenance.config import load_config


class LocalConfigTests(unittest.TestCase):
    def test_defaults_alone_fail_without_disclosing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config/default.json").write_text('{"database":"data/chat.db"}')
            with self.assertRaisesRegex(ValueError, "config/local.json"):
                load_config(root)

    def test_local_overrides_and_paths_are_relative_to_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config/default.json").write_text('{"database":"data/chat.db"}')
            local = dict(
                source_encrypted_database="source.db",
                database_key_file="key.txt",
                audio_directory="audio",
                self_qq=101,
                partner_qq=202,
            )
            (root / "config/local.json").write_text(json.dumps(local))
            loaded = load_config(root)
            self.assertEqual(loaded["database"], str(root / "data/chat.db"))
            self.assertEqual(loaded["self_qq"], 101)
            local["partner_qq"] = 101
            (root / "config/local.json").write_text(json.dumps(local))
            with self.assertRaisesRegex(ValueError, "不同账号"):
                load_config(root)
