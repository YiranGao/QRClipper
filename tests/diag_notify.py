"""真机诊断：走**产品真实通知路径**（notify.Notifier），验证两种图标状态下都弹得出来。

为什么这么测：最初的版本 `_notify()` 先调 `pystray.icon.notify()`，成功就 return。
而在这台机器上 `Shell_NotifyIcon` 的返回成功**不代表用户看得见**（系统通知策略会
静默吞掉，既不抛异常也没有任何回调），于是永远走不到 Tk 浮窗兜底 —— 用户看到的就是
"程序在跑，但完全没反馈"。

所以这里不再直接调 pystray，而是调 App 用的同一个 `Notifier`：

  场景 1 托盘图标已就绪（icon 可见）
  场景 2 完全没有托盘（icon 为 None，对应 --no-tray / 托盘创建失败）

两个场景都必须落到 `通道=Tk 浮窗`，并在屏幕上截图可见。顺带把裸调 pystray 的结果
也打印出来，作为"原生通道不可信"的对照证据。

    python tests/diag_notify.py

产物：tests/_diag_toast.png（有图标）、tests/_diag_pystray.png（无图标）
"""

from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capture  # noqa: E402
import ui  # noqa: E402
from notify import Notifier  # noqa: E402

TITLE = "未识别到二维码"
BODY = "这是通知通道的测试文案：看到这条就说明程序活着、只是没识别到码。"


def shot_of(window: tk.Toplevel, tag: str) -> str:
    """按浮窗真实位置截图（不写死坐标，避免又被 DPI / 位置问题骗一次）。"""
    window.update_idletasks()
    x, y = window.winfo_rootx(), window.winfo_rooty()
    w, h = window.winfo_width(), window.winfo_height()
    path = os.path.abspath(os.path.join("tests", f"_diag_{tag}.png"))
    capture.grab((x - 8, y - 8, x + w + 8, y + h + 8)).save(path)
    return path


def run_scenario(tag: str, with_icon: bool) -> None:
    root = tk.Tk()
    root.withdraw()
    root.update()

    icon = None
    if with_icon:
        import pystray
        from PIL import Image

        icon = pystray.Icon("qrdiag", Image.new("RGB", (64, 64), (30, 30, 30)), "QRDiag")
        threading.Thread(target=icon.run, daemon=True).start()
        deadline = time.time() + 8
        while time.time() < deadline and not icon.visible:
            time.sleep(0.05)
        print(f"    pystray icon.visible = {icon.visible}")

    holder = {}

    def toast(title: str, message: str, duration_ms: int = 4000) -> None:
        holder["win"] = ui.ToastWindow(root, title, message, duration_ms=duration_ms)

    notifier = Notifier(
        logger=__import__("logging").getLogger("diag"),
        icon_getter=lambda: icon,
        toast=toast,
        enabled=lambda: True,
    )
    channel = notifier.send(TITLE, BODY, force=True, duration_ms=8000)
    for _ in range(10):
        root.update()
        time.sleep(0.06)
    time.sleep(0.4)

    window = holder.get("win")
    path = shot_of(window.win, tag) if window else "（没有创建浮窗）"
    print(f"[{'有托盘图标' if with_icon else '无托盘图标'}] 通道 = {channel}  截图 = {path}")
    print(f"    结论：{'PASS 已用浮窗' if channel == 'toast' else 'FAIL 没有走浮窗'}")

    root.destroy()
    if icon is not None:
        icon.stop()
    time.sleep(0.6)


def probe_raw_pystray() -> None:
    """裸调 pystray，留个对照：它返回成功但屏幕上往往什么都没有。"""
    import pystray
    from PIL import Image

    icon = pystray.Icon("qrraw", Image.new("RGB", (64, 64), (30, 30, 30)), "QRRaw")
    threading.Thread(target=icon.run, daemon=True).start()
    deadline = time.time() + 8
    while time.time() < deadline and not icon.visible:
        time.sleep(0.05)
    try:
        icon.notify("裸调 pystray 的对照文案", "未识别到二维码")
        print(f"    裸调 pystray.notify：无异常返回（visible={icon.visible}），")
        print("    但屏幕上通常什么都不会出现 —— 这就是不能用它做主通道的原因")
    except Exception as exc:  # noqa: BLE001
        print(f"    裸调 pystray.notify 抛异常：{exc!r}")
    time.sleep(1.0)
    icon.stop()


if __name__ == "__main__":
    capture.enable_dpi_awareness()
    print("=== 场景 1：托盘图标就绪 ===")
    run_scenario("toast", with_icon=True)
    print("=== 场景 2：完全没有托盘（--no-tray）===")
    run_scenario("pystray", with_icon=False)
    print("=== 对照：裸调 pystray ===")
    probe_raw_pystray()
