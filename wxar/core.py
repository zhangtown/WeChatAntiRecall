"""微信 4.x 防撤回补丁 —— 核心逻辑。

设计要点：
  * 不硬编码文件偏移：微信每次更新偏移都会变，靠特征码定位。
  * 写前备份、写后回读校验，任何一步异常都可以回滚。
  * 状态可查询：原始 / 已打补丁 / 特征不匹配（多半是版本变了）。

本模块只做文件层面的事，不提权、不碰进程，便于在测试里对副本操作。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

DB_PATH = Path(__file__).with_name("patterns.json")
DEFAULT_APP = "weixin"


# --------------------------------------------------------------------------- #
# 特征码
# --------------------------------------------------------------------------- #
def parse_pattern(expr: str) -> list[int | None]:
    """把 "48 89 ?? ?? 4C" 解析成 [0x48, 0x89, None, None, 0x4C]。"""
    out: list[int | None] = []
    for tok in expr.replace(",", " ").split():
        if tok in ("?", "??", "**"):
            out.append(None)
            continue
        try:
            val = int(tok, 16)
        except ValueError as exc:
            raise ValueError(f"无法解析的字节: {tok!r}") from exc
        if not 0 <= val <= 0xFF:
            raise ValueError(f"字节超出范围: {tok!r}")
        out.append(val)
    if not out:
        raise ValueError("空特征码")
    return out


def longest_literal_run(pat: list[int | None]) -> tuple[int, int]:
    """特征码里最长的一段连续固定字节 [start, end)，用来当搜索锚点。"""
    best = (0, 0)
    start = None
    for i, v in enumerate(list(pat) + [None]):     # 末尾补 None 收尾
        if v is None:
            if start is not None:
                if i - start > best[1] - best[0]:
                    best = (start, i)
                start = None
        elif start is None:
            start = i
    return best


def find_all(buf: bytes, pat: list[int | None]) -> list[int]:
    """返回 pat 在 buf 中所有匹配的起始偏移（支持通配）。

    先用最长固定段做一次 C 层 bytes.find，再在候选点上逐字节校验，
    避免对 200 MB 的 DLL 做 Python 级逐字节比较。
    """
    n = len(pat)
    if n == 0 or n > len(buf):
        return []
    a0, a1 = longest_literal_run(pat)
    if a1 == a0:                       # 没有固定字节，无从下手
        return []
    anchor = bytes(pat[a0:a1])
    limit = len(buf) - n
    hits: list[int] = []
    pos = 0
    while True:
        i = buf.find(anchor, pos)
        if i < 0:
            break
        base = i - a0
        if base < 0:
            pos = i + 1
            continue
        if base > limit:
            break
        if all(p is None or buf[base + k] == p for k, p in enumerate(pat)):
            hits.append(base)
        pos = i + 1
    return hits


def diff_positions(search: list[int | None], replace: list[int | None]) -> list[int]:
    """两条等长特征码里真正需要改写的字节下标。"""
    if len(search) != len(replace):
        raise ValueError("search 与 replace 长度不一致")
    return [i for i, (a, b) in enumerate(zip(search, replace))
            if a is not None and a != b]


# --------------------------------------------------------------------------- #
# 安装信息
# --------------------------------------------------------------------------- #
class State(str, Enum):
    ORIGINAL = "original"
    PATCHED = "patched"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {"original": "未打补丁", "patched": "已打补丁", "unknown": "未知"}[self.value]


@dataclass
class Install:
    dll: Path
    version: str = ""
    app: str = DEFAULT_APP

    @property
    def directory(self) -> Path:
        return self.dll.parent

    @property
    def exe(self) -> Path:
        return self.directory / "Weixin.exe"

    def __str__(self) -> str:
        return f"{self.version or '未知版本'}  {self.dll}"


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)) or (0,)


def version_in_range(version: str, lo: str, hi: str) -> bool:
    if not version:
        return True                     # 版本未知时不拦，交给人判断
    v = version_key(version)
    if lo and v < version_key(lo):
        return False
    if hi and v >= version_key(hi):
        return False
    return True


def candidate_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Tencent" / "Weixin")
    # 去重且只保留存在的
    uniq: list[Path] = []
    for r in roots:
        if r not in uniq and r.exists():
            uniq.append(r)
    return uniq


def discover(app: str = DEFAULT_APP) -> list[Install]:
    """在常见安装位置找 Weixin.dll。"""
    module = "Weixin.dll"
    found: list[Install] = []
    seen: set[Path] = set()
    for root in candidate_roots():
        for dll in [root / module, *root.glob(f"*/{module}")]:
            if not dll.is_file() or dll in seen:
                continue
            seen.add(dll)
            name = dll.parent.name
            version = name if re.fullmatch(r"\d+(\.\d+)+", name) else ""
            found.append(Install(dll=dll, version=version, app=app))
    found.sort(key=lambda i: version_key(i.version), reverse=True)
    return found


# --------------------------------------------------------------------------- #
# 分析
# --------------------------------------------------------------------------- #
@dataclass
class Analysis:
    install: Install
    patch: dict | None
    state: State
    offset: int | None = None
    detail: str = ""
    size: int = 0

    @property
    def ok(self) -> bool:
        return self.state is not State.UNKNOWN


def db_path() -> Path:
    """特征库路径；PyInstaller 单文件模式下从 _MEIPASS 取。"""
    if DB_PATH.is_file():
        return DB_PATH
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        alt = Path(meipass) / "wxar" / "patterns.json"
        if alt.is_file():
            return alt
    return DB_PATH


def load_db(path: Path | None = None) -> dict:
    with open(path or db_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def app_spec(app: str = DEFAULT_APP, db: dict | None = None) -> dict:
    db = db or load_db()
    return db["apps"][app]


def pick_patch(version: str, spec: dict) -> dict | None:
    for p in spec["patches"]:
        if version_in_range(version, p.get("min_version", ""), p.get("max_version", "")):
            return p
    return None


def analyze(dll: Path, version: str = "", app: str = DEFAULT_APP,
            db: dict | None = None) -> Analysis:
    spec = app_spec(app, db)
    install = Install(dll=Path(dll), version=version, app=app)
    if not install.dll.is_file():
        return Analysis(install, None, State.UNKNOWN, detail="文件不存在")
    data = install.dll.read_bytes()
    install.version = install.version or next(
        (n for n in [install.dll.parent.name]
         if re.fullmatch(r"\d+(\.\d+)+", n)), "")
    if not (data[:2] == b"MZ"):
        return Analysis(install, None, State.UNKNOWN, size=len(data), detail="不是 PE 文件")

    patch = pick_patch(install.version, spec)
    if patch is None:
        return Analysis(install, None, State.UNKNOWN, size=len(data),
                        detail=f"没有适配 {install.version or '未知版本'} 的特征码")

    search = parse_pattern(patch["search"])
    replace = parse_pattern(patch["replace"])
    hits_orig = find_all(data, search)
    if len(hits_orig) == 1:
        return Analysis(install, patch, State.ORIGINAL, hits_orig[0], size=len(data))

    hits_patched = find_all(data, replace)
    if len(hits_patched) == 1 and not hits_orig:
        return Analysis(install, patch, State.PATCHED, hits_patched[0], size=len(data))
    detail = f"特征命中异常（原始 {len(hits_orig)} 次 / 补丁 {len(hits_patched)} 次）"
    return Analysis(install, patch, State.UNKNOWN, size=len(data), detail=detail)


# --------------------------------------------------------------------------- #
# 补丁应用 / 还原
# --------------------------------------------------------------------------- #
class PatchError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wechat_pids(process: str = "Weixin.exe") -> list[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process}", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == process.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def backup_path(dll: Path) -> Path:
    return dll.with_name(dll.name + time.strftime(".bak-%Y%m%d-%H%M%S"))


def find_backups(dll: Path) -> list[Path]:
    """所有可用备份，最新的排前面（含早期手工留的 .bak）。"""
    cands = list(dll.parent.glob(dll.name + ".bak*"))
    cands = [c for c in cands if c.is_file()]
    cands.sort(key=lambda p: (p.name.endswith(".bak"), p.stat().st_mtime), reverse=True)
    return cands


@dataclass
class PatchResult:
    analysis: Analysis
    backup: Path | None = None
    changed: list[int] = field(default_factory=list)
    sha_before: str = ""
    sha_after: str = ""


def apply_patch(analysis: Analysis, *, dry_run: bool = False,
                make_backup: bool = True) -> PatchResult:
    """按分析结果改写 DLL。调用方负责提权和退进程。"""
    inst, patch = analysis.install, analysis.patch
    if analysis.state is State.PATCHED:
        raise PatchError("已经是补丁状态，无需重复操作")
    if analysis.state is not State.ORIGINAL or patch is None or analysis.offset is None:
        raise PatchError(f"无法打补丁：{analysis.detail or '状态未知'}")

    pids = wechat_pids()
    if not is_writable(inst.dll):
        if pids:
            raise PatchError(f"微信正在运行（PID {', '.join(map(str, pids))}），请先完全退出")
        raise PatchError("没有写入权限，请以管理员身份运行")

    search = parse_pattern(patch["search"])
    replace = parse_pattern(patch["replace"])
    positions = diff_positions(search, replace)
    off = analysis.offset

    if dry_run:
        return PatchResult(analysis, None, positions)

    sha_before = sha256_file(inst.dll)
    backup = None
    if make_backup:
        backup = backup_path(inst.dll)
        shutil.copy2(inst.dll, backup)
        if sha256_file(backup) != sha_before:
            raise PatchError("备份校验失败，已中止")

    try:
        with open(inst.dll, "r+b") as f:
            f.seek(off)
            cur = f.read(len(search))
            if len(cur) != len(search) or any(
                    p is not None and cur[k] != p for k, p in enumerate(search)):
                raise PatchError("补丁点字节与预期不符，已中止")
            for i in positions:
                f.seek(off + i)
                f.write(bytes([replace[i]]))
            f.flush()
            os.fsync(f.fileno())
    except PermissionError as exc:
        raise PatchError("没有写入权限，请以管理员身份运行") from exc
    except PatchError:
        raise
    except OSError as exc:
        raise PatchError(f"写入失败：{exc}") from exc

    after = analyze(inst.dll, inst.version, inst.app)
    if after.state is not State.PATCHED:
        raise PatchError("写后校验未通过，请用备份还原")

    return PatchResult(analysis, backup, positions, sha_before, sha256_file(inst.dll))


def restore(dll: Path, backup: Path | None = None) -> tuple[Path, str]:
    """用备份覆盖 DLL，返回 (使用的备份, 还原后 sha256)。"""
    dll = Path(dll)
    if backup is None:
        backups = find_backups(dll)
        if not backups:
            raise PatchError(f"找不到任何备份（{dll.name}.bak*）")
        backup = backups[0]
    backup = Path(backup)
    if not backup.is_file():
        raise PatchError(f"备份不存在：{backup}")

    pids = wechat_pids()
    if not is_writable(dll):
        if pids:
            raise PatchError(f"微信正在运行（PID {', '.join(map(str, pids))}），请先完全退出")
        raise PatchError("没有写入权限，请以管理员身份运行")

    try:
        shutil.copy2(backup, dll)
    except PermissionError as exc:
        raise PatchError("没有写入权限，请以管理员身份运行") from exc
    return backup, sha256_file(dll)


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_writable(path: Path) -> bool:
    """目标文件当前是否可直接写（Program Files 下通常需要提权）。"""
    try:
        with open(path, "r+b"):
            return True
    except OSError:
        return False


def relaunch_as_admin(argv: list[str] | None = None) -> bool:
    """以管理员身份重启自己；返回是否成功发起。"""
    if sys.platform != "win32":
        return False
    args = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, args
    else:
        exe = sys.executable
        params = [os.path.abspath(sys.argv[0]), *args]
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, subprocess.list2cmdline(params), None, 1)
    except Exception:
        return False
    return rc > 32
