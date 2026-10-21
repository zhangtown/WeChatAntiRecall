"""极简 Tkinter 界面。

单窗口：安装列表 + 状态 + 打补丁/还原按钮 + 日志。
刻意不引入第三方依赖，方便 pyinstaller 打包成单文件 exe。
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__, core

TITLE = "微信防撤回补丁"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{TITLE} v{__version__}")
        self.geometry("880x560")
        self.minsize(720, 460)

        self.analyses: list[core.Analysis] = []
        self._build_ui()
        self.after(50, self.refresh)

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text=TITLE, font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        ttk.Label(top, text=f"v{__version__}", foreground="#888").pack(side="left", padx=8)

        self.admin_var = tk.StringVar()
        self.admin_label = tk.Label(top, textvariable=self.admin_var, fg="#b00")
        self.admin_label.pack(side="right")
        self.admin_var.set("" if core.is_admin() else "当前非管理员")

        cols = ("version", "state", "path")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=5)
        self.tree.heading("version", text="版本")
        self.tree.heading("state", text="状态")
        self.tree.heading("path", text="Weixin.dll")
        self.tree.column("version", width=110, anchor="center")
        self.tree.column("state", width=100, anchor="center")
        self.tree.column("path", width=600, anchor="w")
        self.tree.pack(fill="x", padx=12, pady=(4, 0))
        self.tree.tag_configure("patched", foreground="#0a7d28")
        self.tree.tag_configure("original", foreground="#b06000")
        self.tree.tag_configure("unknown", foreground="#a00")

        bar = ttk.Frame(self, padding=(12, 8))
        bar.pack(fill="x")
        self.btn_refresh = ttk.Button(bar, text="刷新", command=self.refresh)
        self.btn_patch = ttk.Button(bar, text="打补丁", command=self.on_patch)
        self.btn_restore = ttk.Button(bar, text="还原备份", command=self.on_restore)
        self.btn_open = ttk.Button(bar, text="打开目录", command=self.on_open)
        self.btn_admin = ttk.Button(bar, text="以管理员重启", command=self.on_elevate)
        for b in (self.btn_refresh, self.btn_patch, self.btn_restore, self.btn_open):
            b.pack(side="left", padx=(0, 8))
        self.btn_admin.pack(side="right")

        self.log = tk.Text(self, height=12, wrap="word", state="disabled",
                           font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                           insertbackground="#d4d4d4")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def info(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.update_idletasks()

    # ------------------------------------------------------------ actions
    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.analyses.clear()
        self.info("扫描安装……")
        self.configure(cursor="watch")
        try:
            for inst in core.discover():
                a = core.analyze(inst.dll, inst.version)
                self.analyses.append(a)
                tag = "patched" if a.state is core.State.PATCHED else (
                    "original" if a.state is core.State.ORIGINAL else "unknown")
                self.tree.insert("", "end", iid=str(len(self.analyses) - 1),
                                 values=(a.install.version or "未知", a.state.label,
                                         str(a.install.dll)), tags=(tag,))
                self.info(f"{a.install.version or '未知'}  {a.state.label}"
                          + (f"  {a.detail}" if a.detail else ""))
            if not self.analyses:
                self.info("没有找到微信 4.x 安装。")
        finally:
            self.configure(cursor="")

    def _selected(self) -> core.Analysis | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(TITLE, "请先在列表里选择一个安装。")
            return None
        return self.analyses[int(sel[0])]

    def on_patch(self) -> None:
        a = self._selected()
        if a is None:
            return
        if a.state is core.State.PATCHED:
            messagebox.showinfo(TITLE, "这个安装已经是补丁状态了。")
            return
        if a.state is core.State.UNKNOWN:
            messagebox.showerror(TITLE, f"无法打补丁：{a.detail or '状态未知'}")
            return
        if not core.is_writable(a.install.dll):
            if messagebox.askyesno(TITLE, "改写这个文件需要管理员权限，现在提权重启？"):
                core.relaunch_as_admin(["gui"])
                self.destroy()
            return

        self.configure(cursor="watch")
        try:
            res = core.apply_patch(a)
            self.info(f"补丁写入成功：{a.install.dll}")
            self.info(f"  改动字节 {len(res.changed)} 个，偏移 0x{a.offset:08X}")
            if res.backup:
                self.info(f"  备份 {res.backup.name}")
            self.info(f"  SHA256 {res.sha_before[:12]}… -> {res.sha_after[:12]}…")
            messagebox.showinfo(TITLE, "补丁完成，重新打开微信生效。")
        except core.PatchError as exc:
            self.info(f"失败：{exc}")
            messagebox.showerror(TITLE, str(exc))
        finally:
            self.configure(cursor="")
            self.refresh()

    def on_restore(self) -> None:
        a = self._selected()
        if a is None:
            return
        backups = core.find_backups(a.install.dll)
        if not backups:
            messagebox.showerror(TITLE, "没有找到备份（Weixin.dll.bak*）。")
            return
        if not messagebox.askyesno(TITLE, f"用 {backups[0].name} 还原？"):
            return
        if not core.is_writable(a.install.dll):
            if messagebox.askyesno(TITLE, "还原需要管理员权限，现在提权重启？"):
                core.relaunch_as_admin(["gui"])
                self.destroy()
            return
        self.configure(cursor="watch")
        try:
            used, sha = core.restore(a.install.dll, backups[0])
            self.info(f"已用 {used.name} 还原，SHA256 {sha[:16]}…")
            messagebox.showinfo(TITLE, "还原完成。")
        except core.PatchError as exc:
            self.info(f"还原失败：{exc}")
            messagebox.showerror(TITLE, str(exc))
        finally:
            self.configure(cursor="")
            self.refresh()

    def on_open(self) -> None:
        a = self._selected()
        if a is None:
            return
        import os
        os.startfile(str(a.install.directory))  # noqa: S606

    def on_elevate(self) -> None:
        if core.is_admin():
            messagebox.showinfo(TITLE, "已经是管理员身份。")
            return
        if core.relaunch_as_admin(["gui"]):
            self.destroy()
        else:
            messagebox.showerror(TITLE, "提权被取消或失败。")


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
