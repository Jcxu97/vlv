"""Smoke: 创建 App、轮切四个 view、销毁;clean 退出码 0。

非 pytest 用例,独立运行(避免 pytest 在 CI 上拉起真实 Tk 主循环)。
用法: scripts\\run_smoke.bat
"""
from __future__ import annotations

import sys
import traceback

from bilibili_vision.gui import App


def main() -> int:
    app = App()
    try:
        app.update_idletasks()
        for key in ("flow", "api", "chat", "report"):
            try:
                app._show_view(key)
                app.update()
            except Exception:
                traceback.print_exc()
                return 2
        hub = getattr(app, "_local_chat_hub", None)
        if hub is not None:
            nb = getattr(hub, "_local_hub_nb", None)
            if nb is not None:
                for idx in range(nb.index("end")):
                    nb.select(idx)
                    app.update()
    finally:
        try:
            app.destroy()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
