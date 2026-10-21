"""Tkinter 界面：定位文件 → 展示版本/状态 → 用户确认后才动手。

流程刻意做成三步，避免"一打开就改文件"：
  1. 定位 Weixin.dll（自动检测或手动浏览）
  2. 展示版本、大小、当前状态、补丁点
  3. 点「打补丁」并二次确认后才写入

所有耗时操作都在后台线程，结果经队列回到主线程刷新界面。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, core

TITLE = "微信防撤回补丁"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{TITLE} v{__version__}")
        self.geometry("920x620")
        self.minsize(780, 540)

        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.analyses: list[core.Analysis] = []
        self.current: core.Analysis | None = None

        self._build_ui()
        self.after(120, self._poll)
        self.after(200, self.auto_detect)      # 只检测，不写文件

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        head = ttk.Frame(self, padding=(14, 12, 14, 2))
        head.pack(fill="x")
        ttk.Label(head, text=TITLE, font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Label(head, text=f"v{__version__}", foreground="#888").pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(head, textvariable=self.status_var, foreground="#555").pack(side="right")

        # ---- 步骤 1：定位
        step1 = ttk.LabelFrame(self, text=" 步骤 1 · 定位 Weixin.dll ", padding=10)
        step1.pack(fill="x", padx=14, pady=(8, 0))

        self.path_var = tk.StringVar(value="")
        entry = ttk.Entry(step1, textvariable=self.path_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True)

        self.btn_browse = ttk.Button(step1, text="浏览…", width=9, command=self.on_browse)
        self.btn_browse.pack(side="left", padx=(8, 0))
        self.btn_detect = ttk.Button(step1, text="自动检测", width=10, command=self.auto_detect)
        self.btn_detect.pack(side="left", padx=(6, 0))

        pick = ttk.Frame(self, padding=(14, 6, 14, 0))
        pick.pack(fill="x")
        ttk.Label(pick, text="检测到的安装：").pack(side="left")
        self.combo = ttk.Combobox(pick, state="readonly", width=88)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self._on_select)

        # ---- 步骤 2：信息
        step2 = ttk.LabelFrame(self, text=" 步骤 2 · 确认文件信息 ", padding=10)
        step2.pack(fill="x", padx=14, pady=(8, 0))
        step2.columnconfigure(1, weight=1)

        self.info_vars: dict[str, tk.StringVar] = {}
        rows = [("file", "文件名"), ("version", "版本"), ("size", "大小"),
                ("state", "当前状态"), ("patch", "补丁点")]
        for r, (key, label) in enumerate(rows):
            ttk.Label(step2, text=label, width=9, anchor="e").grid(
                row=r, column=0, sticky="e", pady=2, padx=(0, 8))
            var = tk.StringVar(value="—")
            self.info_vars[key] = var
            lbl = ttk.Label(step2, textvariable=var, anchor="w")
            lbl.grid(row=r, column=1, sticky="w", pady=2)
            if key == "state":
                self.state_label = lbl

        # ---- 步骤 3：操作
        step3 = ttk.LabelFrame(self, text=" 步骤 3 · 执行 ", padding=10)
        step3.pack(fill="x", padx=14, pady=(8, 0))

        self.btn_patch = ttk.Button(step3, text="打补丁", width=14,
                                    command=self.on_patch, state="disabled")
        self.btn_patch.pack(side="left")
        self.btn_restore = ttk.Button(step3, text="还原备份", width=14,
                                      command=self.on_restore, state="disabled")
        self.btn_restore.pack(side="left", padx=(8, 0))
        self.btn_open = ttk.Button(step3, text="打开所在目录", command=self.on_open,
                                   state="disabled")
        self.btn_open.pack(side="left", padx=(8, 0))
        self.btn_admin = ttk.Button(step3, text="以管理员重启", command=self.on_elevate)
        self.btn_admin.pack(side="right")

        self.log = tk.Text(self, height=9, wrap="word", state="disabled",
                           font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log.pack(fill="both", expand=True, padx=14, pady=(10, 14))

        self.info("先定位 Weixin.dll，确认版本和状态后再执行。")

    # ------------------------------------------------------------ 基础
    def info(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, flag: bool, text: str = "") -> None:
        self.busy = flag
        self.configure(cursor="watch" if flag else "")
        self.status_var.set(text or ("处理中…" if flag else "就绪"))
        for b in (self.btn_browse, self.btn_detect, self.btn_open, self.btn_admin):
            b.configure(state="disabled" if flag else "normal")
        if flag:
            self.btn_patch.configure(state="disabled")
            self.btn_restore.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle(self, kind: str, payload) -> None:
        if kind == "detected":
            self._apply_detected(payload)
        elif kind == "picked":
            self._apply_picked(payload)
        elif kind == "patched":
            self._apply_patched(*payload)
        elif kind == "restored":
            self._apply_restored(*payload)
        elif kind == "error":
            self.set_busy(False)
            self.info(f"失败：{payload}")
            messagebox.showerror(TITLE, str(payload))

    # ------------------------------------------------------------ 定位
    def auto_detect(self) -> None:
        if self.busy:
            return
        self.set_busy(True, "正在检测安装…")
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self) -> None:
        try:
            items = [core.analyze(i.dll, i.version) for i in core.discover()]
            self.q.put(("detected", items))
        except Exception as exc:                      # noqa: BLE001
            self.q.put(("error", f"检测失败：{exc}"))

    def _apply_detected(self, items: list[core.Analysis]) -> None:
        self.set_busy(False)
        self.analyses = items
        labels = [f"{a.install.version or '未知版本'}    {a.install.dll}" for a in items]
        self.combo.configure(values=labels)
        if not items:
            self.combo.set("")
            self.info("没有检测到微信安装，请点「浏览…」手动选择 Weixin.dll。")
            self._clear_info()
            self.path_var.set("")
            return
        self.combo.current(0)
        self.info(f"检测到 {len(items)} 个安装。")
        self._set_current(items[0])

    def on_browse(self) -> None:
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="选择 Weixin.dll",
            filetypes=[("微信主模块", "Weixin.dll"), ("DLL 文件", "*.dll"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.set_busy(True, "正在读取…")
        threading.Thread(target=self._pick_worker, args=(Path(path),), daemon=True).start()

    def _pick_worker(self, path: Path) -> None:
        try:
            self.q.put(("picked", core.analyze(path)))
        except Exception as exc:                      # noqa: BLE001
            self.q.put(("error", f"读取失败：{exc}"))

    def _apply_picked(self, a: core.Analysis) -> None:
        self.set_busy(False)
        if not a.install.dll.is_file():
            self.info(f"文件不存在：{a.install.dll}")
            self._clear_info()
            return
        self.analyses = [a]
        label = f"{a.install.version or '未知版本'}    {a.install.dll}"
        self.combo.configure(values=[label])
        self.combo.set(label)
        self.combo.current(0)
        self._set_current(a)

    def _on_select(self, _event=None) -> None:
        idx = self.combo.current()
        if 0 <= idx < len(self.analyses):
            self._set_current(self.analyses[idx])

    # ------------------------------------------------------------ 展示
    def _clear_info(self) -> None:
        self.current = None
        for v in self.info_vars.values():
            v.set("—")
        self.state_label.configure(foreground="")
        self.btn_patch.configure(state="disabled")
        self.btn_restore.configure(state="disabled")
        self.btn_open.configure(state="disabled")

    def _set_current(self, a: core.Analysis) -> None:
        self.current = a
        self.path_var.set(str(a.install.dll))
        self.info_vars["file"].set(a.install.dll.name)
        self.info_vars["version"].set(a.install.version or "未知")
        self.info_vars["size"].set(f"{a.size / 1024 / 1024:.1f} MB" if a.size else "—")

        if a.state is core.State.PATCHED:
            self.info_vars["state"].set("已打补丁")
            self.state_label.configure(foreground="#0a7d28")
        elif a.state is core.State.ORIGINAL:
            self.info_vars["state"].set("未打补丁")
            self.state_label.configure(foreground="#b06000")
        else:
            self.info_vars["state"].set(f"无法识别 —— {a.detail}")
            self.state_label.configure(foreground="#a00")

        self.info_vars["patch"].set(
            f"0x{a.offset:08X}（{a.patch['name']}）" if a.offset is not None and a.patch
            else "—")

        can_patch = a.state is core.State.ORIGINAL and not self.busy
        self.btn_patch.configure(state="normal" if can_patch else "disabled")
        self.btn_restore.configure(
            state="normal" if core.find_backups(a.install.dll) and not self.busy else "disabled")
        self.btn_open.configure(state="normal")

        self.info(f"已载入：{a.install.dll}")
        self.info(f"  版本 {a.install.version or '未知'}  状态 {a.state.label}"
                  + (f"  {a.detail}" if a.detail else ""))

    # ------------------------------------------------------------ 执行
    def on_patch(self) -> None:
        a = self.current
        if a is None or self.busy:
            return
        if a.state is not core.State.ORIGINAL:
            messagebox.showinfo(TITLE, f"当前状态是「{a.state.label}」，无法打补丁。")
            return

        detail = (
            f"文件：{a.install.dll}\n"
            f"版本：{a.install.version or '未知'}\n"
            f"补丁点：0x{a.offset:08X}\n\n"
            f"将要修改 1 个字节（指令 mov → sub）：\n"
            f"  48 89 86 …  →  48 29 86 …\n\n"
            f"写入前会自动备份为 {a.install.dll.name}.bak-时间戳。\n"
            f"确定继续吗？"
        )
        if not messagebox.askyesno("确认打补丁", detail, icon="warning"):
            self.info("已取消打补丁。")
            return

        if not core.is_writable(a.install.dll):
            pids = core.wechat_pids()
            if pids:
                messagebox.showerror(TITLE, "微信正在运行，请先从托盘完全退出微信。")
                return
            if messagebox.askyesno(TITLE, "改写这个文件需要管理员权限，现在提权重启？"):
                core.relaunch_as_admin(["gui"])
                self.destroy()
            return

        self.set_busy(True, "正在写入补丁…")
        threading.Thread(target=self._patch_worker, args=(a,), daemon=True).start()

    def _patch_worker(self, a: core.Analysis) -> None:
        try:
            self.q.put(("patched", (a, core.apply_patch(a))))
        except core.PatchError as exc:
            self.q.put(("error", str(exc)))
        except Exception as exc:                      # noqa: BLE001
            self.q.put(("error", f"意外错误：{exc}"))

    def _apply_patched(self, a: core.Analysis, res: core.PatchResult) -> None:
        self.info(f"补丁写入成功：{a.install.dll}")
        self.info(f"  改动 {len(res.changed)} 个字节，偏移 0x{a.offset:08X}")
        if res.backup:
            self.info(f"  备份 {res.backup.name}")
        self.info(f"  SHA256 {res.sha_before[:12]}… -> {res.sha_after[:12]}…")
        messagebox.showinfo(TITLE, "补丁完成，重新打开微信生效。")
        self._reload_current()

    def on_restore(self) -> None:
        a = self.current
        if a is None or self.busy:
            return
        backups = core.find_backups(a.install.dll)
        if not backups:
            messagebox.showerror(TITLE, "没有找到备份（Weixin.dll.bak*）。")
            return
        if not messagebox.askyesno(
                "确认还原",
                f"将用备份覆盖当前文件：\n\n{a.install.dll}\n\n备份：{backups[0].name}\n\n"
                f"确定继续吗？", icon="warning"):
            self.info("已取消还原。")
            return

        if not core.is_writable(a.install.dll):
            if messagebox.askyesno(TITLE, "还原需要管理员权限，现在提权重启？"):
                core.relaunch_as_admin(["gui"])
                self.destroy()
            return

        self.set_busy(True, "正在还原…")
        threading.Thread(target=self._restore_worker, args=(a, backups[0]),
                         daemon=True).start()

    def _restore_worker(self, a: core.Analysis, backup: Path) -> None:
        try:
            used, sha = core.restore(a.install.dll, backup)
            self.q.put(("restored", (used, sha)))
        except core.PatchError as exc:
            self.q.put(("error", str(exc)))
        except Exception as exc:                      # noqa: BLE001
            self.q.put(("error", f"意外错误：{exc}"))

    def _apply_restored(self, used: Path, sha: str) -> None:
        self.info(f"已用 {used.name} 还原，SHA256 {sha[:16]}…")
        messagebox.showinfo(TITLE, "还原完成。")
        self._reload_current()

    def _reload_current(self) -> None:
        """打完/还原后重新分析当前文件，刷新界面。"""
        a = self.current
        if a is None:
            self.auto_detect()
            return
        self.set_busy(True, "正在刷新状态…")
        threading.Thread(target=self._pick_worker, args=(a.install.dll,),
                         daemon=True).start()

    def on_open(self) -> None:
        a = self.current
        if a is None:
            return
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
    try:
        App().mainloop()
    except Exception:                       # noqa: BLE001
        import tempfile
        import traceback
        path = os.path.join(tempfile.gettempdir(), "WeChatAntiRecall-crash.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            traceback.print_exc(file=f)
        messagebox.showerror(TITLE, f"程序出错，日志已写入：\n{path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
