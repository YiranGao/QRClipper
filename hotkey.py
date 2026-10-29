"""全局热键：基于 Win32 RegisterHotKey，无需管理员权限，也不做全局键盘钩子。

在一个独立线程中创建 message-only 窗口并运行消息循环，收到 WM_HOTKEY 后回调。
快捷键变更时可热重载，不需要重启程序。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("QRClipper")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_RELOAD = 0x8001
HWND_MESSAGE = -3

# 热键窗口的唯一标题：便于自动化测试定位，也可用于排查冲突
HOTKEY_WINDOW_TITLE = "QRClipperHotkeyWindow"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

MOD_MAP = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

KEY_MAP = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "printscreen": 0x2C,
    "scrolllock": 0x91,
    "numlock": 0x90,
    "capslock": 0x14,
    "semicolon": 0xBA,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "backslash": 0xDC,
    "bracketleft": 0xDB,
    "bracketright": 0xDD,
    "minus": 0xBD,
    "equal": 0xBB,
    "grave": 0xC0,
    "apostrophe": 0xDE,
}

# 符号键：虚拟键码不等于 ASCII 码（';' 是 0xBA 而不是 0x3B），必须显式映射
OEM_KEY_CODES = {
    ";": 0xBA, ":": 0xBA,
    "=": 0xBB, "+": 0xBB,
    ",": 0xBC, "<": 0xBC,
    "-": 0xBD, "_": 0xBD,
    ".": 0xBE, ">": 0xBE,
    "/": 0xBF, "?": 0xBF,
    "`": 0xC0, "~": 0xC0,
    "[": 0xDB, "{": 0xDB,
    "\\": 0xDC, "|": 0xDC,
    "]": 0xDD, "}": 0xDD,
    "'": 0xDE, '"': 0xDE,
}

# 虚拟键码 -> 界面显示文本（优先于 KEY_MAP 反查，保证符号显示为符号本身）
VK_DISPLAY_NAMES = {
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
    0x20: "Space", 0x0D: "Enter", 0x09: "Tab", 0x1B: "Esc",
    0x08: "Backspace", 0x2E: "Delete", 0x2D: "Insert",
    0x24: "Home", 0x23: "End", 0x21: "PageUp", 0x22: "PageDown",
    0x26: "Up", 0x28: "Down", 0x25: "Left", 0x27: "Right",
    0x2C: "PrintScreen", 0x14: "CapsLock",
}


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def parse_hotkey(text: str) -> Optional[Tuple[int, int]]:
    """把 'ctrl+alt+q' 解析为 (修饰键掩码, 虚拟键码)，失败返回 None。"""
    if not text:
        return None
    parts = [p.strip().lower() for p in text.replace("-", "+").split("+") if p.strip()]
    if not parts:
        return None

    mods = 0
    key: Optional[int] = None
    for part in parts:
        if part in MOD_MAP:
            mods |= MOD_MAP[part]
            continue
        if part.startswith("f") and part[1:].isdigit():
            index = int(part[1:])
            if 1 <= index <= 24:
                key = 0x70 + index - 1
                continue
        if part in KEY_MAP:
            key = KEY_MAP[part]
            continue
        if len(part) == 1:  # 字母/数字走 ASCII；符号键必须用 OEM 映射
            key = OEM_KEY_CODES.get(part, ord(part.upper()))
            continue
        return None

    if key is None:
        return None
    return mods, key


def format_hotkey(text: str) -> str:
    """把 'ctrl+alt+q' 规范化成 'Ctrl+Alt+Q'，用于界面显示。"""
    parsed = parse_hotkey(text)
    if parsed is None:
        return text
    mods, key = parsed
    order = [(MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")]
    pieces = [name for mask, name in order if mods & mask]
    display = VK_DISPLAY_NAMES.get(key)
    if display:
        pieces.append(display)
    elif 0x70 <= key <= 0x87:
        pieces.append(f"F{key - 0x70 + 1}")
    elif 0x60 <= key <= 0x69:
        pieces.append(f"Num{key - 0x60}")
    else:
        pieces.append(chr(key))
    return "+".join(pieces)


class HotKeyManager(threading.Thread):
    """管理一组全局热键，触发时回调 on_trigger(action_id)。"""

    def __init__(self, on_trigger: Callable[[str], None]) -> None:
        super().__init__(name="HotKeyThread", daemon=True)
        self._on_trigger = on_trigger
        self._bindings: Dict[str, str] = {}  # action_id -> hotkey 字符串
        self._registered: Dict[int, str] = {}  # hotkey id -> action_id
        self._active: Dict[str, str] = {}  # action_id -> 实际注册成功的 hotkey
        self._next_id = 0x1000
        self._hwnd: Optional[int] = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._running = False

    # ---------- 对外接口 ----------
    def update_bindings(self, bindings: Dict[str, str]) -> None:
        """更新快捷键绑定并热重载。"""
        self._bindings = dict(bindings)
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP_RELOAD, 0, 0)

    def registered_actions(self) -> List[str]:
        """返回实际注册成功的 action_id 列表（用于错误提示）。"""
        return list(self._registered.values())

    def registered_bindings(self) -> Dict[str, str]:
        """返回实际注册成功的 {action_id: hotkey}（用于自检与诊断）。"""
        return dict(self._active)

    def wait_ready(self, timeout: float = 3.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        """停止消息循环并结束线程。"""
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # ---------- 内部实现 ----------
    def run(self) -> None:  # pragma: no cover - 需要真实消息循环
        self._hwnd = user32.CreateWindowExW(
            0,
            "STATIC",
            HOTKEY_WINDOW_TITLE,
            0,
            0,
            0,
            0,
            0,
            wintypes.HWND(HWND_MESSAGE),
            None,
            None,
            None,
        )
        self._thread_id = kernel32.GetCurrentThreadId()
        self._running = True
        self._ready.set()
        self._reload()

        msg = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                break
            if msg.message == WM_HOTKEY:
                action_id = self._registered.get(int(msg.wParam))
                if action_id:
                    try:
                        self._on_trigger(action_id)
                    except Exception as exc:
                        logger.exception("热键 %s 回调异常：%s", action_id, exc)
            elif msg.message == WM_APP_RELOAD:
                self._reload()

        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
        self._running = False

    def _reload(self) -> None:
        """注销全部热键并按当前绑定重新注册。"""
        if not self._hwnd:
            return
        for hotkey_id in list(self._registered):
            user32.UnregisterHotKey(self._hwnd, hotkey_id)
        self._registered.clear()
        self._active.clear()
        self._next_id = 0x1000

        for action_id, hotkey in self._bindings.items():
            parsed = parse_hotkey(hotkey)
            if parsed is None:
                logger.warning("快捷键 %s 无法解析：%s", action_id, hotkey)
                continue
            mods, key = parsed
            hotkey_id = self._next_id
            self._next_id += 1
            if user32.RegisterHotKey(self._hwnd, hotkey_id, mods | MOD_NOREPEAT, key):
                self._registered[hotkey_id] = action_id
                self._active[action_id] = hotkey
                logger.info("已注册热键 %s -> %s", action_id, format_hotkey(hotkey))
            else:
                logger.warning(
                    "注册热键失败 %s -> %s（可能被其他程序占用）", action_id, format_hotkey(hotkey)
                )
