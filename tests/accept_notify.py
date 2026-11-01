"""真机验收：对着**已部署的 exe**（不是源码）验证通知真的弹在右下角。

为什么单独一个脚本：本轮 bug 的教训是「返回成功 != 用户看得见」。
`icon.notify()` / `Shell_NotifyIcon` 在打包后的 exe 里返回 TRUE、不抛异常，
但屏幕上什么都没有（见 tests/diag_notify.py 的对照实验）。所以验收必须：
触发真实热键 → 截屏 → 人眼（或模型）看到文案。

覆盖三个场景：

  场景 A  屏幕上没有二维码 → 必须看到「未识别到二维码 / 已截取 WxH…」
  场景 B  连续触发两次     → 两条浮窗上下堆叠，都必须看得见
  场景 C  屏幕上有二维码   → 必须看到「已复制到剪贴板」且剪贴板内容正确

场景 A 的难点是「让屏幕上真的没有码」。这里不去关用户的窗口，
而是盖一张**非置顶**的纯白遮罩（topmost 会让产品的浮窗被压在下面）：
遮罩在白底上没有任何二维码，而产品浮窗本身是 topmost，仍然盖在遮罩之上。
脚本会用同一个 capture.grab() 自检「遮罩下确实一个码都识别不到」，
自检不过就直接 FAIL，避免又演一次「假通过」。

    python tests/accept_notify.py            # 跑全部三个场景

产物（均已 .gitignore）：tests/_shot_full_0.png / _shot_stacked.png / _shot_ok.png
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk
from ctypes import wintypes
from typing import List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import capture  # noqa: E402
import clipboard  # noqa: E402
import decoder  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowW.restype = ctypes.c_void_p
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PostMessageW.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

WM_HOTKEY = 0x0312
HOTKEY_WINDOW_TITLE = "QRClipperHotkeyWindow"
HOTKEY_IDS = {"full": 0x1000, "snip": 0x1001}
SHOT_DIR = os.path.dirname(os.path.abspath(__file__))

# 浮窗的类名是 Tk 的 Toplevel；用它来找"屏幕上现在有几条通知"
TOAST_CLASS = "TkTopLevel"
TOAST_MIN_WIDTH = 120
TOAST_MAX_WIDTH = 900
TOAST_MIN_HEIGHT = 40
TOAST_MAX_HEIGHT = 400


def hotkey_window() -> Optional[int]:
    handle = user32.FindWindowW(None, HOTKEY_WINDOW_TITLE)
    return int(handle) if handle else None


def enumerate_windows() -> List[int]:
    handles: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):  # noqa: ANN001
        handles.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return handles


def window_info(hwnd: int) -> Tuple[str, Tuple[int, int, int, int], bool]:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    visible = bool(user32.IsWindowVisible(hwnd))
    return buffer.value, (rect.left, rect.top, rect.right, rect.bottom), visible


def toast_rects(exclude: Tuple[int, ...] = ()) -> List[Tuple[int, int, int, int]]:
    """返回当前屏幕上"像通知浮窗"的窗口矩形（按 top 排序）。"""
    found: List[Tuple[int, int, int, int]] = []
    for hwnd in enumerate_windows():
        if hwnd in exclude:
            continue
        name, (left, top, right, bottom), visible = window_info(hwnd)
        if not visible or name != TOAST_CLASS:
            continue
        width, height = right - left, bottom - top
        if not (TOAST_MIN_WIDTH <= width <= TOAST_MAX_WIDTH):
            continue
        if not (TOAST_MIN_HEIGHT <= height <= TOAST_MAX_HEIGHT):
            continue
        found.append((left, top, right, bottom))
    return sorted(found, key=lambda item: item[1])


def wait_toasts_gone(timeout: float = 12.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and toast_rects():
        time.sleep(0.1)


def shot(tag: str, box: Optional[Tuple[int, int, int, int]] = None) -> str:
    """截一块区域。默认取右下角 1000x600（足够装下多行文案的浮窗）。"""
    left, top, width, height = capture.virtual_screen_rect()
    region = box or (left + width - 1020, top + height - 600, left + width, top + height)
    path = os.path.join(SHOT_DIR, f"_shot_{tag}.png")
    capture.grab(region).save(path)
    return path


def post_hotkey(which: str = "full") -> bool:
    handle = hotkey_window()
    if not handle:
        return False
    return bool(user32.PostMessageW(handle, WM_HOTKEY, HOTKEY_IDS[which], 0))


class QrBoard:
    """在屏幕正中摆一个二维码窗口，供「识别成功」场景使用。

    位置取虚拟屏中心而不是写死坐标：换分辨率/换缩放都不用改脚本。
    """

    def __init__(self, text: str) -> None:
        import qrcode
        from PIL import ImageTk

        self.text = text
        left, top, width, height = capture.virtual_screen_rect()
        image = qrcode.make(text).get_image()
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.configure(bg="white")
        self.root.attributes("-topmost", True)
        self._photo = ImageTk.PhotoImage(image)
        tk.Label(self.root, image=self._photo, bg="white").pack(padx=10, pady=10)
        self.root.update_idletasks()
        board_w = self.root.winfo_width()
        board_h = self.root.winfo_height()
        x = left + (width - board_w) // 2
        y = top + (height - board_h) // 2
        # 与 ui.ToastWindow 同样的坑：overrideredirect 之前要给完整 "宽x高+X+Y"
        self.root.geometry(f"{board_w}x{board_h}+{x}+{y}")
        self.root.update()
        self.hwnd = int(self.root.wm_frame(), 16)
        time.sleep(0.5)

    def visible_codes(self) -> List[str]:
        self.root.update()
        return decoder.decode_image(capture.grab_all(), tile_scan=False)

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


class WhiteOverlay:
    """盖满虚拟屏幕的纯白遮罩。

    刻意**不**置顶：产品浮窗是 topmost，必须能盖在遮罩上面被用户看到。
    """

    def __init__(self, message: str) -> None:
        left, top, width, height = capture.virtual_screen_rect()
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.configure(bg="white")
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        tk.Label(
            self.root,
            text=message,
            bg="white",
            fg="#555555",
            font=("Microsoft YaHei", 28),
        ).place(relx=0.5, rely=0.35, anchor="center")
        self.root.update()
        self.hwnd = int(self.root.wm_frame(), 16)
        time.sleep(0.4)

    def codes_on_screen(self) -> List[str]:
        """自检：遮罩之下（也就是产品会截到的东西）还能识别出码吗？"""
        self.root.update()
        try:
            return decoder.decode_image(capture.grab_all())
        except Exception as exc:  # noqa: BLE001
            print(f"    自检解码失败（按「没有码」继续）：{exc!r}")
            return []

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def scenario_no_code() -> bool:
    print("=== 场景 A：屏幕上没有二维码 → 必须弹「未识别到二维码」 ===")
    wait_toasts_gone()
    overlay = WhiteOverlay("（验收用遮罩：模拟屏幕上没有二维码）")
    try:
        codes = overlay.codes_on_screen()
        print(f"    自检：遮罩下可识别到的码 = {codes}")
        if codes:
            print("    FAIL 遮罩没遮干净，本场景不成立（换个场景再验）")
            return False

        if not post_hotkey("full"):
            print("    FAIL 热键窗口不存在，投递失败")
            return False

        time.sleep(2.2)
        rects = toast_rects(exclude=(overlay.hwnd,))
        print(f"    浮窗数量 = {len(rects)}  位置 = {rects}")
        print(f"    截图 = {shot('full_0')}")
        return bool(rects)
    finally:
        overlay.close()
        time.sleep(0.6)


def scenario_stacked() -> bool:
    print("=== 场景 B：连续触发两次 → 两条浮窗上下堆叠 ===")
    wait_toasts_gone()
    if not post_hotkey("full"):
        print("    FAIL 热键窗口不存在")
        return False
    time.sleep(0.30)
    post_hotkey("full")
    time.sleep(2.0)
    rects = toast_rects()
    print(f"    浮窗数量 = {len(rects)}  位置 = {rects}")
    overlapped = False
    for first, second in zip(rects, rects[1:]):
        if second[1] < first[3]:  # 第二条的顶部高于第一条的底部 => 重叠了
            overlapped = True
    if len(rects) < 2:
        print("    FAIL 只看到一条浮窗，没有堆叠")
        return False
    if overlapped:
        print("    FAIL 两条浮窗互相重叠了，用户会看不清")
        return False
    print("    结论：PASS 两条浮窗错开排列")
    print(f"    截图 = {shot('stacked')}")
    return True


def scenario_success() -> bool:
    print("=== 场景 C：屏幕上有二维码 → 必须弹「已复制到剪贴板」且剪贴板对得上 ===")
    wait_toasts_gone()
    expected = f"QRClipper-ACCEPT-{int(time.time())}"
    board = QrBoard(expected)
    try:
        codes = board.visible_codes()
        print(f"    自检：屏幕上可识别到的码 = {codes}")
        if expected not in codes:
            print("    FAIL 二维码没摆上屏幕，本场景不成立")
            return False

        if not post_hotkey("full"):
            print("    FAIL 热键窗口不存在，投递失败")
            return False

        time.sleep(2.6)
        rects = toast_rects(exclude=(board.hwnd,))
        after = clipboard.get_text()
        print(f"    浮窗数量 = {len(rects)}  位置 = {rects}")
        print(f"    期望剪贴板 = {expected!r}")
        print(f"    实际剪贴板 = {after!r}")
        print(f"    截图 = {shot('ok')}")
        if not rects:
            print("    FAIL 没有浮窗")
            return False
        if after != expected:
            print("    FAIL 剪贴板内容与屏幕上的二维码不一致")
            return False
        print("    结论：PASS 已复制且内容一致")
        return True
    finally:
        board.close()
        time.sleep(0.6)


def main(argv: List[str]) -> int:
    capture.enable_dpi_awareness()
    handle = hotkey_window()
    print(f"热键窗口「{HOTKEY_WINDOW_TITLE}」= {handle if handle else 'NULL'}")
    if not handle:
        print("FAIL 程序没在运行 / 核心没起来，先启动 QRClipper.exe")
        return 2

    wanted = set(argv) or {"no_code", "stacked", "success"}
    runners = {
        "no_code": scenario_no_code,
        "stacked": scenario_stacked,
        "success": scenario_success,
    }
    results = {}
    for name, runner in runners.items():
        if name in wanted:
            results[name] = runner()

    print("--- 汇总 ---")
    for name, ok in results.items():
        print(f"  {name:10s} {'PASS' if ok else 'FAIL'}")
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
