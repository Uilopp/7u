import sqlite3
import tempfile
import unittest
import wave
from pathlib import Path

from maintenance.voice.playback import AudioUnavailable, prepare


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.source = self.root / "source.amr"
        self.source.write_bytes(b"fixture")
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE message_provenance(msg_id INTEGER,audio_path TEXT,raw_msg_type INTEGER)"
        )
        self.db.execute("INSERT INTO message_provenance VALUES(1,?,6)", (str(self.source),))

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def test_cache_is_content_addressed_and_source_is_unchanged(self):
        calls = []

        def decode(source, target):
            calls.append(source.read_bytes())
            with wave.open(str(target), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(24000)
                out.writeframes(b"\0" * 480)

        first = prepare(self.db, "1", self.root / "cache", decode)
        self.assertEqual(prepare(self.db, "1", self.root / "cache", decode), first)
        self.assertEqual(len(calls), 1)
        self.source.write_bytes(b"changed fixture")
        self.assertNotEqual(prepare(self.db, "1", self.root / "cache", decode), first)
        self.assertEqual(self.source.read_bytes(), b"changed fixture")

    def test_paths_and_missing_messages_are_rejected(self):
        for identifier in ("../../secret", "file.amr", 99):
            with self.assertRaises(AudioUnavailable):
                prepare(self.db, identifier, self.root / "cache")

    def test_decode_error_does_not_publish_cache_or_expose_path(self):
        def broken(source, target):
            raise RuntimeError(str(source))

        with self.assertRaises(AudioUnavailable) as caught:
            prepare(self.db, 1, self.root / "cache", broken)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertEqual(list((self.root / "cache").glob("*.wav")), [])
