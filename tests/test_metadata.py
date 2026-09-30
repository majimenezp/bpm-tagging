import unittest

from bpm_tagging.metadata import _tag_value, parse_bpm_value


class MetadataTests(unittest.TestCase):
    def test_existing_bpm_parsing(self) -> None:
        self.assertEqual(parse_bpm_value("128 BPM"), 128.0)
        self.assertEqual(parse_bpm_value(["127.5"]), 127.5)
        self.assertIsNone(parse_bpm_value("0"))
        self.assertIsNone(parse_bpm_value("-12"))
        self.assertIsNone(parse_bpm_value("NaN"))
        self.assertIsNone(parse_bpm_value(float("inf")))
        self.assertIsNone(parse_bpm_value([]))
        self.assertIsNone(parse_bpm_value("nonsense"))
        self.assertIsNone(parse_bpm_value("unknown"))
        self.assertEqual(_tag_value({"bpm": [140]}, ("BPM",)), 140.0)
