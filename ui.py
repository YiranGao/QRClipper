"""图形界面：框选遮罩、设置窗口、托盘图标。

框选遮罩覆盖整个虚拟屏幕（支持多显示器），拖拽出选区后回传虚拟坐标。
设置窗口可重新捕获快捷键组合并保存配置。
"""

from __future__ import annotations

import ctypes
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from capture import cursor_pos, virtual_screen_rect
from hotkey import VK_DISPLAY_NAMES

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetKeyState.argtypes = [ctypes.c_int]
_user32.GetKeyState.restype = ctypes.c_short

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# 修饰键的输出顺序（ctrl -> alt -> shift -> win），与界面显示保持一致
MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")

# Tk keysym -> 本程序使用的按键名
TK_KEYSYM_MAP = {
    "semicolon": ";",
    "colon": ":",
    "comma": ",",
    "less": "<",
    "period": ".",
    "greater": ">",
    "slash": "/",
    "question": "?",
    "backslash": "\\",
    "bar": "|",
    "bracketleft": "[",
    "braceleft": "{",
    "bracketright": "]",
    "braceright": "}",
    "minus": "-",
    "underscore": "_",
    "equal": "=",
    "plus": "+",
    "grave": "`",
    "asciitilde": "~",
    "apostrophe": "'",
    "quotedbl": '"',
    "space": "space",
    "Escape": "esc",
    "Return": "enter",
    "Tab": "tab",
    "BackSpace": "backspace",
    "Delete": "delete",
    "Insert": "insert",
    "Home": "home",
    "End": "end",
    "Prior": "pageup",
    "Next": "pagedown",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "Print": "printscreen",
}

MODIFIER_KEYSYMS = {
    "Control_L",
    "Control_R",
    "Alt_L",
    "Alt_R",
    "Shift_L",
    "Shift_R",
    "Super_L",
    "Super_R",
    "Meta_L",
    "Meta_R",
}


def keysym_to_key_name(keysym: str) -> str:
    """把 Tk 的 keysym 转换为配置里使用的按键名。"""
    if keysym in TK_KEYSYM_MAP:
        return TK_KEYSYM_MAP[keysym]
    if len(keysym) == 1:
        return keysym.lower()
    return keysym.lower()


# 需要排除的修饰键（含左右之分），轮询按键时不能把它们当成主键
_MODIFIER_VKS = {
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN,
    0xA0, 0xA1,  # VK_LSHIFT / VK_RSHIFT
    0xA2, 0xA3,  # VK_LCONTROL / VK_RCONTROL
    0xA4, 0xA5,  # VK_LMENU / VK_RMENU
}

_SPECIAL_VK_NAMES = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x1B: "esc",
    0x20: "space",
    0x21: "pageup",
    0x22: "pagedown",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2C: "printscreen",
    0x2D: "insert",
    0x2E: "delete",
    0x5D: "apps",
    0x90: "numlock",
    0x91: "scrolllock",
}


def vk_to_key_name(virtual_key: int) -> Optional[str]:
    """虚拟键码 -> 配置里使用的按键名（与 parse_hotkey 兼容）。"""
    vk = virtual_key
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:  # 数字与字母
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:  # F1 - F24
        return f"f{vk - 0x70 + 1}"
    if 0x60 <= vk <= 0x69:  # 小键盘 0-9
        return f"num{vk - 0x60}"
    special = _SPECIAL_VK_NAMES.get(vk)  # esc / space / enter ...
    if special:
        return special
    # 符号键（; = , - . / ` [ \ ] '）：虚拟键码与 ASCII 不同，查显示名反查表
    symbol = VK_DISPLAY_NAMES.get(vk)
    if symbol and len(symbol) == 1:
        return symbol
    return None


def pressed_key_vk() -> Optional[int]:
    """返回当前按下的主键（非修饰键）的虚拟键码，没有则返回 None。"""
    buffer = (ctypes.c_ubyte * 256)()
    if not _user32.GetKeyboardState(ctypes.byref(buffer)):
        return None
    for vk in range(0x08, 0xFF):
        if (buffer[vk] & 0x80) and vk not in _MODIFIER_VKS:
            return vk
    return None


def _key_down(virtual_key: int) -> bool:
    return bool(_user32.GetKeyState(virtual_key) & 0x8000)


def pressed_modifiers(event_state: int = 0) -> List[str]:
    """获取当前按下的修饰键。

    Tk 的 event.state 在 Windows 上并不可靠（按下 Alt 时走 WM_SYSKEYDOWN，
    Mod1 位经常取不到），因此以 GetKeyState 实时查询为准，
    再与 event.state 取并集兜底。
    """
    detected = set()

    if _key_down(VK_CONTROL):
        detected.add("ctrl")
    if _key_down(VK_MENU):  # Alt
        detected.add("alt")
    if _key_down(VK_SHIFT):
        detected.add("shift")
    if _key_down(VK_LWIN) or _key_down(VK_RWIN):
        detected.add("win")

    # Tk 的 state 位兜底（不同版本映射有差异）
    if event_state & 0x0004:
        detected.add("ctrl")
    if event_state & 0x0008:  # Mod1，Windows 上通常就是 Alt
        detected.add("alt")
    if event_state & 0x0001:
        detected.add("shift")
    if event_state & 0x0040:  # Mod4，Windows 上通常是 Win
        detected.add("win")
    if event_state & 0x20000:  # 个别版本把 Alt 报在高位
        detected.add("alt")

    return [name for name in MODIFIER_ORDER if name in detected]


class Snipper:
    """全屏半透明遮罩，鼠标拖拽选择区域。

    on_finish(region) 中 region 为 (left, top, right, bottom) 虚拟屏幕坐标，
    取消时传入 None。
    """

    def __init__(
        self,
        root: tk.Misc,
        on_finish: Callable[[Optional[Tuple[int, int, int, int]]], None],
        coord_provider: Optional[Callable[[], Optional[Tuple[int, int]]]] = None,
    ) -> None:
        """创建遮罩。

        Args:
            root: Tk 根窗口。
            on_finish: 选区回调，(left, top, right, bottom) 或 None（取消）。
            coord_provider: 返回鼠标物理像素坐标的函数，默认用
                capture.cursor_pos（GetCursorPos）。测试可注入固定值。
        """
        self._root = root
        self._on_finish = on_finish
        self._coord_provider = coord_provider or cursor_pos
        self._start: Optional[Tuple[int, int]] = None
        self._rect = None
        self._vleft, self._vtop, self._vwidth, self._vheight = virtual_screen_rect()

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.35)
        self.win.configure(bg="black")
        self.win.geometry(
            f"{self._vwidth}x{self._vheight}+{self._vleft}+{self._vtop}"
        )

        self.canvas = tk.Canvas(
            self.win,
            width=self._vwidth,
            height=self._vheight,
            bg="black",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", lambda _e: self._finish(None))
        self.win.bind("<Button-3>", lambda _e: self._finish(None))
        self.win.bind("<FocusOut>", lambda _e: None)

        self.canvas.create_text(
            self._vwidth // 2,
            32,
            text="拖拽框选二维码所在区域，Esc 或右键取消",
            fill="#ffffff",
            font=("Microsoft YaHei UI", 12),
        )

    # ---------- 事件 ----------
    def _to_canvas(self, event: tk.Event) -> Tuple[int, int]:
        """换算成遮罩内的物理像素坐标。

        截图用的是物理像素（capture.virtual_screen_rect），所以鼠标坐标也必须
        取物理像素：优先用 GetCursorPos，拿不到才退回 Tk 的 x_root/y_root。
        实测 4K@150% 下两者一致（差值 0），但 GetCursorPos 不受 Tk 自身缩放与
        多显示器混合 DPI 影响，是更可靠的口径。
        """
        position = self._coord_provider()
        if position is None:
            return event.x_root - self._vleft, event.y_root - self._vtop
        x, y = position
        return x - self._vleft, y - self._vtop

    def _on_press(self, event: tk.Event) -> None:
        self._start = self._to_canvas(event)
        if self._rect:
            self.canvas.delete(self._rect)
            self._rect = None

    def _on_drag(self, event: tk.Event) -> None:
        if not self._start:
            return
        x, y = self._to_canvas(event)
        x0, y0 = self._start
        if self._rect:
            self.canvas.delete(self._rect)
        self._rect = self.canvas.create_rectangle(
            x0, y0, x, y, outline="#ff4d4f", width=2, fill="#ffffff", stipple="gray25"
        )

    def _on_release(self, event: tk.Event) -> None:
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = self._to_canvas(event)
        self._start = None
        self._finish(
            (
                min(x0, x1) + self._vleft,
                min(y0, y1) + self._vtop,
                max(x0, x1) + self._vleft,
                max(y0, y1) + self._vtop,
            )
        )

    def _finish(self, region: Optional[Tuple[int, int, int, int]]) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self._on_finish(region)


_ACTIVE_TOASTS: List["ToastWindow"] = []

# Win32 置顶相关
_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_HWND_TOPMOST = ctypes.c_void_p(-1)
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_GA_ROOT = 2
_get_ancestor = _USER32.GetAncestor
_get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_get_ancestor.restype = ctypes.c_void_p
_set_window_pos = _USER32.SetWindowPos
_set_window_pos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_set_window_pos.restype = ctypes.c_bool

_window_from_point = _USER32.WindowFromPoint
_window_from_point.argtypes = [wintypes.POINT]
_window_from_point.restype = ctypes.c_void_p
_get_window_rect = _USER32.GetWindowRect
_get_window_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
_get_window_rect.restype = ctypes.c_bool


class ToastWindow:
    """右下角提示小窗：这是**唯一能保证用户看得见**的通知通道。

    为什么不用系统气泡：真机实测打包成 exe 后 `icon.notify()` 与
    `Shell_NotifyIcon` 都返回成功但屏幕上什么都不出现（详见 notify.py）。
    自己画的窗口不经过系统通知策略，只要进程活着就一定看得见。

    多个通知同时到达时向上错开叠放，避免互相遮挡成"只有一条"。
    """

    MARGIN_RIGHT = 24
    MARGIN_BOTTOM = 72
    GAP = 8

    def __init__(
        self,
        root: tk.Misc,
        title: str,
        message: str,
        duration_ms: int = 4000,
    ) -> None:
        self.win = tk.Toplevel(root)
        self.win.configure(bg="#1f1f1f")

        frame = tk.Frame(self.win, bg="#1f1f1f", padx=14, pady=10)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text=title, bg="#1f1f1f", fg="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"), anchor="w", justify="left",
        ).pack(fill="x")
        tk.Label(
            frame, text=message, bg="#1f1f1f", fg="#dddddd",
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
            wraplength=360,
        ).pack(fill="x", pady=(4, 0))

        self.win.update_idletasks()
        width = self.win.winfo_width()
        height = self.win.winfo_height()
        self.height = height
        vleft, vtop, vwidth, vheight = virtual_screen_rect()
        # 已有浮窗占住的位置要让开，否则连按两次快捷键只会看到一条，另一条被盖住
        offset = sum(
            other.height + self.GAP for other in list(_ACTIVE_TOASTS) if other.alive()
        )
        x = vleft + vwidth - width - self.MARGIN_RIGHT
        y = vtop + vheight - height - self.MARGIN_BOTTOM - offset
        x, y = max(x, vleft), max(y, vtop)
        # 坑：overrideredirect 之前必须给出**完整**的 "宽x高+X+Y"。
        # 只给 "+X+Y" 时 Tk 会把位置请求丢掉，窗口停在 (0,0) 左上角。
        self.win.geometry(f"{width}x{height}+{x}+{y}")
        self.win.overrideredirect(True)
        self._x, self._y, self._vtop = x, y, vtop
        _ACTIVE_TOASTS.append(self)

        # 置顶必须在窗口**映射之后**再做一次：overrideredirect 的窗口如果只在
        # 创建前设 -topmost，实测会停在普通 z 序里被前台窗口挡住——
        # 窗口对象存在、IsWindowVisible 也是 True，但屏幕上就是看不见。
        self._raise_topmost()
        self._avoid_occlusion()
        self.win.after(60, self._raise_topmost)
        self.win.after(duration_ms, self.close)

    def _avoid_occlusion(self, attempts: int = 3) -> None:
        """目标位置被挡住时把浮窗往上抬。

        实测场景：Windows 自己的通知气泡（XAML 岛窗口）弹出后停在右下角，
        它属于系统的合成层，普通窗口既压不过也挪不动。此时如果硬把浮窗放在
        固定位置，用户就什么都看不见——等于没提示。
        这里用 WindowFromPoint 判断自己是不是最上层，不是就往上让。
        """
        hwnd = _get_ancestor(int(self.win.winfo_id()), _GA_ROOT)
        if not hwnd:
            return
        for _ in range(attempts):
            probe_x = self._x + max(self.win.winfo_width() // 2, 8)
            probe_y = self._y + max(self.height // 2, 8)
            try:
                top = _window_from_point(wintypes.POINT(probe_x, probe_y))
                top_root = _get_ancestor(top, _GA_ROOT) or top
            except Exception:
                return
            if int(top_root or 0) == int(hwnd):
                return

            rect = wintypes.RECT()
            try:
                _get_window_rect(top_root, ctypes.byref(rect))
            except Exception:
                return
            # 抬到遮挡窗口上沿之上，至少让开自己的高度
            new_y = rect.top - self.height - self.GAP
            if new_y >= self._y:
                new_y = self._y - self.height - self.GAP
            self._y = max(self._vtop, new_y)
            self._raise_topmost()

    def _raise_topmost(self) -> None:
        """真正做到窗口置顶，并校正位置。

        两个坑，都只在实测里能发现：
        1. `overrideredirect(True)` 的窗口上 `wm attributes -topmost` **不生效**
           ——设完之后 WS_EX_TOPMOST 位仍然是 False，提示框就停在普通 z 序里
           被前台窗口挡住：窗口在、IsWindowVisible 也是 True，屏幕上看不见。
        2. override-redirect 之后 Tk 会忽略位置请求，窗口可能停在 (0,0)。
        所以统一用 SetWindowPos(HWND_TOPMOST) 同时锁住位置和层级。
        """
        if not self.alive():
            return
        try:
            self.win.attributes("-topmost", True)
            self.win.lift()
        except tk.TclError:
            return
        try:
            hwnd = _get_ancestor(int(self.win.winfo_id()), _GA_ROOT)
            _set_window_pos(
                ctypes.c_void_p(int(hwnd) if hwnd else int(self.win.winfo_id())),
                _HWND_TOPMOST, self._x, self._y,
                self.win.winfo_width(), self.height,
                _SWP_NOACTIVATE,
            )
        except Exception:
            # 置顶失败不影响窗口本身，最差退化成普通窗口
            pass

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        if self in _ACTIVE_TOASTS:
            _ACTIVE_TOASTS.remove(self)
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class HotkeyEntry(ttk.Frame):
    """快捷键输入框：点击后捕获下一次按键组合。"""

    def __init__(self, master: tk.Misc, label: str, value: str) -> None:
        super().__init__(master)
        self.value = value
        self._capturing = False
        self._top_binding: Optional[str] = None

        ttk.Label(self, text=label, width=10).pack(side="left", padx=(0, 8))
        self.var = tk.StringVar(value=value)
        self.entry = ttk.Entry(self, textvariable=self.var, width=22, justify="center")
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<Key>", self._on_key, add="+")
        self.entry.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_in(self, _event: tk.Event) -> None:
        self._capturing = True
        self.var.set("请按下新的快捷键…")
        # 按下 Alt 时焦点可能被系统菜单抢走，额外在顶层窗口挂一份绑定兜底
        try:
            self._top_binding = self.winfo_toplevel().bind("<Key>", self._on_key, add="+")
        except tk.TclError:
            self._top_binding = None
        # 再兜一层：直接轮询键盘状态，避免 Tk 完全收不到 Alt 组合事件
        self.after(60, self._poll_keyboard)

    def _poll_keyboard(self) -> None:
        """轮询当前按下的主键（配合 GetKeyState 读取修饰键），不依赖 Tk 事件。"""
        if not self._capturing:
            return
        try:
            vk = pressed_key_vk()
            if vk is not None:
                name = vk_to_key_name(vk)
                if name:
                    parts = pressed_modifiers()
                    parts.append(name)
                    self.value = "+".join(parts)
                    self.var.set(self.value)
                    self._release_capture()
                    return
            self.after(60, self._poll_keyboard)
        except tk.TclError:  # 组件已销毁
            self._capturing = False

    def _release_capture(self) -> None:
        if self._top_binding:
            try:
                self.winfo_toplevel().unbind("<Key>", self._top_binding)
            except tk.TclError:
                pass
            self._top_binding = None
        self._capturing = False

    def _on_focus_out(self, _event: tk.Event) -> None:
        self._release_capture()
        if not self.var.get().startswith("请按下"):
            self.value = self.var.get()
        else:
            self.var.set(self.value)

    def _on_key(self, event: tk.Event) -> str:
        if not self._capturing or event.keysym in MODIFIER_KEYSYMS:
            return "break"

        parts = pressed_modifiers(getattr(event, "state", 0) or 0)
        parts.append(keysym_to_key_name(event.keysym))
        self.value = "+".join(parts)
        self.var.set(self.value)
        self._release_capture()
        return "break"


class SettingsWindow:
    """设置窗口：修改两个快捷键与各项开关。"""

    def __init__(
        self, root: tk.Misc, config: Dict, on_save: Callable[[Dict], None]
    ) -> None:
        self._on_save = on_save
        self.win = tk.Toplevel(root)
        self.win.title("QRClipper 设置")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(self.win, padding=16)
        frame.pack(fill="both", expand=True)

        self.full_entry = HotkeyEntry(frame, "全屏识别", config.get("hotkey_full", ""))
        self.full_entry.pack(fill="x", pady=4)
        self.snip_entry = HotkeyEntry(frame, "框选识别", config.get("hotkey_snip", ""))
        self.snip_entry.pack(fill="x", pady=4)

        ttk.Label(
            frame,
            text="提示：点击输入框后直接按下组合键（如 Ctrl+Alt+Q）即可设置。",
            foreground="#666666",
        ).pack(anchor="w", pady=(2, 8))

        self.notify_var = tk.BooleanVar(value=bool(config.get("notify", True)))
        self.autostart_var = tk.BooleanVar(value=bool(config.get("autostart", False)))
        self.copy_var = tk.BooleanVar(value=bool(config.get("copy_to_clipboard", True)))
        self.sound_var = tk.BooleanVar(value=bool(config.get("sound", False)))

        ttk.Checkbutton(frame, text="识别后显示气泡提示", variable=self.notify_var).pack(
            anchor="w", pady=2
        )
        ttk.Checkbutton(frame, text="识别结果写入剪贴板", variable=self.copy_var).pack(
            anchor="w", pady=2
        )
        ttk.Checkbutton(frame, text="提示音", variable=self.sound_var).pack(anchor="w", pady=2)
        ttk.Checkbutton(frame, text="开机自动启动", variable=self.autostart_var).pack(
            anchor="w", pady=2
        )

        button_bar = ttk.Frame(frame)
        button_bar.pack(fill="x", pady=(12, 0))
        ttk.Button(button_bar, text="保存", command=self._save).pack(side="right", padx=4)
        ttk.Button(button_bar, text="取消", command=self.close).pack(side="right", padx=4)

        self._center()

    def _center(self) -> None:
        self.win.update_idletasks()
        width, height = self.win.winfo_width(), self.win.winfo_height()
        x = (self.win.winfo_screenwidth() - width) // 2
        y = (self.win.winfo_screenheight() - height) // 3
        self.win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _save(self) -> None:
        self._on_save(
            {
                "hotkey_full": self.full_entry.value,
                "hotkey_snip": self.snip_entry.value,
                "notify": self.notify_var.get(),
                "copy_to_clipboard": self.copy_var.get(),
                "sound": self.sound_var.get(),
                "autostart": self.autostart_var.get(),
            }
        )
        self.close()

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


def make_tray_image(size: int = 64) -> Image.Image:
    """绘制一个二维码风格的托盘图标（不依赖外部图片资源）。"""
    image = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    margin = size // 8
    box = size - margin * 2
    draw.rectangle([margin, margin, margin + box, margin + box], fill="white")

    marker = box // 4
    inner = marker // 2
    for (ox, oy) in ((0, 0), (box - marker, 0), (0, box - marker)):
        x = margin + ox
        y = margin + oy
        draw.rectangle([x, y, x + marker, y + marker], fill="black")
        draw.rectangle(
            [x + marker // 5, y + marker // 5, x + marker - marker // 5, y + marker - marker // 5],
            fill="white",
        )
        draw.rectangle(
            [
                x + (marker - inner) // 2,
                y + (marker - inner) // 2,
                x + (marker + inner) // 2,
                y + (marker + inner) // 2,
            ],
            fill="black",
        )

    # 右下区域的随机感模块（用固定图案保证每次一致）
    pattern = [
        (0, 3), (1, 1), (1, 3), (2, 0), (2, 2), (3, 1), (3, 3),
        (0, 0), (1, 2), (2, 1), (3, 0), (0, 2), (2, 3),
    ]
    cell = box // 8
    for col, row in pattern:
        x = margin + box - cell * 4 + col * cell
        y = margin + box - cell * 4 + row * cell
        draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill="black")
    return image
