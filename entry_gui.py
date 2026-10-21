"""PyInstaller 入口：图形界面版。

支持 `--selftest`：创建窗口、等自动检测完成、把界面状态写到文件后退出，
用于在无人值守/锁屏环境下验证打包产物是否正常。
"""

import os
import sys

from wxar import gui


def selftest() -> int:
    out = os.environ.get("WXAR_SELFTEST_OUT", "wxar_selftest.txt")
    app = gui.App()

    def report() -> None:
        a = app.current
        lines = [
            f"busy={app.busy}",
            f"status={app.status_var.get()}",
            f"combo={app.combo.get()}",
            f"version={app.info_vars['version'].get()}",
            f"size={app.info_vars['size'].get()}",
            f"state={app.info_vars['state'].get()}",
            f"patch={app.info_vars['patch'].get()}",
            f"btn_patch={app.btn_patch['state']}",
            f"btn_restore={app.btn_restore['state']}",
            f"dll={a.install.dll if a else ''}",
        ]
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        app.destroy()

    app.after(3000, report)
    app.mainloop()
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(gui.main())
