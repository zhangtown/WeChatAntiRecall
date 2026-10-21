"""核心逻辑单元测试（不依赖真实 Weixin.dll）。

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxar import core  # noqa: E402


class PatternTests(unittest.TestCase):
    def test_parse_pattern(self):
        self.assertEqual(core.parse_pattern("48 89 ?? 4C"), [0x48, 0x89, None, 0x4C])
        self.assertEqual(core.parse_pattern("48,89,**"), [0x48, 0x89, None])

    def test_parse_pattern_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            core.parse_pattern("zz")
        with self.assertRaises(ValueError):
            core.parse_pattern("1FF")

    def test_find_all_exact(self):
        buf = b"\x00\x48\x89\x86\x01\x02\x03\x04\x4C"
        pat = core.parse_pattern("48 89 86")
        self.assertEqual(core.find_all(buf, pat), [1])

    def test_find_all_wildcard(self):
        buf = bytes.fromhex("48008601020304") + bytes.fromhex("489186aabbccdd")
        pat = core.parse_pattern("48 ?? 86 ?? ?? ?? ??")
        self.assertEqual(core.find_all(buf, pat), [0, 7])

    def test_find_all_multiple(self):
        buf = b"AAAA" + b"\x48\x89\x86" + b"BB" + b"\x48\x89\x86"
        self.assertEqual(core.find_all(buf, core.parse_pattern("48 89 86")), [4, 9])

    def test_find_all_empty(self):
        self.assertEqual(core.find_all(b"", core.parse_pattern("48")), [])

    def test_diff_positions(self):
        s = core.parse_pattern("48 89 86 ?? 4C 8D")
        r = core.parse_pattern("48 29 87 ?? 4C 8D")
        self.assertEqual(core.diff_positions(s, r), [1, 2])

    def test_diff_positions_rejects_length_mismatch(self):
        with self.assertRaises(ValueError):
            core.diff_positions(core.parse_pattern("48"), core.parse_pattern("48 29"))


class VersionTests(unittest.TestCase):
    def test_version_key(self):
        self.assertEqual(core.version_key("4.1.15.53"), (4, 1, 15, 53))
        self.assertGreater(core.version_key("4.1.15.53"), core.version_key("4.1.9.0"))

    def test_range(self):
        self.assertTrue(core.version_in_range("4.1.15.53", "4.1.12.0", ""))
        self.assertFalse(core.version_in_range("4.1.9.0", "4.1.12.0", ""))
        self.assertTrue(core.version_in_range("4.1.9.0", "4.1.9.0", "4.1.12.0"))
        self.assertFalse(core.version_in_range("4.1.12.0", "4.1.9.0", "4.1.12.0"))

    def test_unknown_version_passes(self):
        self.assertTrue(core.version_in_range("", "4.1.12.0", ""))


class DatabaseTests(unittest.TestCase):
    def test_db_is_consistent(self):
        db = core.load_db()
        spec = db["apps"]["weixin"]
        for p in spec["patches"]:
            s = core.parse_pattern(p["search"])
            r = core.parse_pattern(p["replace"])
            self.assertEqual(len(s), len(r), p["id"])
            self.assertTrue(core.diff_positions(s, r), p["id"])

    def test_pick_patch_for_current_version(self):
        spec = core.app_spec()
        self.assertIsNotNone(core.pick_patch("4.1.15.53", spec))
        self.assertIsNone(core.pick_patch("3.9.0.0", spec))


if __name__ == "__main__":
    unittest.main()
