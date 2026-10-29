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


def get_text() -> Optional[str]:
    """读取剪贴板文本（用于自检与调试）。"""
    try:
        if not user32.OpenClipboard(None):
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            locked = kernel32.GlobalLock(handle)
            if not locked:
                return None
            try:
                return ctypes.wstring_at(locked)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except OSError:
        return None
