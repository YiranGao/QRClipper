"""QRClipper 主程序。

后台常驻系统托盘，支持两组自定义全局快捷键：
- 全屏识别：截取所有显示器画面，识别其中的二维码并复制到剪贴板；
- 框选识别：拖拽框选屏幕区域后识别该区域。

命令行参数：
    --scan      立即全屏识别一次并退出（可用于命令行/其他程序调用）
    --snip      弹出框选一次并退出
    --selftest  生成测试二维码图片并自检识别链路
    --doctor    输出环境诊断（DPI/坐标/热键/引擎/权限/自启动/剪贴板）
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
import decoder as decoder_module
from decoder import decode_image
from hotkey import HotKeyManager, format_hotkey, parse_hotkey
from notify import Notifier
from ui import SettingsWindow, Snipper, ToastWindow, make_tray_image

ACTION_FULL = "full"
ACTION_SNIP = "snip"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


def is_process_elevated() -> bool:
    """判断当前进程是否以提升权限（管理员）运行。

    高完整性进程写入的剪贴板，普通权限的程序受 UIPI 限制往往读不出来，
    用户会表现为「识别成功但粘贴不到内容」，因此需要在启动时明确提示。
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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
        self._stage = "init"  # 启动阶段标记，供看门狗定位卡点
        self._tk_thread_id = threading.get_ident()

        # 通知是「用户能不能知道发生了什么」的唯一出口，必须与托盘解耦：
        # Notifier 内部按 pystray -> Shell_NotifyIcon -> Tk 浮窗 三级降级。
        self.notifier = Notifier(
            logger=self.logger,
            icon_getter=lambda: self.icon,
            toast=self._toast_on_main,
            enabled=lambda: bool(self.cfg.get("notify", True)),
        )

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """启动顺序的硬原则：**核心功能先起来，诊断/提示永远排在后面**。

        曾经把 _log_environment() 放在最前面，任何一步诊断卡住都会连带
        热键线程和托盘一起起不来，程序表现为"活着但完全无响应"。
        """
        self._start_watchdog()
        self._stage = "autostart"
        self._apply_autostart()

        self._stage = "hotkeys"
        self.hotkeys.start()
        self.hotkeys.wait_ready()
        self._reload_hotkeys()
        # update_bindings 只是投递消息，真正的 RegisterHotKey 稍后才发生；
        # 必须等它执行完再判断，否则每次启动都会误报"没有任何快捷键注册成功"。
        if not self.hotkeys.wait_bindings(2.0):
            self.logger.warning("热键重载超时，注册结果可能不完整")
        self._check_hotkey_registration()

        self._stage = "tray"
        tray_ok = True
        if self.show_tray:
            tray_ok = self._start_tray()

        self._stage = "environment"
        self._log_environment()  # 诊断放最后，且内部整体兜异常

        self._stage = "ready"
        self._warn_if_elevated()
        # 放进事件循环里发：托盘线程刚起来，给它一点时间把自己的图标挂上，
        # 顺带保证浮窗通道所需的主循环已经在跑。
        self.root.after(300, self._announce_startup_safe, tray_ok)
        self.root.mainloop()

    def _announce_startup_safe(self, tray_ok: bool) -> None:
        """定时回调里不能抛异常：windowed 模式下 traceback 没人看得见。"""
        try:
            self._announce_startup(tray_ok)
        except Exception as exc:
            self.logger.error("启动通知发送失败（不影响功能）：%s", exc)

    def _announce_startup(self, tray_ok: bool) -> None:
        """启动完成后弹一条"已启动"，把当前真正生效的快捷键告诉用户。

        用户按快捷键没反应时最需要的就是这条：它能先证明「进程起来了、
        热键注册的是这两个组合」，把"程序挂了"和"热键不是我以为的那个"分开。
        """
        full = format_hotkey(self.cfg.get("hotkey_full"))
        snip = format_hotkey(self.cfg.get("hotkey_snip"))
        if not self.show_tray:
            tray_state = "未启用"
        elif tray_ok:
            tray_state = "已创建"
        else:
            tray_state = "不可用"
        self.logger.info("QRClipper 已启动：全屏=%s 框选=%s（托盘=%s）",
                         full, snip, tray_state)

        active = self.hotkeys.registered_bindings()
        if not active:
            # 全部注册失败属于功能级故障，必须无视 notify 开关告诉用户
            self._notify(
                "QRClipper 已启动，但快捷键不可用",
                "两个快捷键都没能注册成功（可能被其他程序占用）。\n"
                f"请从托盘菜单「设置…」换一组组合，或退出后重新启动。\n"
                f"当前配置：全屏={full}，框选={snip}",
                force=True,
                level=Notifier.LEVEL_WARN,
            )
            return

        hints = "，".join(
            f"{'全屏' if action == ACTION_FULL else '框选'} {format_hotkey(hotkey)}"
            for action, hotkey in active.items()
        )
        if not tray_ok:
            self._notify(
                "QRClipper 已启动（托盘不可用）",
                f"托盘图标没能创建，请用快捷键操作：{hints}",
                force=True,
                level=Notifier.LEVEL_WARN,
            )
            return
        self._notify("QRClipper 已启动", f"快捷键：{hints}")

    def _start_watchdog(self, timeout: float = 10.0) -> None:
        """独立线程看门狗：核心没在预期时间内就绪就记一条明确日志。

        Tk 的 after 在主线程被阻塞时同样不会触发，所以必须用独立线程。
        """
        def _watch() -> None:
            time.sleep(timeout)
            if self._stage == "ready":
                return
            self.logger.warning(
                "启动看门狗：%.0f 秒后仍未就绪，卡在阶段「%s」"
                "（autostart -> hotkeys -> tray -> environment -> ready）",
                timeout, self._stage,
            )

        threading.Thread(target=_watch, name="StartWatchdog", daemon=True).start()

    def _log_environment(self) -> None:
        """把 DPI / 缩放 / 坐标系 / 自启动状态写进日志，便于事后定位。

        整段包在 try/except 里：诊断信息写不出来只影响排查，**绝不能**因此
        中断启动流程。
        """
        try:
            self._log_environment_inner()
        except Exception as exc:
            self.logger.warning("记录环境信息失败（不影响功能）：%s", exc)

    def _log_environment_inner(self) -> None:
        summary = capture.dpi_summary()
        self.logger.info(
            "环境：DPI 感知=%s 系统 DPI=%d（缩放 %d%%） 虚拟屏幕=%s",
            summary["awareness"],
            summary["system_dpi"],
            summary["scale_percent"],
            summary["virtual_screen"],
        )
        self.logger.info("显示器数量：%d", summary["monitor_count"])

        # Tk 的屏幕尺寸必须与物理虚拟屏幕一致，否则框选坐标会整体错位
        tk_size = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        physical = (summary["virtual_screen"][2], summary["virtual_screen"][3])
        if tk_size != physical:
            self.logger.warning(
                "Tk 屏幕尺寸 %dx%d 与物理虚拟屏幕 %dx%d 不一致，框选坐标可能错位",
                tk_size[0], tk_size[1], physical[0], physical[1],
            )

        # 在 _apply_autostart() 之后记录，因此这里是注册表里的最终状态
        autostart = bool(self.cfg.get("autostart"))
        self.logger.info(
            "开机自启动：%s（注册表状态：%s）",
            "已开启" if autostart else "已关闭（重启电脑后不会自动运行，需要手动启动）",
            "已写入" if config_module.is_autostart_enabled() else "无",
        )

        engines = decoder_engines()
        if engines:
            self.logger.info("识别引擎：%s", "、".join(name for name, _fn in engines))
        else:
            self.logger.warning("没有任何可用的二维码识别引擎，识别功能不可用")

    def _check_hotkey_registration(self) -> None:
        """校验热键注册结果，只在**真的**失败时才告警。

        必须在 hotkeys.wait_bindings() 之后调用：否则读到的是重载前的空结果，
        会每次启动都打一条误导性的 WARNING。
        """
        expected = {
            ACTION_FULL: self.cfg.get("hotkey_full", ""),
            ACTION_SNIP: self.cfg.get("hotkey_snip", ""),
        }
        active = self.hotkeys.registered_bindings()
        failed = [action for action in expected if action not in active]

        if failed and len(failed) == len(expected):
            self.logger.warning(
                "没有任何快捷键注册成功（%s），请检查设置与其他程序占用情况",
                "、".join(format_hotkey(expected[action]) for action in failed),
            )
            return
        for action in failed:
            self.logger.warning(
                "快捷键 %s（%s）注册失败，可能被其他程序占用或组合无效",
                action, format_hotkey(expected[action]),
            )
        if active:
            self.logger.info(
                "热键注册完成：%s",
                "，".join(f"{action}={format_hotkey(hotkey)}"
                          for action, hotkey in active.items()),
            )

    def _warn_if_elevated(self) -> None:
        """以管理员权限运行时提示：剪贴板可能因 UIPI 无法被普通程序粘贴。"""
        if not is_process_elevated():
            return
        message = (
            "检测到 QRClipper 正以管理员权限运行。\n"
            "此时写入的剪贴板内容，普通权限的程序可能粘贴不出来（UIPI 限制）。\n"
            "建议退出后以普通方式启动。"
        )
        self.logger.warning("进程处于提升权限状态，剪贴板写入可能被 UIPI 拦截")
        self._notify("请不要以管理员身份运行", message)

    def _start_tray(self) -> bool:
        """创建托盘图标，返回是否成功。

        失败时**不**中断启动：热键才是主路径，托盘只是入口之一。
        """
        try:
            import pystray
        except Exception as exc:  # pragma: no cover
            self.logger.error("托盘库不可用：%s", exc)
            return False

        try:
            self.icon = pystray.Icon(
                "QRClipper",
                make_tray_image(),
                "QRClipper - 屏幕二维码识别",
                menu=pystray.Menu(self._build_tray_menu),
            )
            threading.Thread(target=self.icon.run, name="TrayThread", daemon=True).start()
            return True
        except Exception as exc:
            self.logger.error("托盘图标创建失败（不影响热键）：%s", exc)
            self.icon = None
            return False

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
            self.notifier.close()
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
        """后台执行「截图 -> 识别」，把结果连同尺寸/错误一起交回 Tk 线程。

        三种结局必须能被区分开（这正是用户分不清"程序挂了"还是"没识别到"
        的根源），所以这里把 error 单独收集，而不是统一折叠成空结果：
          - 成功        -> texts 非空
          - 截到但没码  -> texts 空，error 空，size 有值
          - 出错        -> error 非空
        """
        started = time.perf_counter()
        image = None
        texts: List[str] = []
        error = ""
        try:
            image = capture.grab(region) if region else capture.grab_all()
        except Exception as exc:
            error = f"截屏失败：{exc}"
            self.logger.exception("截屏失败：%s", exc)

        if image is not None:
            self.logger.info(
                "截图完成：%dx%d（%s）", image.width, image.height,
                f"区域 {region}" if region else "全屏",
            )
            try:
                texts = decode_image(image)
            except Exception as exc:
                error = f"识别过程出错：{exc}"
                self.logger.exception("识别过程异常：%s", exc)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        size = (image.width, image.height) if image is not None else (0, 0)
        self.root.after(0, lambda: self._present(texts, elapsed_ms, size, error))

    def _present(
        self,
        texts: List[str],
        elapsed_ms: int,
        size: Tuple[int, int] = (0, 0),
        error: str = "",
    ) -> None:
        """统一出口：无论哪种结局都必须给用户一个可见的右下角通知。

        通知在识别流程结束的**同一时刻**发出（不再依赖托盘是否活着），
        并且三种结局的标题各不相同，用户一眼就能分清。
        """
        if error:
            self.logger.error("识别失败（耗时 %d ms）：%s", elapsed_ms, error)
            self._notify(
                "识别失败",
                f"{error}\n请重试一次；若反复失败，可运行 QRClipper.exe --doctor 查看环境诊断。",
                force=True,
                level=Notifier.LEVEL_ERROR,
                duration_ms=7000,
            )
            self._beep(False)
            return

        if not texts:
            self.logger.info("未识别到二维码（耗时 %d ms）", elapsed_ms)
            taken = f"已截取 {size[0]}x{size[1]}，" if size[0] and size[1] else ""
            self._notify(
                "未识别到二维码",
                f"{taken}请确认二维码清晰可见、没有被遮挡或反光；"
                f"屏上二维码较小时，改用「框选识别」({format_hotkey(self.cfg.get('hotkey_snip'))}) 缩小范围。",
                force=True,
                level=Notifier.LEVEL_WARN,
                duration_ms=6000,
            )
            self._beep(False)
            return

        joined = "\n".join(texts)
        copy_enabled = bool(self.cfg.get("copy_to_clipboard", True))
        copied = False
        if copy_enabled:
            copied = clipboard.set_text(joined)
            if copied:
                # 回读校验：剪贴板可能被剪贴板管理器 / 云同步 / 其他程序抢走，
                # 只凭 SetClipboardData 的返回值判断会把「复制失败」误报成成功。
                readback = clipboard.get_text()
                if readback != joined:
                    copied = False
                    self.logger.warning("剪贴板回读校验失败（读到 %r）", (readback or "")[:60])
            if not copied:
                self.logger.warning("写入剪贴板失败")

        self.logger.info("识别成功（%d 个，耗时 %d ms）", len(texts), elapsed_ms)

        preview = texts[0] if len(texts) == 1 else f"{texts[0]}\n…共 {len(texts)} 个"
        if len(preview) > 120:
            preview = preview[:120] + "…"
        if len(texts) > 1:
            preview = f"{preview}\n共 {len(texts)} 个"

        if not copy_enabled:
            self._notify(f"识别到 {len(texts)} 个二维码", preview)
        elif copied:
            title = f"已复制 {len(texts)} 个二维码内容" if len(texts) > 1 else "已复制到剪贴板"
            self._notify(title, preview)
        else:
            # 剪贴板写失败属于故障，不能因为 notify=false 就静默
            self._notify(
                "识别成功，但复制失败",
                "剪贴板可能被其他程序占用或被安全软件拦截。\n"
                "可重试一次，或在设置里关闭「识别结果写入剪贴板」改用提示框查看。",
                force=True,
                level=Notifier.LEVEL_WARN,
            )
        self._beep(copied or not copy_enabled)

    def _toast_on_main(self, title: str, message: str, duration_ms: int = 4000) -> None:
        """把 Tk 浮窗切到主线程创建（Tk 对象只能在创建它的线程操作）。"""
        def _show() -> None:
            ToastWindow(self.root, title, message, duration_ms=duration_ms)

        if threading.get_ident() == self._tk_thread_id:
            _show()
        else:
            self.root.after(0, _show)

    def _notify(
        self,
        title: str,
        message: str,
        force: bool = False,
        level: str = Notifier.LEVEL_INFO,
        duration_ms: int = 4000,
    ) -> str:
        """统一通知入口，返回实际使用的通道名。

        Args:
            force: 失败/无结果类通知必须为 True——用户关掉 notify 只是不想被
                "已复制"这种成功提示打扰，而不是想在出问题时什么都看不到。
        """
        return self.notifier.send(
            title, message, force=force, level=level, duration_ms=duration_ms
        )

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
            self._notify(
                "快捷键无效",
                "已恢复为原来的设置，请重新录入。",
                force=True,
                level=Notifier.LEVEL_WARN,
            )
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


def run_doctor() -> int:
    """输出环境诊断：DPI / 坐标 / 热键 / 引擎 / 权限 / 自启动 / 剪贴板。

    用于定位「快捷键没反应」「框选位置不对」「识别不到」「复制不到」这类
    与机器环境强相关的问题。
    """
    capture.enable_dpi_awareness()
    setup_logging()  # _emit 会把报告同时写进日志，打包后靠它留痕
    report: List[str] = []

    def emit(text: str) -> None:
        report.append(text)
        _emit(text)

    emit("===== QRClipper 环境诊断 =====")
    summary = capture.dpi_summary()
    emit(f"DPI 感知级别    : {summary['awareness']}")
    emit(f"系统 DPI / 缩放 : {summary['system_dpi']} / {summary['scale_percent']}%")
    emit(f"虚拟屏幕(物理)  : {summary['virtual_screen']}")
    emit(f"显示器数量      : {summary['monitor_count']}")

    try:
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        tk_size = (root.winfo_screenwidth(), root.winfo_screenheight())
        physical = (summary["virtual_screen"][2], summary["virtual_screen"][3])
        same = "一致" if tk_size == physical else "*** 与物理屏幕不一致，框选会错位 ***"
        emit(f"Tk 屏幕尺寸     : {tk_size} -> {same}")
        cursor = capture.cursor_pos()
        emit(f"鼠标 物理 / Tk  : {cursor} / {(root.winfo_pointerx(), root.winfo_pointery())}")
        root.destroy()
    except Exception as exc:
        emit(f"Tk 坐标检查失败 : {exc}")

    cfg = Config()
    bindings = {
        ACTION_FULL: cfg.get("hotkey_full", ""),
        ACTION_SNIP: cfg.get("hotkey_snip", ""),
    }
    manager = HotKeyManager(lambda _action: None)
    manager.start()
    manager.wait_ready(3.0)
    manager.update_bindings(bindings)
    time.sleep(0.5)
    active = manager.registered_bindings()
    for action_id, hotkey in bindings.items():
        ok = action_id in active
        emit(
            f"热键 {action_id:<5}     : {format_hotkey(hotkey)} -> "
            f"{'注册成功' if ok else '*** 注册失败，可能被其他程序占用 ***'}"
        )
    manager.stop()

    engines = [name for name, _fn in decoder_engines()]
    emit(f"识别引擎        : {', '.join(engines) if engines else '*** 无可用引擎 ***'}")
    emit(f"管理员权限      : {'是（剪贴板可能被 UIPI 拦截）' if is_process_elevated() else '否'}")
    emit(
        f"开机自启动      : {'已开启' if cfg.get('autostart') else '已关闭'}"
        f"（注册表状态：{'已写入' if config_module.is_autostart_enabled() else '无'}）"
    )

    probe = "QRClipper-doctor-" + time.strftime("%H%M%S")
    clipboard_ok = clipboard.set_text(probe) and clipboard.verify_text(probe)
    emit(f"剪贴板 写/读    : {'正常' if clipboard_ok else '*** 校验失败 ***'}")

    started = time.perf_counter()
    try:
        image = capture.grab_all()
        texts = decode_image(image)
        grab_ms = int((time.perf_counter() - started) * 1000)
        emit(f"全屏截图+识别   : {image.width}x{image.height}，{grab_ms} ms，命中 {len(texts)} 个")
    except Exception as exc:
        emit(f"全屏截图失败    : {exc}")

    emit("===== 诊断结束 =====")

    # 打包成 windowed 后没有控制台：报告一定要落盘，不能只靠弹窗
    try:
        report_path = os.path.join(config_module.app_dir(), "doctor_result.txt")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(report) + "\n")
        emit(f"报告已写入：{report_path}")
    except OSError as exc:
        report_path = ""
        emit(f"报告写入失败：{exc}")

    if sys.stdout is None or getattr(sys, "frozen", False):
        _show_message("QRClipper 环境诊断", "\n".join(report))
    return 0


def decoder_engines() -> List[tuple]:
    """返回可用的识别引擎列表 [(名称, 函数)]，供诊断与测试复用。"""
    return decoder_module._load_engines()


def _notify_already_running(message: str, duration_ms: int = 6000) -> None:
    """非阻塞地提示"已在运行中"：用自动关闭的 Tk 浮窗，不用模态 MessageBox。

    浮窗几秒后自动销毁并退出 mainloop，进程随即结束、释放互斥体句柄；
    任何异常都吞掉，保证这条分支一定会走到 return，不留下挂住的进程。
    """
    try:
        root = tk.Tk()
        root.withdraw()
        ToastWindow(root, "QRClipper 已在运行中", message, duration_ms=duration_ms)
        root.after(duration_ms + 500, root.destroy)
        root.mainloop()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="屏幕二维码识别并复制到剪贴板")
    parser.add_argument("--scan", action="store_true", help="全屏识别一次后退出")
    parser.add_argument("--snip", action="store_true", help="框选识别一次后退出")
    parser.add_argument("--selftest", action="store_true", help="运行识别链路自检")
    parser.add_argument("--doctor", action="store_true", help="输出环境诊断信息")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    if args.selftest:
        return run_selftest()
    if args.doctor:
        return run_doctor()
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
        # 打包成 windowed 程序后没有控制台，只写 stderr 等于毫无反馈，
        # 用户会以为「双击没反应 / 程序没起来」，所以用浮窗提示后再退出。
        # 注意：**绝不能**用 MessageBox——它是模态的，没人点就永远不返回，
        # 这个进程会一直挂着并占住互斥体句柄，之后每次启动都会被判成
        # "重复启动"，程序表现为彻底无响应。
        message = (
            "QRClipper 已经在运行中，无需重复启动。\n"
            "托盘图标可能被 Windows 折叠进了任务栏右下角的隐藏区域，"
            "点开即可看到；若热键无反应，请先从托盘退出后重新启动。"
        )
        print(message, file=sys.stderr)
        setup_logging().info("重复启动被拒绝：已有实例在运行")
        _notify_already_running(message)
        return 0

    app = App(show_tray=not args.no_tray)
    try:
        app.start()
    except KeyboardInterrupt:
        app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
