"""QRClipper 主程序。

后台常驻系统托盘，支持两组自定义全局快捷键：
- 全屏识别：截取所有显示器画面，识别其中的二维码并复制到剪贴板；
- 框选识别：拖拽框选屏幕区域后识别该区域。

命令行参数：
    --scan      立即全屏识别一次并退出（可用于命令行/其他程序调用）
    --snip      弹出框选一次并退出
    --selftest  生成测试二维码图片并自检识别链路
    --no-tray   不显示托盘图标（配合 --scan 使用）
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes
from typing import List, Optional, Tuple

import capture
import clipboard
import config as config_module
from config import Config, set_autostart, setup_logging
from decoder import decode_image
from hotkey import HotKeyManager, format_hotkey, parse_hotkey
from ui import SettingsWindow, Snipper, make_tray_image

ACTION_FULL = "full"
ACTION_SNIP = "snip"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


class SingleInstance:
    """用命名互斥体保证只有一个实例运行。"""

    def __init__(self, name: str = "QRClipper_SingleInstance") -> None:
        self._handle = kernel32.CreateMutexW(None, False, name)

    @property
    def already_running(self) -> bool:
        return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


class App:
    """应用主控：托盘 + 热键 + 识别调度。"""

    def __init__(self, show_tray: bool = True) -> None:
        capture.enable_dpi_awareness()
        self.cfg = Config()
        self.logger: logging.Logger = setup_logging()
        self.show_tray = show_tray

        self.root = tk.Tk()
        self.root.title("QRClipper")
        self.root.withdraw()

        self.hotkeys = HotKeyManager(self._on_hotkey)
        self.icon = None
        self._settings_window: Optional[SettingsWindow] = None
        self._snipper: Optional[Snipper] = None

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self._apply_autostart()
        self.hotkeys.start()
        self.hotkeys.wait_ready()
        self._reload_hotkeys()

        if self.show_tray:
            self._start_tray()

        self.logger.info(
            "QRClipper 已启动：全屏=%s 框选=%s",
            format_hotkey(self.cfg.get("hotkey_full")),
            format_hotkey(self.cfg.get("hotkey_snip")),
        )
        self.root.mainloop()

    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception as exc:  # pragma: no cover
            self.logger.error("托盘库不可用：%s", exc)
            return

        self.icon = pystray.Icon(
            "QRClipper",
            make_tray_image(),
            "QRClipper - 屏幕二维码识别",
            menu=pystray.Menu(self._build_tray_menu),
        )
        threading.Thread(target=self.icon.run, name="TrayThread", daemon=True).start()

    def _build_tray_menu(self):
        import pystray

        full_label = f"全屏识别 ({format_hotkey(self.cfg.get('hotkey_full'))})"
        snip_label = f"框选识别 ({format_hotkey(self.cfg.get('hotkey_snip'))})"
        return pystray.Menu(
            pystray.MenuItem(full_label, self._menu_scan, default=True),
            pystray.MenuItem(snip_label, self._menu_snip),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置…", self._menu_settings),
            pystray.MenuItem(
                "开机自动启动",
                self._menu_toggle_autostart,
                checked=lambda _item: bool(self.cfg.get("autostart")),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._menu_quit),
        )

    def _refresh_tray_menu(self) -> None:
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def quit(self) -> None:
        self.logger.info("QRClipper 退出")
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ---------- 托盘菜单回调（运行在托盘线程，统一转发到 Tk 线程）----------
    def _menu_scan(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.scan_full)

    def _menu_snip(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.begin_snip)

    def _menu_settings(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.open_settings)

    def _menu_toggle_autostart(self, _icon=None, _item=None) -> None:
        self.root.after(0, self._toggle_autostart)

    def _menu_quit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.quit)

    def _toggle_autostart(self) -> None:
        new_value = not bool(self.cfg.get("autostart"))
        self.cfg.set("autostart", new_value)
        self._apply_autostart()
        self._refresh_tray_menu()

    def _apply_autostart(self) -> None:
        if self.cfg.get("autostart"):
            if not set_autostart(True):
                self.logger.warning("写入开机自启动失败（可能被安全软件拦截）")
        else:
            set_autostart(False)

    # ---------- 热键 ----------
    def _reload_hotkeys(self) -> None:
        bindings = {
            ACTION_FULL: self.cfg.get("hotkey_full", ""),
            ACTION_SNIP: self.cfg.get("hotkey_snip", ""),
        }
        self.hotkeys.update_bindings(bindings)

    def _on_hotkey(self, action_id: str) -> None:
        if action_id == ACTION_FULL:
            self.root.after(0, self.scan_full)
        elif action_id == ACTION_SNIP:
            self.root.after(0, self.begin_snip)

    # ---------- 识别 ----------
    def scan_full(self) -> None:
        """全屏识别：在工作线程执行，避免阻塞界面。"""
        threading.Thread(target=self._scan_worker, args=(None,), daemon=True).start()

    def begin_snip(self) -> None:
        """弹出框选遮罩（必须在 Tk 线程创建窗口）。"""
        if self._snipper is not None:
            return
        self._snipper = Snipper(self.root, self._on_snip_finished)

    def _on_snip_finished(self, region: Optional[Tuple[int, int, int, int]]) -> None:
        self._snipper = None
        if region is None:
            return
        try:
            region = capture.normalize_region(*region)
        except ValueError:
            self._notify("框选已取消", "选区太小，请重新框选")
            return
        threading.Thread(target=self._scan_worker, args=(region,), daemon=True).start()

    def _scan_worker(self, region: Optional[Tuple[int, int, int, int]]) -> None:
        started = time.perf_counter()
        try:
            image = capture.grab(region) if region else capture.grab_all()
            texts = decode_image(image)
        except Exception as exc:
            self.logger.exception("识别过程异常：%s", exc)
            texts = []
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.root.after(0, lambda: self._present(texts, elapsed_ms))

    def _present(self, texts: List[str], elapsed_ms: int) -> None:
        if not texts:
            self.logger.info("未识别到二维码（耗时 %d ms）", elapsed_ms)
            self._notify("未识别到二维码", "请确认二维码在屏幕上清晰可见")
            self._beep(False)
            return

        joined = "\n".join(texts)
        if self.cfg.get("copy_to_clipboard", True):
            if not clipboard.set_text(joined):
                self.logger.warning("写入剪贴板失败")
        self.logger.info("识别成功（%d 个，耗时 %d ms）", len(texts), elapsed_ms)

        preview = texts[0] if len(texts) == 1 else f"{texts[0]}\n…共 {len(texts)} 个"
        if len(preview) > 120:
            preview = preview[:120] + "…"
        title = f"已复制 {len(texts)} 个二维码内容" if len(texts) > 1 else "已复制到剪贴板"
        self._notify(title, preview)
        self._beep(True)

    def _notify(self, title: str, message: str) -> None:
        if not self.cfg.get("notify", True):
            return
        if self.icon is not None:
            try:
                self.icon.notify(message, title)
            except Exception as exc:
                self.logger.debug("气泡提示失败：%s", exc)

    def _beep(self, success: bool) -> None:
        if not self.cfg.get("sound", False):
            return
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_OK if success else winsound.MB_ICONHAND)
        except Exception:
            pass

    # ---------- 设置 ----------
    def open_settings(self) -> None:
        if self._settings_window is not None:
            return
        self._settings_window = SettingsWindow(self.root, self.cfg.as_dict(), self._save_settings)
        self.root.wait_window(self._settings_window.win)
        self._settings_window = None

    def _save_settings(self, values: dict) -> None:
        invalid = [
            name
            for name in ("hotkey_full", "hotkey_snip")
            if parse_hotkey(values.get(name, "")) is None
        ]
        if invalid:
            self._notify("快捷键无效", "已恢复为原来的设置")
            return

        for key, value in values.items():
            self.cfg.set(key, value)
        self.cfg.save()
        self._apply_autostart()
        self._reload_hotkeys()
        self._refresh_tray_menu()
        self.logger.info("设置已保存：%s", values)


# ---------- 命令行模式 ----------
def run_single_scan(region: Optional[Tuple[int, int, int, int]] = None) -> int:
    """截屏识别一次，把结果复制到剪贴板并打印，然后退出。"""
    capture.enable_dpi_awareness()
    setup_logging()
    try:
        image = capture.grab(region) if region else capture.grab_all()
        texts = decode_image(image)
    except Exception as exc:
        print(f"识别失败：{exc}", file=sys.stderr)
        return 2

    if not texts:
        print("未识别到二维码")
        return 1

    joined = "\n".join(texts)
    clipboard.set_text(joined)
    for text in texts:
        print(text)
    return 0


def _emit(text: str) -> None:
    """输出结果：控制台可用时打印，无控制台（打包后的 windowed 模式）只写日志。"""
    print(text)
    logging.getLogger("QRClipper").info(text)


def _show_message(title: str, text: str) -> None:
    """无控制台时用系统弹窗展示结果（自检场景）。"""
    try:
        ctypes.WinDLL("user32").MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION
    except Exception:
        pass


def run_selftest() -> int:
    """生成二维码图片并验证识别链路（含剪贴板读写）。"""
    capture.enable_dpi_awareness()
    logger = setup_logging()
    try:
        import qrcode
        from PIL import Image
    except Exception as exc:
        _emit(f"自检需要 qrcode 库：{exc}")
        return 2

    cases = [
        ("https://workbuddy.cn/docs", "URL"),
        ("你好，QRClipper！中文测试", "中文"),
        ("WIFI:T:WPA;S:MyNet;P:pass123;;", "WiFi 配置"),
        ("x" * 300, "长文本"),
    ]

    passed = 0
    report: List[str] = []
    tmp_dir = tempfile.mkdtemp(prefix="qrclip_selftest_")
    for index, (content, label) in enumerate(cases, start=1):
        qr = qrcode.QRCode(version=None, box_size=8, border=4)
        qr.add_data(content)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        path = os.path.join(tmp_dir, f"case{index}.png")
        image.save(path)

        # 模拟真实场景：把二维码贴到一张“屏幕截图”大小的背景上
        background = Image.new("RGB", (1280, 720), (240, 240, 240))
        background.paste(image, (100, 80))
        results = decode_image(background)
        ok = results and results[0] == content
        line = f"[{'PASS' if ok else 'FAIL'}] {label}: {results[:1] if results else '未识别'}"
        report.append(line)
        _emit(line)
        passed += 1 if ok else 0

    probe = "QRClipper 剪贴板自检 " + time.strftime("%H:%M:%S")
    clipboard_ok = clipboard.set_text(probe) and clipboard.get_text() == probe
    line = f"[{'PASS' if clipboard_ok else 'FAIL'}] 剪贴板写入/读取"
    report.append(line)
    _emit(line)
    passed += 1 if clipboard_ok else 0

    total = len(cases) + 1
    summary = f"自检结果：{passed}/{total} 通过"
    report.append(summary)
    _emit(summary)

    # 打包后没有控制台，把报告落到文件并弹窗提示
    try:
        report_path = os.path.join(config_module.app_dir(), "selftest_result.txt")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(report))
    except OSError:
        pass
    if sys.stdout is None or getattr(sys, "frozen", False):
        _show_message("QRClipper 自检", "\n".join(report))

    return 0 if passed == total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="屏幕二维码识别并复制到剪贴板")
    parser.add_argument("--scan", action="store_true", help="全屏识别一次后退出")
    parser.add_argument("--snip", action="store_true", help="框选识别一次后退出")
    parser.add_argument("--selftest", action="store_true", help="运行识别链路自检")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    if args.selftest:
        return run_selftest()
    if args.scan:
        return run_single_scan()
    if args.snip:
        capture.enable_dpi_awareness()
        setup_logging()
        region_holder: List[Optional[Tuple[int, int, int, int]]] = []

        root = tk.Tk()
        root.withdraw()

        def on_finish(region):
            region_holder.append(region)
            root.destroy()

        root.after(50, lambda: Snipper(root, on_finish))
        root.mainloop()
        region = region_holder[0] if region_holder else None
        if region is None:
            return 1
        return run_single_scan(capture.normalize_region(*region))

    guard = SingleInstance()
    if guard.already_running:
        print("QRClipper 已在运行中（托盘图标位于屏幕右下角）", file=sys.stderr)
        return 0

    app = App(show_tray=not args.no_tray)
    try:
        app.start()
    except KeyboardInterrupt:
        app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
