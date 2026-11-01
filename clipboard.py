"""剪贴板写入：直接调用 Win32 API，不依赖第三方库。

写入 CF_UNICODETEXT（UTF-16LE），可被所有 Windows 程序粘贴。
带重试：剪贴板被其他程序短暂占用时重试若干次。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _write_once(text: str) -> bool:
    """单次尝试写入剪贴板，成功返回 True。"""
    encoded = text.encode("utf-16-le") + b"\x00\x00"  # 需以双字节 0 结尾
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not handle:
        return False

    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False

    try:
        ctypes.memmove(locked, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(handle)

    try:
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            # 成功后系统接管该内存块，不能再 GlobalFree
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                return False
            handle = None  # type: ignore[assignment]
        finally:
            user32.CloseClipboard()
    finally:
        if handle:
            kernel32.GlobalFree(handle)
    return True


def set_text(text: str, retries: int = 5, delay: float = 0.08) -> bool:
    """把文本写入系统剪贴板，失败自动重试。"""
    for attempt in range(retries):
        try:
            if _write_once(text):
                return True
        except OSError:
            pass
        if attempt < retries - 1:
            time.sleep(delay)
    return False


def _read_once() -> tuple[bool, Optional[str]]:
    """单次尝试读取剪贴板文本。

    Returns:
        (opened, text)：
        opened 为 False 表示连剪贴板都没打开（被其他程序占用，值得重试）；
        opened 为 True 而 text 为 None 表示剪贴板能打开但里面没有文本数据。
        调用方据此区分「暂时打不开」和「确实没内容」，避免无谓重试。
    """
    opened = False
    try:
        if not user32.OpenClipboard(None):
            return False, None
        opened = True

        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return True, None
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return True, None
        try:
            return True, ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)
    except OSError:
        return opened, None
    finally:
        if opened:
            user32.CloseClipboard()


def get_text(retries: int = 5, delay: float = 0.05) -> Optional[str]:
    """读取剪贴板文本（用于自检与调试），剪贴板暂时取不到内容时自动重试。

    与 set_text 对称地重试。除 OpenClipboard 失败（ERROR_ACCESS_DENIED，剪贴板
    被其他程序短暂占用）之外，**取不到文本也要重试**：实测（Win11 4K 环境，
    开启剪贴板历史）刚写完立刻读，OpenClipboard 会成功但 GetClipboardData 返回
    NULL，再读一次才拿到内容。旧实现在「能打开但没数据」时直接返回 None，
    导致写入后立刻回读 100% 失败（冒烟用例与 --selftest 都会误报）。

    重试上限内仍拿不到文本才返回 None，即剪贴板里确实没有文本。
    """
    for attempt in range(retries):
        _opened, text = _read_once()
        if text is not None:
            return text
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def verify_text(text: str, retries: int = 5, delay: float = 0.05) -> bool:
    """回读校验：确认剪贴板里确实是刚写入的内容。

    用于识别成功后确认「真的复制成功了」——剪贴板可能被剪贴板管理器、
    云同步或其他程序抢走，只凭 SetClipboardData 的返回值判断会误报成功。
    """
    return get_text(retries, delay) == text
