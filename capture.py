"""屏幕截图：基于 Win32 GDI，支持多显示器与高 DPI。

不使用 PIL.ImageGrab，直接用 GetDC + BitBlt + GetDIBits，
这样可以正确处理虚拟屏幕的负坐标（副屏在主屏左侧的情况）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Tuple

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# GetSystemMetrics 用的是 int，虚拟屏坐标可为负
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable_dpi_awareness() -> None:
    """声明进程为 DPI 感知，避免高 DPI 屏幕上截图被缩放导致识别失败。"""
    try:
        shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """返回虚拟屏幕的 (left, top, width, height)，覆盖所有显示器。"""
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:  # 极少见的取值失败，退回主屏
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        left, top = 0, 0
    return left, top, width, height


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def grab(region: Tuple[int, int, int, int] | None = None) -> Image.Image:
    """截取屏幕区域，region 为 (left, top, right, bottom) 虚拟坐标。

    region 为 None 时截取整个虚拟屏幕（所有显示器）。
    """
    if region is None:
        left, top, width, height = virtual_screen_rect()
        right, bottom = left + width, top + height
    else:
        left, top, right, bottom = region

    width = max(1, right - left)
    height = max(1, bottom - top)

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise OSError("无法获取屏幕 DC")
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    if not mem_dc or not bitmap:
        raise OSError("无法创建兼容 DC 或位图")

    try:
        gdi32.SelectObject(mem_dc, bitmap)
        if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY):
            raise OSError("BitBlt 截图失败")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = height  # 正值：自下而上
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = gdi32.GetDIBits(
            mem_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
        )
        if lines != height:
            raise OSError("GetDIBits 读取位图数据失败")
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

    image = Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
    # 位图是自下而上存储的，需要翻转
    return image.transpose(Image.FLIP_TOP_BOTTOM)


def grab_all() -> Image.Image:
    """截取所有显示器组成的完整虚拟屏幕。"""
    return grab(None)


def normalize_region(x1: int, y1: int, x2: int, y2: int) -> Tuple[int, int, int, int]:
    """把任意两点整理成 (left, top, right, bottom)，并保证不超出虚拟屏幕。"""
    left, top = min(x1, x2), min(y1, y2)
    right, bottom = max(x1, x2), max(y1, y2)
    if right - left < 4 or bottom - top < 4:  # 太小的选区没有意义
        raise ValueError("选区过小")
    return left, top, right, bottom
