import tempfile
import unittest
from pathlib import Path

from maintenance.tencent_asr_config import TencentAsrConfig, load, save, validate


class TencentAsrConfigTests(unittest.TestCase):
    def test_secure_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "asr.dpapi"
            config = TencentAsrConfig("AKID" + "a" * 20, "0123456789abcdefghijklmnopqrstuv")
            save(path, config, protector=lambda raw: b"protected:" + raw[::-1])
            self.assertNotIn(config.secret_key.encode(), path.read_bytes())
            self.assertEqual(
                load(path, unprotector=lambda raw: raw.removeprefix(b"protected:")[::-1]), config
            )

    def test_rejects_bad_fields(self):
        with self.assertRaises(ValueError):
            validate({"secret_id": "short", "secret_key": "x" * 32, "region": "ap-shanghai"})
