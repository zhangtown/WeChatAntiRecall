"""重定位工具的核心函数测试（纯字节，不依赖真实 DLL）。

    python -m unittest discover -s tests -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxar import core  # noqa: E402

spec = importlib.util.spec_from_file_location("relocate", ROOT / "tools" / "relocate.py")
relocate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relocate)


# 4.1.12+ 的原始特征，通配处用具体值填实，便于构造夹具
FILLED = bytes.fromhex("488986480200004c89ad080200004c8d05")
PATCHED = bytes.fromhex("482986480200004c89ad080200004c8d05")
PADDING = bytes.fromhex("90" * 16)


def make(data: bytes) -> bytes:
    return PADDING + data + PADDING


def flip(buf: bytearray, index: int, value: int) -> bytes:
    buf[index] = value
    return bytes(buf)


class FuzzyFindTests(unittest.TestCase):
    def setUp(self):
        self.patch = core.load_db()["apps"]["weixin"]["patches"][0]
        self.pat = core.parse_pattern(self.patch["search"])

    def test_exact_hit(self):
        buf = make(FILLED)
        hits = relocate.fuzzy_find(buf, self.pat, 0)
        self.assertEqual(hits, [(16, 0, [])])

    def test_drift_inside_wildcard_is_not_a_mismatch(self):
        buf = bytearray(make(FILLED))
        buf[16 + 3] = 0x99                    # 第 3 字节本来通配
        hits = relocate.fuzzy_find(bytes(buf), self.pat, 0)
        self.assertEqual(hits, [(16, 0, [])])

    def test_drift_outside_anchor(self):
        buf = bytearray(make(FILLED))
        buf[16 + 12] = 0x10                   # 落在锚点里，单锚点会漏
        hits = relocate.fuzzy_find(bytes(buf), self.pat, 2)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], 16)
        self.assertEqual(hits[0][2], [12])

    def test_too_many_mismatches(self):
        buf = bytearray(make(FILLED))
        for k in (1, 2, 8):
            buf[16 + k] = 0xFF
        self.assertEqual(relocate.fuzzy_find(bytes(buf), self.pat, 2), [])


class SuggestEntryTests(unittest.TestCase):
    def setUp(self):
        self.patch = core.load_db()["apps"]["weixin"]["patches"][0]

    def test_clean_match_keeps_opcode_change(self):
        entry, drift, unsafe = relocate.suggest_entry(self.patch, make(FILLED), 16)
        self.assertEqual(drift, [])
        self.assertEqual(unsafe, [])
        s = core.parse_pattern(entry["search"])
        r = core.parse_pattern(entry["replace"])
        self.assertEqual(core.diff_positions(s, r), [1])

    def test_non_mod_position_opens_on_both_sides(self):
        buf = bytearray(make(FILLED))
        buf[16 + 12] = 0x10
        entry, drift, unsafe = relocate.suggest_entry(self.patch, bytes(buf), 16)
        self.assertEqual(unsafe, [])
        self.assertEqual([d[0] for d in drift], [12])
        s = core.parse_pattern(entry["search"])
        r = core.parse_pattern(entry["replace"])
        self.assertIsNone(s[12])
        self.assertIsNone(r[12])              # 两边都放开，diff_positions 才不会误写
        self.assertEqual(core.diff_positions(s, r), [1])

    def test_opcode_drift_is_refused(self):
        buf = bytearray(make(FILLED))
        buf[16 + 1] = 0xFF
        entry, drift, unsafe = relocate.suggest_entry(self.patch, bytes(buf), 16)
        self.assertEqual(len(unsafe), 1)
        self.assertEqual(unsafe[0][0], 1)
        # 拒绝时不应留下“看起来能用的”改写
        s = core.parse_pattern(entry["search"])
        r = core.parse_pattern(entry["replace"])
        self.assertEqual(core.diff_positions(s, r), [])

    def test_patched_bytes_produce_no_diff(self):
        buf = make(PATCHED)
        pat = core.parse_pattern(self.patch["replace"])
        hits = relocate.fuzzy_find(buf, pat, 0)
        self.assertEqual(hits[0][0], 16)


class LiteralRunTests(unittest.TestCase):
    def test_runs_sorted_by_length(self):
        pat = core.parse_pattern("48 89 ?? ?? 4C 89 AD 08 02 00 00 ?? 05")
        runs = relocate.literal_runs(pat)
        self.assertEqual(runs[0], (4, 11))
        lengths = [b - a for a, b in runs]
        self.assertEqual(lengths, sorted(lengths, reverse=True))


if __name__ == "__main__":
    unittest.main()
