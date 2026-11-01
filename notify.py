"""多通道通知：保证「图标/托盘没起来时也一定弹得出来」。

本次故障里托盘根本没创建成功，如果通知只走 pystray 的 `icon.notify()`，
那用户就永远看不到任何反馈——分不清"程序挂了"和"没识别到"。

------------------------------------------------------------------
通道顺序为什么是「Tk 浮窗优先」而不是「pystray 优先」
------------------------------------------------------------------
最初按"原生优先"写的顺序是 pystray -> Shell_NotifyIcon -> Tk 浮窗。
在真机上实测后发现这条路不成立，两个原生通道都是**静默失败**：

  * 打包成 QRClipper.exe 后，`icon.notify()` 不抛异常、我们自己的
    `Shell_NotifyIcon(NIM_ADD/NIM_MODIFY)` 也返回 TRUE，但屏幕上
    既没有出现气泡，也没有任何新窗口（EnumWindows 全程零增量），
    右下角像素哈希纹丝不动 —— 连着 10 次、2 个通道、多个时间点都一样。
  * 同一份代码用 python.exe 跑，同样的调用却能正常弹出气泡。

也就是说：原生通道"返回成功"完全不代表用户看得见，而且没有任何
可编程的确认手段。继续把它放在第一位，就等于把用户唯一的反馈寄托在
一个会静默丢弃的通道上——那正是用户抱怨"现在什么都没有"的成因。

所以最终顺序改成**先把能保证的通道用掉**：

1. Tk 浮窗（`ui.ToastWindow`）——自己画的置顶窗口，不经过系统通知策略，
   打包后实测可见；这是"一定弹得出来"的保证。
2. 浮窗不可用（没有 Tk 根窗口，例如 --scan 之类的命令行场景）时才退到
   pystray `icon.notify()`（图标存在且可见）。
3. 再退到 Win32 `Shell_NotifyIcon` 自建临时通知区图标气泡。

所有 Win32 调用都做防御性处理：结构体或环境不配合时只会返回 False，
不会抛异常，也不会阻断调用方。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --- Shell_NotifyIcon 常量 ---
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_NONE = 0x00000000
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003
IDI_ERROR = 32513
IDI_WARNING = 32515
IDI_INFORMATION = 32516
WS_POPUP = 0x80000000

# 气泡自动消失后把临时图标删掉，避免通知区留下一个多余图标
_ICON_LIFETIME_SECONDS = 10.0
_TIP_MAX = 128
_INFO_MAX = 256
_TITLE_MAX = 64


class NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW 的 V2 布局（到 dwInfoFlags 为止）。

    cbSize 直接取本结构的 sizeof，Shell 会按 V2 解析；V3/V4 才有的
    guidItem / hBalloonIcon 不参与，避免结构体版本不匹配。
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * _TIP_MAX),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * _INFO_MAX),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * _TITLE_MAX),
        ("dwInfoFlags", wintypes.DWORD),
    ]


user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.LoadIconW.restype = wintypes.HICON
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class Notifier:
    """按 三级降级 顺序发送通知，返回实际使用的通道名。"""

    LEVEL_INFO = "info"
    LEVEL_WARN = "warn"
    LEVEL_ERROR = "error"

    _LEVEL_FLAGS = {
        "info": NIIF_INFO,
        "warn": NIIF_WARNING,
        "error": NIIF_ERROR,
    }
    _LEVEL_ICONS = {
        "info": IDI_INFORMATION,
        "warn": IDI_WARNING,
        "error": IDI_ERROR,
    }

    def __init__(
        self,
        logger: logging.Logger,
        icon_getter: Callable[[], object],
        toast: Callable[..., None],
        enabled: Callable[[], bool],
    ) -> None:
        """
        Args:
            logger: 日志器。
            icon_getter: 返回 pystray 图标（没有则 None）。
            toast: 浮窗回调 (title, message, duration_ms) -> None。
            enabled: 返回用户是否开启了通知（config 的 notify 开关）。
        """
        self._logger = logger
        self._icon_getter = icon_getter
        self._toast = toast
        self._enabled = enabled
        self._hwnd: Optional[int] = None
        self._icon_handle: Optional[int] = None
        self._icon_added = False
        self._delete_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    # ---------- 对外 ----------
    def last_channel(self) -> str:
        return self._last_channel

    def send(
        self,
        title: str,
        message: str,
        force: bool = False,
        level: str = LEVEL_INFO,
        duration_ms: int = 4000,
    ) -> str:
        """发送通知，返回通道名："toast" / "pystray" / "systray" / "skipped" / "none"。

        Args:
            force: 为 True 时忽略 notify 开关。失败/异常类通知必须 force，
                否则静默失败和程序崩溃在用户眼里完全没有区别。
            duration_ms: 浮窗通道的停留时长。
        """
        if not force and not self._enabled():
            self._logger.info("通知已关闭（notify=false），跳过：%s", title)
            self._last_channel = "skipped"
            return "skipped"

        # 通道 1：Tk 浮窗。唯一能"确认看得见"的通道，必须放在最前面。
        if self._try_toast(title, message, duration_ms):
            self._last_channel = "toast"
            self._logger.info("通知已发送（通道=Tk 浮窗）：%s", title)
            return "toast"

        if self._try_pystray(title, message):
            self._last_channel = "pystray"
            self._logger.info("通知已发送（通道=pystray）：%s", title)
            return "pystray"

        if self._try_balloon(title, message, level):
            self._last_channel = "systray"
            self._logger.info("通知已发送（通道=Shell_NotifyIcon 气泡）：%s", title)
            return "systray"

        self._last_channel = "none"
        self._logger.error("所有通知通道都失败，用户不会看到提示：%s / %s", title, message)
        return "none"

    _last_channel = "none"

    def close(self) -> None:
        """退出时清掉临时通知区图标与隐藏窗口。"""
        if self._delete_timer is not None:
            self._delete_timer.cancel()
            self._delete_timer = None
        self._remove_icon()
        if self._hwnd:
            try:
                user32.DestroyWindow(wintypes.HWND(self._hwnd))
            except Exception:
                pass
            self._hwnd = None

    # ---------- 通道 1：Tk 浮窗 ----------
    def _try_toast(self, title: str, message: str, duration_ms: int = 4000) -> bool:
        try:
            self._toast(title, message, duration_ms)
            return True
        except Exception as exc:
            self._logger.warning("Tk 浮窗提示失败（%s），降级到 pystray", exc)
            return False

    # ---------- 通道 2：pystray ----------
    def _try_pystray(self, title: str, message: str) -> bool:
        icon = self._icon_getter()
        if icon is None:
            self._logger.debug("pystray 图标不存在，降级到 Shell_NotifyIcon")
            return False
        if not getattr(icon, "visible", False):
            self._logger.debug("pystray 图标不可见（托盘未就绪），降级到 Shell_NotifyIcon")
            return False
        try:
            icon.notify(message, title)
            return True
        except Exception as exc:
            self._logger.warning("pystray 通知失败（%s），降级到 Shell_NotifyIcon", exc)
            return False

    # ---------- 通道 2：Shell_NotifyIcon ----------
    def _ensure_icon(self) -> bool:
        """确保临时通知区图标已添加（只添加一次）。"""
        if self._icon_added:
            return True
        try:
            if not self._hwnd:
                # 普通隐藏窗口（WS_POPUP 且从不 ShowWindow），比 HWND_MESSAGE
                # 更兼容通知区气泡。
                self._hwnd = int(user32.CreateWindowExW(
                    0, "STATIC", "QRClipperNotify", WS_POPUP,
                    0, 0, 0, 0, None, None, None, None,
                ) or 0)
                if not self._hwnd:
                    self._logger.warning("Shell_NotifyIcon：创建通知窗口失败")
                    return False
            if not self._icon_handle:
                self._icon_handle = int(user32.LoadIconW(
                    None, ctypes.c_void_p(IDI_INFORMATION)) or 0)
                if not self._icon_handle:
                    self._logger.warning("Shell_NotifyIcon：加载系统图标失败")
                    return False

            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = wintypes.HWND(self._hwnd)
            data.uID = 1
            # 不设 NIF_MESSAGE：这个临时窗口不处理回调消息，uCallbackMessage
            # 留 0 会让 Shell 往 WM_NULL 投递，反而可能被判定为无效注册。
            data.uFlags = NIF_ICON | NIF_TIP
            data.hIcon = wintypes.HICON(self._icon_handle)
            data.szTip = "QRClipper"
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                self._logger.warning(
                    "Shell_NotifyIcon：NIM_ADD 失败（error=%d），降级到 Tk 浮窗",
                    ctypes.get_last_error(),
                )
                return False
            self._icon_added = True
            return True
        except Exception as exc:
            self._logger.warning("Shell_NotifyIcon 初始化异常（%s），降级到 Tk 浮窗", exc)
            return False

    def _try_balloon(self, title: str, message: str, level: str) -> bool:
        with self._lock:
            if not self._ensure_icon():
                return False
            try:
                data = NOTIFYICONDATAW()
                data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                data.hWnd = wintypes.HWND(self._hwnd)
                data.uID = 1
                data.uFlags = NIF_INFO
                data.szInfo = message[:_INFO_MAX - 1]
                data.szInfoTitle = title[:_TITLE_MAX - 1]
                data.dwInfoFlags = self._LEVEL_FLAGS.get(level, NIIF_INFO)
                data.uTimeout = 8000
                if not shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
                    self._logger.warning(
                        "Shell_NotifyIcon：NIM_MODIFY 失败（error=%d），降级到 Tk 浮窗",
                        ctypes.get_last_error(),
                    )
                    return False
                self._schedule_cleanup()
                return True
            except Exception as exc:
                self._logger.warning("Shell_NotifyIcon 发送异常（%s），降级到 Tk 浮窗", exc)
                return False

    def _schedule_cleanup(self) -> None:
        if self._delete_timer is not None:
            self._delete_timer.cancel()
        timer = threading.Timer(_ICON_LIFETIME_SECONDS, self._remove_icon)
        timer.daemon = True
        timer.start()
        self._delete_timer = timer

    def _remove_icon(self) -> None:
        with self._lock:
            if not self._icon_added:
                return
            try:
                data = NOTIFYICONDATAW()
                data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                data.hWnd = wintypes.HWND(self._hwnd)
                data.uID = 1
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            except Exception as exc:
                self._logger.debug("删除临时通知区图标失败：%s", exc)
            finally:
                self._icon_added = False

    # ---------- 通道 3：Shell_NotifyIcon ----------


__all__ = ["Notifier", "NOTIFYICONDATAW"]
