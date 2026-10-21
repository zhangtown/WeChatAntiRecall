#!/usr/bin/env python3
"""特征码重定位工具：微信升级后用它找新的补丁点。

思路（按可靠性从高到低，逐级降级）：
  1. 精确匹配现有特征码（search / replace 都试）
  2. 模糊匹配：允许少量字节漂移，报告漂移位置
  3. 字符串锚定：找 "replacemsg" / "newmsgid" 的引用点，
     在引用点附近找 mov [reg+disp32], r64 形态的写指令

用法：
    python tools/relocate.py --dll "C:\\Program Files\\Tencent\\Weixin\\4.1.15.53\\Weixin.dll"
    python tools/relocate.py --dll copy.dll --max-mismatch 3 --dump

输出里的 “建议条目” 可以直接贴进 wxar/patterns.json。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxar import core  # noqa: E402


# --------------------------------------------------------------------------- #
# 最小 PE 解析（只用标准库，避免维护工具依赖 pefile）
# --------------------------------------------------------------------------- #
class PE:
    def __init__(self, data: bytes) -> None:
        self.data = data
        if data[:2] != b"MZ":
            raise ValueError("不是 PE 文件")
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            raise ValueError("PE 签名缺失")
        coff = e_lfanew + 4
        num_sec = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        opt = coff + 20
        self.magic = struct.unpack_from("<H", data, opt)[0]
        self.base = struct.unpack_from("<Q", data, opt + 24)[0]
        self.size = struct.unpack_from("<I", data, opt + 56)[0]
        tbl = opt + opt_size
        self.sections = []
        for i in range(num_sec):
            o = tbl + i * 40
            name = data[o:o + 8].rstrip(b"\0").decode("latin-1")
            vsize, vaddr, rsize, rptr = struct.unpack_from("<IIII", data, o + 8)
            self.sections.append(dict(name=name, va=vaddr, vsize=vsize,
                                      raw=rptr, rawsize=rsize))

    def section(self, name: str) -> dict:
        for s in self.sections:
            if s["name"] == name:
                return s
        raise KeyError(name)

    def off_of_va(self, va: int) -> int | None:
        rva = va - self.base
        for s in self.sections:
            if s["va"] <= rva < s["va"] + max(s["vsize"], s["rawsize"]):
                return s["raw"] + (rva - s["va"])
        return None

    def va_of_off(self, off: int) -> int | None:
        for s in self.sections:
            if s["raw"] <= off < s["raw"] + s["rawsize"]:
                return self.base + s["va"] + (off - s["raw"])
        return None


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #
def literal_runs(pat: list[int | None]) -> list[tuple[int, int]]:
    """所有连续固定字节段 [start, end)，按长度降序。"""
    runs, start = [], None
    for i, v in enumerate(list(pat) + [None]):
        if v is None:
            if start is not None:
                runs.append((start, i))
                start = None
        elif start is None:
            start = i
    return sorted(runs, key=lambda r: r[1] - r[0], reverse=True)


def fuzzy_find(data: bytes, pat: list[int | None], max_mismatch: int):
    """模糊匹配，返回 [(偏移, 不匹配数, 不匹配位置)]。

    逐个尝试每一段固定字节做锚点：单锚点遇到漂移会整体漏检，
    多锚点取并集才稳。
    """
    n = len(pat)
    cands: set[int] = set()
    for a0, a1 in literal_runs(pat):
        if a1 - a0 < 2:
            continue
        anchor = bytes(pat[a0:a1])
        i = 0
        while True:
            i = data.find(anchor, i)
            if i < 0:
                break
            base = i - a0
            if base >= 0 and base + n <= len(data):
                cands.add(base)
            i += 1

    out = []
    for base in cands:
        bad = [k for k, p in enumerate(pat)
               if p is not None and data[base + k] != p]
        if len(bad) <= max_mismatch:
            out.append((base, len(bad), bad))
    out.sort(key=lambda t: t[1])
    return out


def find_lea_refs(pe: PE, target_va: int) -> list[int]:
    """找 7 字节 RIP 相对寻址指令（REX + 8D + modrm(00,101) + disp32）。"""
    data = pe.data
    text = pe.section(".text")
    start, end = text["raw"], text["raw"] + text["rawsize"]
    hits = []
    i = start
    while True:
        i = data.find(b"\x8d", i, end)
        if i < 0:
            break
        if i - 1 >= start and data[i - 1] in (0x48, 0x4C, 0x49, 0x4D):
            modrm = data[i + 1]
            if (modrm & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", data, i + 2)[0]
                end_va = pe.va_of_off(i + 6)
                if end_va is not None and end_va + disp == target_va:
                    hits.append(i - 1)
        i += 1
    return hits


def find_stores_near(pe: PE, site_off: int, window: int = 0xC0) -> list[int]:
    """在 site 前后找 mov [reg+disp32], r64（48 89 / 4C 89，mod=10）。"""
    data = pe.data
    lo = max(0, site_off - window)
    hi = min(len(data), site_off + window)
    found = []
    i = lo
    while True:
        i = data.find(b"\x89", i, hi)
        if i < 0:
            break
        if i - 1 >= 0 and data[i - 1] in (0x48, 0x4C, 0x49, 0x4D):
            modrm = data[i + 1]
            if (modrm & 0xC0) == 0x80:          # mod=10 → disp32
                found.append(i - 1)
        i += 1
    return found


def suggest_entry(patch: dict, data: bytes, base: int):
    """把匹配点转成 patterns.json 条目。

    返回 (entry, drift, unsafe)：
      drift  —— 非修改位的漂移，可以安全地放开成 ??
      unsafe —— 修改位本身漂移，说明指令编码变了，不能自动给条目
    """
    search_orig = core.parse_pattern(patch["search"])
    replace_orig = core.parse_pattern(patch["replace"])
    mod_pos = set(core.diff_positions(search_orig, replace_orig))

    search, replace = [], []
    drift, unsafe = [], []
    for k in range(len(search_orig)):
        cur = data[base + k]
        if search_orig[k] is None:                  # 本来就是通配
            search.append("??")
            replace.append("??")
            continue
        if cur != search_orig[k]:
            drift.append((k, search_orig[k], cur))
        if k in mod_pos:
            if cur == search_orig[k]:               # 修改位没动，照常改写
                search.append(f"{search_orig[k]:02X}")
                replace.append(f"{replace_orig[k]:02X}")
            else:                                   # 修改位漂移：拒绝猜测
                unsafe.append((k, search_orig[k], replace_orig[k], cur))
                search.append("??")
                replace.append("??")
        else:
            if cur == search_orig[k]:
                search.append(f"{search_orig[k]:02X}")
                replace.append(f"{search_orig[k]:02X}")
            else:                                   # 非修改位漂移：两边都放开
                search.append("??")
                replace.append("??")

    entry = dict(patch)
    entry["search"] = " ".join(search)
    entry["replace"] = " ".join(replace)
    entry["verified"] = []
    return entry, drift, unsafe


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="微信防撤回补丁点重定位")
    ap.add_argument("--dll", required=True, help="Weixin.dll 路径（建议用副本）")
    ap.add_argument("--max-mismatch", type=int, default=2, help="允许的漂移字节数，默认 2")
    ap.add_argument("--dump", action="store_true", help="打印命中点附近的字节")
    ap.add_argument("--pick", default="", help="同级候选有多个时，指定采用的偏移（0x 开头）")
    ap.add_argument("--json", action="store_true", help="只输出建议条目 JSON")
    args = ap.parse_args(argv)

    path = Path(args.dll)
    data = path.read_bytes()
    pe = PE(data)
    if not args.json:
        print(f"文件      : {path}")
        print(f"大小      : {len(data):,} bytes")
        print(f"ImageBase : 0x{pe.base:X}")
        print()

    db = core.load_db()
    spec = db["apps"]["weixin"]

    verdict = 0
    for patch in spec["patches"]:
        search = core.parse_pattern(patch["search"])
        replace = core.parse_pattern(patch["replace"])
        if not args.json:
            print(f"== {patch['id']} ({patch['name']})  "
                  f"适配 {patch.get('min_version') or '?'} ~ {patch.get('max_version') or '最新'}")

        exact_s = core.find_all(data, search)
        exact_r = core.find_all(data, replace)
        if exact_s:
            off = exact_s[0]
            if not args.json:
                print(f"   精确匹配 search : {len(exact_s)} 处，偏移 0x{off:08X}"
                      f"（文件 {off:#x}）—— 当前未打补丁")
                print("   结论：特征码仍然有效，无需适配。")
                if args.dump:
                    print("   " + hexs(data[off - 16:off + 32]))
            continue
        if exact_r:
            off = exact_r[0]
            if not args.json:
                print(f"   精确匹配 replace: {len(exact_r)} 处，偏移 0x{off:08X}"
                      f" —— 该副本已打补丁")
                print("   结论：特征码有效；请用未打补丁的原始 DLL 复核。")
            continue

        # 模糊匹配
        hits = fuzzy_find(data, search, args.max_mismatch)
        if args.pick:
            want = int(args.pick, 16)
            hits = [h for h in hits if h[0] == want] or hits
        if hits:
            hits.sort(key=lambda t: t[1])
            best = hits[0][1]
            ties = [h for h in hits if h[1] == best]
            if len(ties) > 1 and not args.pick:
                if args.json:
                    print(json.dumps({"error": "多个同级候选", "candidates":
                                      [f"0x{o:08X}" for o, _, _ in ties]},
                                     ensure_ascii=False, indent=2))
                else:
                    print(f"   ⚠ 有 {len(ties)} 个同等匹配度的候选，不能自动决定：")
                    for o, b, pos in ties:
                        print(f"       0x{o:08X}  漂移 {b} 字节  {pos}")
                    print("     请用 --pick 0x<偏移> 指定，或按 docs/ADAPTING.md 手工确认。")
                verdict = 2
                continue
            off, bad, positions = hits[0]
            entry, drift, unsafe = suggest_entry(patch, data, off)
            if args.json:
                if unsafe:
                    print(json.dumps({"error": "修改位漂移，需人工确认",
                                      "offset": f"0x{off:08X}",
                                      "unsafe": [[k, f"{a:02X}", f"{b:02X}", f"{c:02X}"]
                                                 for k, a, b, c in unsafe]},
                                     ensure_ascii=False, indent=2))
                else:
                    print(json.dumps(entry, ensure_ascii=False, indent=2))
                continue
            print(f"   精确匹配       : 0 处")
            print(f"   模糊匹配       : {len(hits)} 处，最佳偏移 0x{off:08X}，"
                  f"漂移 {bad} 字节")
            for k in positions:
                print(f"      第 {k:2d} 字节: 期望 {search[k]:02X}  实际 {data[off + k]:02X}")
            print(f"   上下文         : {hexs(data[off - 8:off + 24])}")
            print()
            if unsafe:
                print("   ⚠ 拒绝自动生成特征码：本次漂移落在『修改位』上，")
                print("     说明这条指令的编码已经变了，照抄旧字节会写出错误指令。")
                for k, old, new, cur in unsafe:
                    print(f"       第 {k} 字节: 原 {old:02X} → 现 {cur:02X}"
                          f"（原计划改成 {new:02X}）")
                print("     请按 docs/ADAPTING.md『手工路径』确认新的改写方式。")
                verdict = 2
                continue
            if drift:
                print("   非修改位漂移（已放开成 ??）：")
                for k, old, cur in drift:
                    print(f"       第 {k} 字节: {old:02X} → {cur:02X}")
            print("   建议条目（贴进 wxar/patterns.json 的 patches 数组）：")
            print(json.dumps(entry, ensure_ascii=False, indent=2))
            continue

        # 字符串锚定兜底
        if not args.json:
            print(f"   精确/模糊匹配均为 0 处，改用字符串锚定……")
        anchors = [b"replacemsg", b"newmsgid", b"revoketime"]
        for anchor in anchors:
            idx = data.find(anchor)
            if idx < 0:
                continue
            va = pe.va_of_off(idx)
            if va is None:
                continue
            refs = find_lea_refs(pe, va)
            if not args.json:
                print(f"   字符串 {anchor.decode():12s} @ VA 0x{va:X}，"
                      f"被引用 {len(refs)} 次")
            for site in refs[:4]:
                stores = find_stores_near(pe, site)
                if not args.json:
                    print(f"      引用点 0x{pe.va_of_off(site):X}（文件 {site:#x}）"
                          f" 附近 mov [reg+disp], r64 共 {len(stores)} 处")
                for st in stores[:6]:
                    if not args.json:
                        print(f"        候选 0x{pe.va_of_off(st):X}  "
                              f"{hexs(data[st:st + 7])}")
        verdict = 1
        if not args.json:
            print()
            print("   未自动定位到补丁点。请按 docs/ADAPTING.md 的『手工路径』逐步确认，")
            print("   不要直接把候选偏移写进特征库。")

    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
