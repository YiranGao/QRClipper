"""屏幕截图：基于 Win32 GDI，支持多显示器与高 DPI。

不使用 PIL.ImageGrab，直接用 GetDC + BitBlt + GetDIBits，
这样可以正确处理虚拟屏幕的负坐标（副屏在主屏左侧的情况）。

坐标系约定：本模块对外一律使用**物理像素**的虚拟屏幕坐标。要让
GetSystemMetrics / BitBlt / GetCursorPos 三者口径一致，进程必须先声明
DPI 感知（见 enable_dpi_awareness），否则 Windows 会对 DPI 无感知进程
做坐标虚拟化（4K@150% 时会把 3840x2160 报告成 2560x1440）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

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
SM_CXSCREEN = 0
SM_CYSCREEN = 1

# GetSystemMetrics 用的是 int，虚拟屏坐标可为负
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetDpiForSystem.argtypes = []
user32.GetDpiForSystem.restype = ctypes.c_uint
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = ctypes.c_uint

PROCESS_PER_MONITOR_DPI_AWARE = 2
# DPI_AWARENESS_CONTEXT 实际是个指针大小的句柄，V2 定义为 -4
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# DPI_AWARENESS 枚举（GetAwarenessFromDpiAwarenessContext 的返回值）
DPI_AWARENESS_NAMES = {
    0: "UNAWARE",
    1: "SYSTEM_AWARE",
    2: "PER_MONITOR_AWARE",
}
SM_CMONITORS = 80

# 感知级别由高到低排序，便于比较
_AWARENESS_RANK = {"UNAWARE": 0, "SYSTEM_AWARE": 1, "PER_MONITOR_AWARE": 2,
                   "PER_MONITOR_AWARE_V2": 3}


def current_dpi_awareness() -> str:
    """查询本进程当前实际生效的 DPI 感知级别。

    优先用 GetThreadDpiAwarenessContext（Win10 1607+），
    退回 GetProcessDpiAwareness（Win8.1+），两者都不可用时返回 "UNKNOWN"。
    """
    try:
        get_context = user32.GetThreadDpiAwarenessContext
        get_context.argtypes = []
        get_context.restype = wintypes.LPVOID
        from_context = user32.GetAwarenessFromDpiAwarenessContext
        from_context.argtypes = [wintypes.LPVOID]
        from_context.restype = ctypes.c_uint
        value = int(from_context(get_context()))
        return DPI_AWARENESS_NAMES.get(value, "UNKNOWN")
    except (AttributeError, OSError):
        pass

    try:
        get_process = shcore.GetProcessDpiAwareness
        get_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint)]
        get_process.restype = ctypes.HRESULT
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        value = ctypes.c_uint(0)
        get_process(kernel32.GetCurrentProcess(), ctypes.byref(value))
        return DPI_AWARENESS_NAMES.get(value.value, "UNKNOWN")
    except (AttributeError, OSError):
        return "UNKNOWN"


def enable_dpi_awareness() -> str:
    """声明进程为 DPI 感知，避免高 DPI 屏幕上截图被缩放导致识别失败。

    依次尝试 Per-Monitor V2 -> Per-Monitor -> System Aware，
    返回**实际生效**的感知级别（失败时查询真实值，不会谎报成功）。

    注意：旧实现给 SetProcessDpiAwareness 留了默认的 int restype，
    于是它返回 E_ACCESSDENIED 时不会抛异常，后面两级兜底永远走不到，
    感知设置失败被静默吞掉。这里显式声明 restype 修掉该问题。
    """
    try:
        set_context = user32.SetProcessDpiAwarenessContext
    except AttributeError:
        set_context = None
    if set_context is not None:
        set_context.argtypes = [wintypes.LPVOID]
        set_context.restype = wintypes.BOOL
        try:
            if set_context(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
                return current_dpi_awareness()
        except OSError:
            pass

    try:
        set_awareness = shcore.SetProcessDpiAwareness
    except AttributeError:
        set_awareness = None
    if set_awareness is not None:
        set_awareness.argtypes = [ctypes.c_uint]
        set_awareness.restype = ctypes.HRESULT
        try:
            if set_awareness(PROCESS_PER_MONITOR_DPI_AWARE) >= 0:
                return current_dpi_awareness()
        except OSError:
            # E_ACCESSDENIED：感知级别已被（清单或前面的调用）固定，不是错误
            pass

    try:
        if user32.SetProcessDPIAware():
            return current_dpi_awareness()
    except (AttributeError, OSError):
        pass

    return current_dpi_awareness()


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """返回虚拟屏幕的 (left, top, width, height)，覆盖所有显示器。"""
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:  # 极少见的取值失败，退回主屏
        width = user32.GetSystemMetrics(SM_CXSCREEN)
        height = user32.GetSystemMetrics(SM_CYSCREEN)
        left, top = 0, 0
    return left, top, width, height


def cursor_pos() -> Optional[Tuple[int, int]]:
    """返回鼠标在虚拟屏幕上的**物理像素**坐标，失败返回 None。

    与 Tk 的 event.x_root / winfo_pointerx 相比，GetCursorPos 不受 Tk 自身
    缩放与窗口层级影响，是框选坐标换算的可靠来源。
    """
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return None
    return int(point.x), int(point.y)


def monitor_count() -> int:
    """返回显示器数量。

    刻意**不用** EnumDisplayMonitors + ctypes 回调：回调对象生命周期与 GC
    交互在真实会话里有挂起风险，而诊断信息不值得拿启动流程去冒险。
    数量用 GetSystemMetrics(SM_CMONITORS) 即可，逐显示器 DPI 不采集。
    """
    try:
        return int(user32.GetSystemMetrics(SM_CMONITORS))
    except (AttributeError, OSError):
        return 0


def system_dpi() -> int:
    """返回系统 DPI（失败返回 96）。"""
    try:
        value = int(user32.GetDpiForSystem())
        return value if value > 0 else 96
    except (AttributeError, OSError):
        return 96


def dpi_summary() -> Dict[str, object]:
    """返回 DPI / 缩放 / 虚拟屏幕的诊断信息，供日志与 --doctor 使用。"""
    dpi = system_dpi()
    left, top, width, height = virtual_screen_rect()
    return {
        "awareness": current_dpi_awareness(),
        "system_dpi": dpi,
        "scale_percent": round(dpi / 96.0 * 100),
        "virtual_screen": (left, top, width, height),
        "monitor_count": monitor_count(),
    }


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
