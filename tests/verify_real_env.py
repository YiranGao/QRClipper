"""真实环境验收工具（需要可交互的桌面会话，不参与 unittest）。

为什么需要它：本项目两次「假通过」的教训都是——只读日志不看屏幕。
`Shell_NotifyIcon` 返回 TRUE、`icon.notify()` 不抛异常，都不等于用户看得见。
所以这里每一步都在真实桌面上**截图留证**，并同时对比日志与剪贴板。

会话限制说明：不能用 SendInput 注入按键，因此直接向热键窗口
`PostMessageW(WM_HOTKEY)`。RegisterHotKey 之后的 WM_HOTKEY 走的是同一个
WndProc、同一个 register_id，链路与真人按键一致。

用法：
    python tests/verify_real_env.py hwnd                  # 热键窗口句柄 + 日志尾部
    python tests/verify_real_env.py trigger full          # 触发全屏识别并连续截图
    python tests/verify_real_env.py trigger snip          # 触发框选识别（会弹遮罩）
    python tests/verify_real_env.py qr-on-screen 25       # 屏幕上显示二维码 25 秒
    python tests/verify_real_env.py clipboard             # 打印当前剪贴板文本
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk
from ctypes import wintypes
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capture  # noqa: E402
import clipboard  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowW.restype = ctypes.c_void_p
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PostMessageW.restype = wintypes.BOOL

WM_HOTKEY = 0x0312
TITLE = "QRClipperHotkeyWindow"
IDS = {"full": 0x1000, "snip": 0x1001}
LOG = os.path.join(os.environ.get("APPDATA", ""), "QRClipper", "qrclip.log")
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))


def log_size() -> int:
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def log_since(offset: int) -> str:
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read().strip()
    except OSError:
        return ""


def shot(tag: str) -> str:
    """抓右下角一块（通知区），文件名以 _ 开头，已被 .gitignore 忽略。"""
    left, top, width, height = capture.virtual_screen_rect()
    region = (left + width - 1000, top + height - 460, left + width, top + height)
    path = os.path.join(SHOT_DIR, f"_shot_{tag}.png")
    capture.grab(region).save(path)
    return path


def hotkey_window() -> Optional[int]:
    handle = user32.FindWindowW(None, TITLE)
    return int(handle) if handle else None


def cmd_hwnd() -> int:
    handle = hotkey_window()
    print(f"热键窗口「{TITLE}」= {handle if handle else 'NULL'}")
    if not handle:
        print("FAIL 程序核心没起来（FindWindowW 返回 NULL）")
        return 2
    print("OK")
    tail = log_since(max(0, log_size() - 4000))
    print("--- 日志尾部 ---")
    print(tail or "（空）")
    return 0


def cmd_trigger(which: str) -> int:
    handle = hotkey_window()
    print(f"[1] 热键窗口 = {handle if handle else 'NULL'}")
    if not handle:
        print("FAIL 没有热键窗口，无法触发")
        return 2

    before = log_size()
    hotkey_id = IDS[which]
    posted = bool(user32.PostMessageW(handle, WM_HOTKEY, hotkey_id, 0))
    print(f"[2] 投递 {which}(id=0x{hotkey_id:X}) = {posted}")

    # 全屏识别约 1.2~1.6s 出结果；截图节奏覆盖「通知刚出现」到「稳定显示」
    for index, wait in enumerate((1.2, 0.8, 0.8)):
        time.sleep(wait)
        print(f"    t+{index} {shot(f'{which}_{index}')}")

    print("[3] 本次新增日志：")
    print(log_since(before).replace("\n", "\n    ") or "    （无）")

    text = clipboard.get_text()
    print(f"[4] 剪贴板 = {text!r}")
    return 0


def cmd_qr_on_screen(seconds: str = "25") -> int:
    """在屏幕正中显示一张二维码，供真人/脚本触发识别。

    张望窗口置顶，脚本自身不会截图，只负责"把码摆在那儿"。
    """
    duration = float(seconds)
    try:
        import qrcode
    except Exception as exc:
        print(f"需要 qrcode 库：{exc}")
        return 2

    content = f"QRClipper-E2E-{time.strftime('%H%M%S')}"
    qr = qrcode.QRCode(version=None, box_size=10, border=3)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    root = tk.Tk()
    root.title("QRClipper E2E QR")
    root.attributes("-topmost", True)
    root.configure(bg="white")
    from PIL import ImageTk

    photo = ImageTk.PhotoImage(image.get_image())
    tk.Label(root, image=photo, bg="white").pack(padx=12, pady=12)
    tk.Label(root, text=content, bg="white", font=("Consolas", 12)).pack(pady=(0, 10))
    root.update_idletasks()
    width, height = root.winfo_width(), root.winfo_height()
    left, top, vw, vh = capture.virtual_screen_rect()
    root.geometry(f"+{left + (vw - width) // 2}+{top + (vh - height) // 2}")

    print(f"READY content={content}", flush=True)
    root.after(int(duration * 1000), root.destroy)
    root.mainloop()
    print("CLOSED", flush=True)
    return 0


def cmd_clipboard() -> int:
    text = clipboard.get_text()
    print(f"剪贴板 = {text!r}")
    return 0 if text else 1


def main(argv: List[str]) -> int:
    capture.enable_dpi_awareness()
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "hwnd":
        return cmd_hwnd()
    if command == "trigger":
        return cmd_trigger(rest[0] if rest else "full")
    if command == "qr-on-screen":
        return cmd_qr_on_screen(rest[0] if rest else "25")
    if command == "clipboard":
        return cmd_clipboard()
    print(f"未知命令：{command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
