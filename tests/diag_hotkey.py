"""热键诊断：同一进程内注册全局热键并用 SendInput 注入，验证能否收到 WM_HOTKEY。

    python tests/diag_hotkey.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotkey import HWND_MESSAGE, MOD_ALT, MOD_CONTROL, WM_HOTKEY, HotKeyManager  # noqa: E402

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_MENU = 0x11, 0x12


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("ki", KEYBDINPUT)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def send(vk: int, up: bool = False) -> int:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, ctypes.pointer(ctypes.c_ulong(0)))
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


hits = []
manager = HotKeyManager(lambda action: hits.append(action))
manager.update_bindings({"diag": "ctrl+alt+k"})
manager.start()
manager.wait_ready()
time.sleep(0.3)
print("已注册:", manager.registered_actions())

sent = []
sent.append(send(VK_CONTROL))
sent.append(send(VK_MENU))
time.sleep(0.05)
sent.append(send(ord("K")))
time.sleep(0.05)
sent.append(send(ord("K"), True))
time.sleep(0.05)
sent.append(send(VK_MENU, True))
sent.append(send(VK_CONTROL, True))
print("SendInput 返回值:", sent, "(每项应为 1)")

for _ in range(20):
    if hits:
        break
    time.sleep(0.2)

print("收到热键回调:", hits)
manager.stop()
print("结论:", "PASS 注入可触发热键" if hits else "FAIL 注入未触发热键")
sys.exit(0 if hits else 1)
