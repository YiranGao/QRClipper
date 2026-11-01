"""诊断：分别验证两条通知通道在真实桌面上是否肉眼可见。

  1) Tk 浮窗（ToastWindow）—— 自绘，不依赖系统通知设置
  2) pystray 气泡（Shell_NotifyIcon NIF_INFO）—— 受「专注助手 / 通知设置」管控

每个通道弹完立刻抓右下角，把图存到 tests/_diag_*.png，由人眼确认。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capture  # noqa: E402


def shot(tag: str) -> str:
    left, top, width, height = capture.virtual_screen_rect()
    region = (left + width - 1100, top + height - 560, left + width, top + height)
    path = os.path.abspath(os.path.join("tests", f"_diag_{tag}.png"))
    capture.grab(region).save(path)
    return path


def test_toast() -> str:
    import ui

    root = tk.Tk()
    root.withdraw()
    ui.ToastWindow(root, "未识别到二维码", "这是 Tk 浮窗通道的测试文案。")
    root.update()
    time.sleep(0.4)
    root.update()
    path = shot("toast")
    root.destroy()
    return path


def test_pystray() -> str:
    import pystray
    from PIL import Image

    image = Image.new("RGB", (64, 64), (30, 30, 30))
    holder: dict = {}

    def _run(icon):
        holder["err"] = None
        try:
            icon.notify("这是 pystray 气泡通道的测试文案。", "未识别到二维码")
        except Exception as exc:  # noqa: BLE001
            holder["err"] = repr(exc)
        holder["done"] = True

    icon = pystray.Icon("qrdiag", image, "QRDiag")
    thread = threading.Thread(target=icon.run, args=(_run,), daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not holder.get("done") and time.time() < deadline:
        time.sleep(0.1)
    time.sleep(0.6)
    path = shot("pystray")
    err = holder.get("err")
    icon.stop()
    if err:
        print("    pystray.notify 异常：", err)
    elif not holder.get("done"):
        print("    pystray.notify 超时未返回（图标可能没建成）")
    else:
        print("    pystray.notify 返回成功（无异常）")
    return path


if __name__ == "__main__":
    capture.enable_dpi_awareness()
    print("ToastWindow ->", test_toast())
    print("pystray     ->", test_pystray())
