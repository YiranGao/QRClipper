"""图形界面端到端冒烟测试（需要用户桌面会话）。

验证内容：
1. 全局热键是否注册成功；
2. 托盘图标是否成功创建；
3. 真实屏幕上显示二维码 -> 全屏识别 -> 写入剪贴板 的完整链路；
4. 框选遮罩能否正常创建与关闭。

    python tests/test_gui.py
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clipboard  # noqa: E402
import qrclip  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402
from qrcode import QRCode  # noqa: E402

CONTENT = "QRClipper-GUI-E2E-" + time.strftime("%H%M%S")
results: dict = {}


def build_qr_image(text: str, box_size: int = 10):
    qr = QRCode(box_size=box_size, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def show_qr_window(root: tk.Tk, photo: ImageTk.PhotoImage) -> tk.Toplevel:
    win = tk.Toplevel(root)
    win.title("QRClipper 测试二维码")
    win.attributes("-topmost", True)
    win.geometry("+200+200")
    label = tk.Label(win, image=photo)
    label.image = photo  # 保持引用
    label.pack()
    win.update()
    return win


def main() -> int:
    app = qrclip.App(show_tray=True)
    root = app.root
    root.title("QRClipper GUI Test")

    qr_window: list = []

    def step_show_qr():
        image = build_qr_image(CONTENT)
        photo = ImageTk.PhotoImage(image)
        qr_window.append(show_qr_window(root, photo))
        root.after(700, step_scan)

    def step_scan():
        # 清空剪贴板，确保后面读到的是本次识别结果
        clipboard.set_text("__EMPTY__")
        app.scan_full()
        root.after(2500, step_check_clipboard)

    def step_check_clipboard():
        value = clipboard.get_text() or ""
        results["clipboard_match"] = value == CONTENT
        results["clipboard_value"] = value[:60]

        # 热键注册情况
        actions = set(app.hotkeys.registered_actions())
        results["hotkeys"] = sorted(actions)
        results["hotkeys_ok"] = {"full", "snip"} <= actions

        # 托盘图标
        results["tray_created"] = app.icon is not None
        if app.icon is not None:
            try:
                results["tray_visible"] = bool(app.icon.visible)
            except Exception:
                results["tray_visible"] = None

        if qr_window:
            try:
                qr_window[0].destroy()
            except tk.TclError:
                pass
        root.after(200, step_snipper)

    def step_snipper():
        try:
            app.begin_snip()
            snipper = app._snipper
            results["snipper_created"] = snipper is not None
            if snipper is not None:
                # 直接结束框选（传入一个有效区域，走完整回调不识别也没关系）
                snipper._finish((10, 10, 300, 300))
                results["snipper_closed"] = app._snipper is None
        except Exception as exc:
            results["snipper_error"] = repr(exc)
        root.after(500, step_done)

    def step_done():
        print("---- GUI 冒烟测试结果 ----")
        for key, value in results.items():
            print(f"{key}: {value}")
        passed = bool(results.get("clipboard_match")) and bool(results.get("hotkeys_ok"))
        print("结果:", "PASS" if passed else "FAIL")
        app.quit()
        sys.exit(0 if passed else 1)

    root.after(1000, step_show_qr)
    app.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
