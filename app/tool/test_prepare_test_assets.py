from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from prepare_test_assets import prepare_assets


class SyntheticAssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / 'pubspec.yaml').write_text('name: synthetic_fixture_test\n')
        (self.root / 'agent').mkdir()
        self.canonical = self.root / 'agent/prompt.md'
        self.canonical.write_text('Synthetic test canonical prompt.', encoding='utf-8')
        self.assets = self.app / 'assets/memory'

    def test_prepare_contains_only_marked_synthetic_data_and_expected_schema(self):
        result = prepare_assets(self.app)
        self.assertTrue(result['synthetic'])
        with closing(sqlite3.connect(self.assets / 'memory_pack.db')) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='is_synthetic'").fetchone()[0], 'true')
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 15)
            self.assertTrue(all('合成测试' in row[0] for row in db.execute('SELECT text FROM messages')))
        pool = json.loads((self.assets / 'daily_memories.json').read_text(encoding='utf-8'))
        self.assertTrue(pool['is_synthetic'])
        self.assertEqual(len(pool['items']), 15)
        self.assertTrue(all(len(item['text'].encode('utf-16-le')) // 2 <= 15 for item in pool['items']))
        self.assertEqual((self.app / 'assets/agent/prompt.md').read_bytes(), self.canonical.read_bytes())

    def test_existing_asset_blocks_all_writes_including_partial_private_assets(self):
        for name in ('memory_pack.db', 'daily_memories.json'):
            with self.subTest(name=name):
                self.assets.mkdir(parents=True, exist_ok=True)
                target = self.assets / name
                target.write_bytes(b'private sentinel')
                with self.assertRaises(FileExistsError):
                    prepare_assets(self.app)
                self.assertEqual(target.read_bytes(), b'private sentinel')
                self.assertEqual(list(self.assets.iterdir()), [target])
                target.unlink()

    def test_existing_prompt_is_not_overwritten(self):
        prompt = self.app / 'assets/agent/prompt.md'
        prompt.parent.mkdir(parents=True)
        prompt.write_bytes(b'local prompt sentinel')
        prepare_assets(self.app)
        self.assertEqual(prompt.read_bytes(), b'local prompt sentinel')

    def test_missing_canonical_prompt_stops_before_assets_are_created(self):
        self.canonical.unlink()
        with self.assertRaises(ValueError):
            prepare_assets(self.app)
        self.assertFalse(self.assets.exists())

    def test_failure_rolls_back_only_files_created_by_this_invocation(self):
        import prepare_test_assets
        original_link = prepare_test_assets.os.link
        def fail_second(source, destination):
            if destination.name == 'daily_memories.json':
                raise OSError('simulated publication failure')
            original_link(source, destination)
        with patch.object(prepare_test_assets.os, 'link', side_effect=fail_second):
            with self.assertRaises(OSError):
                prepare_assets(self.app)
        self.assertEqual(list(self.assets.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
