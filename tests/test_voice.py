import struct
import unittest

from maintenance.voice.silk import inspect_packets


class VoiceTests(unittest.TestCase):
    def test_packet_length_uses_full_little_endian_byte(self):
        payload = b"\x02#!SILK_V3" + struct.pack("<h", 300) + b"x" * 300 + struct.pack("<h", -1)
        result = inspect_packets(payload)
        self.assertEqual(result["packet_count"], 1)
        self.assertEqual(result["max_packet_bytes"], 300)

    def test_truncated_audio_rejected(self):
        with self.assertRaises(ValueError):
            inspect_packets(b"#!SILK_V3" + struct.pack("<h", 20) + b"x")


if __name__ == "__main__":
    unittest.main()
