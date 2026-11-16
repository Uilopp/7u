import tempfile
import unittest
from pathlib import Path

from maintenance.model_config import ModelConfig, load_model_config, save_model_config, validate_model_config


class ModelConfigTests(unittest.TestCase):
    def test_encrypted_round_trip_is_atomic_and_never_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.dpapi"
            config = ModelConfig("sk-" + "a" * 24, "https://api.deepseek.com", "deepseek-flash")
            saved = save_model_config(path, config, protector=lambda raw: b"protected:" + raw[::-1])
            self.assertEqual(saved, config)
            self.assertNotIn(config.api_key.encode(), path.read_bytes())
            loaded = load_model_config(path, unprotector=lambda raw: raw.removeprefix(b"protected:")[::-1])
            self.assertEqual(loaded, config)

    def test_invalid_fields_urls_and_keys_fail_closed(self):
        valid = {
            "api_key": "sk-" + "a" * 24,
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
        }
        cases = []
        for key, value in [
            ("extra", True),
            ("api_key", "short"),
            ("api_key", "x" * 20 + " "),
            ("base_url", "http://example.com"),
            ("base_url", "https://user:pass@example.com"),
            ("base_url", "https://example.com?q=secret"),
            ("model", "   "),
        ]:
            candidate = dict(valid)
            candidate[key] = value
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_model_config(candidate)

    def test_missing_is_unconfigured_and_corruption_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.dpapi"
            self.assertIsNone(load_model_config(path))
            path.write_bytes(b"damaged")
            with self.assertRaises(ValueError):
                load_model_config(path, unprotector=lambda raw: b"not-json")

    @unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI only")
    def test_real_windows_dpapi_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.dpapi"
            config = ModelConfig("sk-" + "z" * 24)
            save_model_config(path, config)
            self.assertEqual(load_model_config(path), config)


if __name__ == "__main__":
    unittest.main()
