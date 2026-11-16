import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from maintenance.tencent_asr_config import TencentAsrConfig
from maintenance.voice.tencent_asr import recognize, recognize_audio, run


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TencentAsrTests(unittest.TestCase):
    def setUp(self):
        self.config = TencentAsrConfig("AKID" + "a" * 20, "b" * 32)

    def test_request_is_signed_and_audio_is_base64(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "a.silk"
            audio.write_bytes(b"voice")
            captured = {}

            def open_request(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return _Response(json.dumps({"Response": {"Result": "你好。", "RequestId": "r"}}).encode())

            self.assertEqual(recognize(self.config, audio, 1, opener=open_request), ("你好。", "r"))
            payload = json.loads(captured["request"].data)
            self.assertEqual((payload["VoiceFormat"], payload["DataLen"]), ("silk", 5))
            self.assertIn("TC3-HMAC-SHA256", captured["request"].get_header("Authorization"))

    def test_amr_is_declared_as_amr_and_punctuation_is_not_filtered(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "a.amr"
            audio.write_bytes(b"voice")
            captured = {}

            def open_request(request, timeout):
                captured["payload"] = json.loads(request.data)
                return _Response(json.dumps({"Response": {"Result": "你好。"}}).encode())

            recognize(self.config, audio, 1, opener=open_request)
            self.assertEqual(captured["payload"]["VoiceFormat"], "amr")
            self.assertEqual(captured["payload"]["FilterPunc"], 0)

    def test_qq_silk_header_wins_over_amr_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "mislabelled.amr"
            audio.write_bytes(b"\x02#!SILK_V3voice")
            captured = {}

            def open_request(request, timeout):
                captured["payload"] = json.loads(request.data)
                return _Response(json.dumps({"Response": {"Result": "你好。"}}).encode())

            recognize(self.config, audio, 1, opener=open_request)
            self.assertEqual(captured["payload"]["VoiceFormat"], "silk")

    def test_empty_success_is_cached_for_discard_not_retried_as_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "a.amr"
            audio.write_bytes(b"voice")

            def opener(request, timeout):
                return _Response(json.dumps({"Response": {"Result": "  ", "RequestId": "empty"}}).encode())

            self.assertEqual(recognize(self.config, audio, 1, opener=opener), ("", "empty"))

    def test_new_voice_needs_no_legacy_candidate_and_pending_gets_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "chat.db"
            audio = root / "a.silk"
            audio.write_bytes(b"voice")
            digest = hashlib.sha256(b"voice").hexdigest()
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE messages(msg_id INTEGER,ts INTEGER)")
                db.execute(
                    "CREATE TABLE message_provenance(msg_id INTEGER,audio_path TEXT,raw_msg_type INTEGER,voice_state TEXT)"
                )
                db.executemany("INSERT INTO messages VALUES(?,?)", [(1, 1), (2, 2)])
                db.executemany(
                    "INSERT INTO message_provenance VALUES(?,?,6,?)",
                    [(1, str(audio), "asr_accepted"), (2, str(audio), "pending")],
                )

            def opener(request, timeout):
                return _Response(
                    json.dumps({"Response": {"Result": "新语音。", "RequestId": "new"}}).encode()
                )

            with (
                patch("maintenance.voice.tencent_asr.load", return_value=self.config),
                patch(
                    "maintenance.voice.tencent_asr.audio_metrics",
                    return_value={"duration": 2.5, "audio_sha256": digest, "segments": []},
                ),
            ):
                result = run(db_path, root / "config", selection="all", limit=1, opener=opener)
            self.assertEqual(result["completed"], 1)
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(
                    db.execute("SELECT msg_id FROM voice_secondary_candidates").fetchall(), [(2,)]
                )
                metrics = json.loads(db.execute("SELECT metrics FROM voice_candidates").fetchone()[0])
                self.assertEqual(metrics["duration"], 2.5)

    def test_unknown_audio_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "a.bin"
            audio.write_bytes(b"voice")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                recognize(self.config, audio, 1)

    def test_long_audio_is_split_but_every_text_still_comes_from_tencent(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "long.amr"
            audio.write_bytes(b"voice")

            class Result:
                returncode = 0

            def runner(command, **kwargs):
                pattern = Path(command[-1])
                for index in range(2):
                    Path(str(pattern).replace("%03d", f"{index:03d}")).write_bytes(b"RIFFvoice")
                return Result()

            with (
                patch(
                    "maintenance.voice.tencent_asr.decode",
                    side_effect=lambda source, target: Path(target).write_bytes(b"RIFFdecoded"),
                ),
                patch(
                    "maintenance.voice.tencent_asr.recognize",
                    side_effect=[("第一段。", "a"), ("第二段！", "b")],
                ) as mocked,
            ):
                text, request_id = recognize_audio(self.config, audio, 1, 61, runner=runner)
            self.assertEqual((text, request_id), ("第一段。第二段！", "a,b"))
            self.assertEqual(mocked.call_count, 2)

    def test_only_pending_voice_is_cached_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "chat.db"
            audio = root / "a.silk"
            audio.write_bytes(b"voice")
            digest = hashlib.sha256(b"voice").hexdigest()
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE messages(msg_id INTEGER,ts INTEGER)")
                db.execute(
                    "CREATE TABLE message_provenance(msg_id INTEGER,audio_path TEXT,raw_msg_type INTEGER,voice_state TEXT)"
                )
                db.execute("CREATE TABLE voice_candidates(msg_id INTEGER,metrics TEXT)")
                db.execute("INSERT INTO messages VALUES(1,1)")
                db.execute("INSERT INTO message_provenance VALUES(1,?,6,'pending')", (str(audio),))
                db.execute(
                    "INSERT INTO voice_candidates VALUES(1,?)", (json.dumps({"audio_sha256": digest}),)
                )
            config_path = root / "config"
            config_path.write_bytes(b"x")
            with patch("maintenance.voice.tencent_asr.load", return_value=self.config):

                def opener(request, timeout):
                    return _Response(
                        json.dumps({"Response": {"Result": "第二候选", "RequestId": "r"}}).encode()
                    )

                first = run(db_path, config_path, opener=opener)
                second = run(db_path, config_path, opener=opener)
            self.assertEqual((first["completed"], second["reused"]), (1, 1))

    def test_random_selection_is_deterministic_and_excludes_human_states(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "chat.db"
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE messages(msg_id INTEGER,ts INTEGER)")
                db.execute(
                    "CREATE TABLE message_provenance(msg_id INTEGER,audio_path TEXT,raw_msg_type INTEGER,voice_state TEXT)"
                )
                db.execute("CREATE TABLE voice_candidates(msg_id INTEGER,metrics TEXT)")
                for msg_id, state in (
                    (1, "asr_accepted"),
                    (2, "asr_accepted"),
                    (3, "verified"),
                    (4, "pending"),
                ):
                    audio = root / f"{msg_id}.amr"
                    audio.write_bytes(str(msg_id).encode())
                    digest = hashlib.sha256(str(msg_id).encode()).hexdigest()
                    db.execute("INSERT INTO messages VALUES(?,?)", (msg_id, msg_id))
                    db.execute("INSERT INTO message_provenance VALUES(?,?,6,?)", (msg_id, str(audio), state))
                    db.execute(
                        "INSERT INTO voice_candidates VALUES(?,?)",
                        (msg_id, json.dumps({"audio_sha256": digest})),
                    )
            seen = []

            def opener(request, timeout):
                seen.append(json.loads(request.data)["UsrAudioKey"])
                return _Response(json.dumps({"Response": {"Result": "有标点。"}}).encode())

            with patch("maintenance.voice.tencent_asr.load", return_value=self.config):
                result = run(
                    db_path, root / "config", selection="random", limit=2, seed="same", opener=opener
                )
            self.assertEqual(result["completed"], 2)
            self.assertEqual(set(seen), {"1", "2"})


if __name__ == "__main__":
    unittest.main()
