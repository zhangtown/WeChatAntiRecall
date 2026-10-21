"""命令行入口：python -m wxar <命令>。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import core

BANNER = "WeChatAntiRecall —— 微信 4.x 防撤回补丁"


def _fmt_bytes(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _print_analysis(a: core.Analysis, verbose: bool = True) -> None:
    status = a.state.label
    suffix = f"  偏移 0x{a.offset:08X}" if a.offset is not None else ""
    print(f"  版本    : {a.install.version or '未知'}")
    print(f"  文件    : {a.install.dll}")
    print(f"  大小    : {_fmt_bytes(a.size)}")
    print(f"  状态    : {status}{suffix}")
    if a.detail and verbose:
        print(f"  说明    : {a.detail}")


def _resolve(args) -> list[core.Analysis]:
    if args.file:
        path = Path(args.file).expanduser()
        version = ""
        if path.parent and path.parent.name:
            import re
            if re.fullmatch(r"\d+(\.\d+)+", path.parent.name):
                version = path.parent.name
        return [core.analyze(path, version)]

    installs = core.discover()
    if not installs:
        print("没有找到微信 4.x 安装（Weixin.dll）。可用 --file 指定文件。")
        return []
    if args.version:
        installs = [i for i in installs if args.version in i.version]
        if not installs:
            print(f"没有匹配版本 {args.version} 的安装")
            return []
    return [core.analyze(i.dll, i.version) for i in installs]


# --------------------------------------------------------------------------- #
def cmd_list(args) -> int:
    analyses = _resolve(args)
    if not analyses:
        return 1
    for idx, a in enumerate(analyses, 1):
        print(f"[{idx}] {a.install.version or '未知版本'}")
        _print_analysis(a)
        print()
    return 0


def cmd_status(args) -> int:
    analyses = _resolve(args)
    if not analyses:
        return 1
    rc = 0
    for a in analyses:
        _print_analysis(a)
        if a.state is core.State.UNKNOWN:
            rc = 1
    return rc


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _ensure_admin(args, rerun_args: list[str], dll: Path) -> bool:
    """返回 True 表示可以继续；False 表示已转交提权进程或用户放弃。"""
    if core.is_writable(dll):
        return True
    pids = core.wechat_pids()
    if pids:
        print(f"微信正在运行（PID {', '.join(map(str, pids))}），请先从托盘完全退出微信。")
        return False
    if args.no_elevate:
        print("没有写入权限（微信可能在运行，或需要管理员），请完全退出微信后以管理员身份运行。")
        return False
    if not args.yes and not _confirm("需要管理员权限，是否以管理员身份重新运行？"):
        print("已取消。")
        return False
    print("正在请求管理员权限……")
    if core.relaunch_as_admin(rerun_args):
        print("已在新窗口继续，请在那里查看结果。")
    else:
        print("提权被取消或失败。")
    return False


def cmd_patch(args) -> int:
    analyses = _resolve(args)
    if not analyses:
        return 1
    if len(analyses) > 1 and not args.all:
        print("检测到多个安装，请用 --version 指定，或加 --all 全部处理：")
        for a in analyses:
            print(f"  {a.install.version or '未知'}  {a.install.dll}")
        return 1

    targets = analyses if args.all else analyses[:1]
    pending = [a for a in targets if a.state is core.State.ORIGINAL]
    if not pending:
        for a in targets:
            print(f"{a.install.version}: {a.state.label} —— {a.detail or '无需操作'}")
        return 0 if all(a.state is core.State.PATCHED for a in targets) else 1

    rerun = ["patch", *(["--file", str(args.file)] if args.file else []),
             *(["--wechat-version", args.version] if args.version else []),
             "--yes", "--no-elevate"]
    if not _ensure_admin(args, rerun, pending[0].install.dll):
        return 2

    rc = 0
    for a in pending:
        print(f"==> {a.install.version or '未知版本'}  {a.install.dll}")
        if getattr(args, "dry_run", False):
            print("  [dry-run] 不做写入")
            _print_analysis(a)
            continue
        try:
            res = core.apply_patch(a)
        except core.PatchError as exc:
            print(f"  失败：{exc}")
            rc = 1
            continue
        print(f"  已写入，改动字节 {len(res.changed)} 个（偏移 0x{a.offset:08X} 起）")
        if res.backup:
            print(f"  备份：{res.backup.name}")
        print(f"  校验：通过（{res.sha_before[:12]}… -> {res.sha_after[:12]}…）")
        print("  重新打开微信即可生效。")
    return rc


def cmd_restore(args) -> int:
    analyses = _resolve(args)
    if not analyses:
        return 1
    a = analyses[0]
    backups = core.find_backups(a.install.dll)
    if not backups:
        print(f"没有找到 {a.install.dll.name}.bak* 备份，无法还原")
        return 1

    if not _ensure_admin(args, ["restore", "--file", str(a.install.dll),
                                "--yes", "--no-elevate"], a.install.dll):
        return 2

    try:
        used, sha = core.restore(a.install.dll, Path(args.backup) if args.backup else None)
    except core.PatchError as exc:
        print(f"还原失败：{exc}")
        return 1
    print(f"已用 {used.name} 还原 {a.install.dll.name}")
    print(f"还原后 SHA256：{sha}")
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    def common(parser, suppress: bool) -> None:
        d = argparse.SUPPRESS if suppress else ""
        parser.add_argument("--file", default=d, metavar="DLL",
                            help="直接指定 Weixin.dll 路径")
        parser.add_argument("--wechat-version", dest="version", default=d,
                            metavar="VER", help="只处理指定版本（如 4.1.15）")
        parser.add_argument("--yes", action="store_true",
                            default=argparse.SUPPRESS if suppress else False,
                            help="跳过确认")
        parser.add_argument("--no-elevate", action="store_true", dest="no_elevate",
                            default=argparse.SUPPRESS if suppress else False,
                            help="不提权")

    p = argparse.ArgumentParser(prog="wxar", description=BANNER)
    common(p, suppress=False)
    p.add_argument("-v", "--print-version", action="version",
                   version=f"%(prog)s 1.0.0")

    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("list", help="列出所有安装及状态")
    common(sp, suppress=True)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("status", help="查看补丁状态")
    common(sp, suppress=True)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("patch", help="打补丁")
    common(sp, suppress=True)
    sp.add_argument("--all", action="store_true", help="处理所有安装")
    sp.add_argument("--dry-run", action="store_true", help="只看会改哪里，不写文件")
    sp.set_defaults(func=cmd_patch)

    sp = sub.add_parser("restore", help="从备份还原")
    common(sp, suppress=True)
    sp.add_argument("--backup", default="", help="指定备份文件，默认用最新的")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("gui", help="打开图形界面")
    common(sp, suppress=True)
    sp.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
