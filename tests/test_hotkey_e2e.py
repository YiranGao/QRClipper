"""热键链路端到端测试（针对已运行的 QRClipper.exe）。

测试步骤：
1. 在屏幕上显示一个二维码窗口；
2. 向程序的热键窗口投递 WM_HOTKEY（等价于用户按下 Ctrl+Alt+Q）；
3. 检查剪贴板是否被写入该二维码内容。

注意：自动化环境里 SendInput 常被系统拒绝（返回 0），
因此用 PostMessage(WM_HOTKEY) 等价验证消息 -> 回调 -> 截图 -> 识别 -> 剪贴板 全链路。

    python tests/test_hotkey_e2e.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clipboard  # noqa: E402
from hotkey import HOTKEY_WINDOW_TITLE, WM_HOTKEY  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402
from qrcode import QRCode  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

HOTKEY_ID_FULL = 0x1000  # 第一个注册的绑定（全屏识别）
HOTKEY_ID_SNIP = 0x1001


def main() -> int:
    hwnd = user32.FindWindowW(None, HOTKEY_WINDOW_TITLE)
    if not hwnd:
        print("未找到运行中的 QRClipper（请先启动 dist\\QRClipper.exe）")
        return 2
    print(f"已定位热键窗口: 0x{hwnd:x}")

    content = "HotKey-E2E-" + time.strftime("%H%M%S")

    root = tk.Tk()
    root.title("QRClipper 热键测试")
    root.attributes("-topmost", True)
    root.geometry("+150+150")
    qr = QRCode(box_size=10, border=4)
    qr.add_data(content)
    qr.make(fit=True)
    image: Image.Image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    photo = ImageTk.PhotoImage(image)
    label = tk.Label(root, image=photo)
    label.image = photo
    label.pack()
    root.update()

    clipboard.set_text("__EMPTY__")
    time.sleep(0.3)

    print("投递 WM_HOTKEY（等价于按下 Ctrl+Alt+Q）…")
    user32.PostMessageW(hwnd, WM_HOTKEY, HOTKEY_ID_FULL, 0)

    deadline = time.time() + 8
    value = ""
    while time.time() < deadline:
        root.update()
        value = clipboard.get_text() or ""
        if value == content:
            break
        time.sleep(0.3)

    root.destroy()
    ok = value == content
    print(f"期望: {content}")
    print(f"剪贴板: {value[:80]}")
    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
