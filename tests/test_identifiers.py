import unittest

from maintenance.core.identifiers import message_id


class MessageIdentifierTests(unittest.TestCase):
    def test_exact_decimal_strings_are_preserved(self):
        for value in ("1", str(2**63 - 1)):
            self.assertEqual(message_id(value), value)

    def test_invalid_or_lossy_identifiers_are_rejected(self):
        for value in (True, 1, 1.0, None, "", "0", "-1", "１", "1.0", " 1", str(2**63), "1" * 20):
            with self.subTest(value=value), self.assertRaises(ValueError):
                message_id(value)
