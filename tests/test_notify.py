"""通知降级链路的回归测试。

需求背景：用户完全分不清「程序挂了」和「没识别到」，因为托盘没起来时
`icon.notify()` 什么都弹不出来。

通道顺序经真机实测后定为：

    Tk 浮窗 -> pystray -> Shell_NotifyIcon 气泡

原生两个通道在打包成 exe 后会**静默失败**（返回成功但屏幕无变化），
所以只有自绘浮窗是"一定看得见"的保证，必须排在第一位。这里把顺序和
每一级的可用/不可用组合都固定下来，防止以后重构又把"图标没起就不提示"
这种死路写回去。
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
import notify as notify_module  # noqa: E402
import qrclip  # noqa: E402
from notify import Notifier  # noqa: E402


def _quiet_logger() -> logging.Logger:
    logger = logging.getLogger("qrclip.tests.notify")
    logger.handlers = [logging.NullHandler()]
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    return logger


class _FakeIcon:
    def __init__(self, visible: bool = True, raises: bool = False) -> None:
        self.visible = visible
        self.raises = raises
        self.calls: List[Tuple[str, str]] = []

    def notify(self, message: str, title: str) -> None:
        if self.raises:
            raise RuntimeError("pystray notify 不可用")
        self.calls.append((title, message))


class _RecordingNotifier(Notifier):
    """用可控的假实现替换三条通道，专注验证降级**顺序**。"""

    def __init__(self, toast_ok: bool, pystray_ok: bool, balloon_ok: bool) -> None:
        super().__init__(
            logger=_quiet_logger(),
            icon_getter=lambda: None,
            toast=lambda title, message, duration_ms=4000: None,
            enabled=lambda: True,
        )
        self._toast_ok = toast_ok
        self._pystray_ok = pystray_ok
        self._balloon_ok = balloon_ok
        self.order: List[str] = []

    def _try_toast(self, title: str, message: str, duration_ms: int = 4000) -> bool:
        self.order.append("toast")
        return self._toast_ok

    def _try_pystray(self, title: str, message: str) -> bool:
        self.order.append("pystray")
        return self._pystray_ok

    def _try_balloon(self, title: str, message: str, level: str) -> bool:
        self.order.append("systray")
        return self._balloon_ok


class TestNotifierFallbackChain(unittest.TestCase):
    """降级顺序与返回值。"""

    def test_toast_is_preferred_when_available(self) -> None:
        """浮窗是唯一可确认可见的通道，必须在最前面。"""
        notifier = _RecordingNotifier(True, True, True)
        self.assertEqual(notifier.send("t", "m"), "toast")
        self.assertEqual(notifier.order, ["toast"])

    def test_falls_back_to_pystray_when_no_tk_root(self) -> None:
        """没有 Tk 根窗口（--scan 之类）时才用 pystray。"""
        notifier = _RecordingNotifier(False, True, True)
        self.assertEqual(notifier.send("t", "m"), "pystray")
        self.assertEqual(notifier.order, ["toast", "pystray"])

    def test_falls_back_to_balloon_when_no_tray_at_all(self) -> None:
        notifier = _RecordingNotifier(False, False, True)
        self.assertEqual(notifier.send("t", "m"), "systray")
        self.assertEqual(notifier.order, ["toast", "pystray", "systray"])

    def test_reports_none_when_every_channel_fails(self) -> None:
        notifier = _RecordingNotifier(False, False, False)
        self.assertEqual(notifier.send("t", "m"), "none")
        self.assertEqual(notifier.last_channel(), "none")

    def test_last_channel_tracks_real_channel(self) -> None:
        notifier = _RecordingNotifier(False, True, True)
        notifier.send("t", "m")
        self.assertEqual(notifier.last_channel(), "pystray")

    def test_force_bypasses_disabled_config(self) -> None:
        """notify=false 只压制成功提示；失败/无结果必须 force 穿透。"""
        notifier = _RecordingNotifier(True, True, True)
        notifier._enabled = lambda: False
        self.assertEqual(notifier.send("已复制到剪贴板", "x"), "skipped")
        self.assertEqual(notifier.order, [])
        self.assertEqual(notifier.send("未识别到二维码", "x", force=True), "toast")
        self.assertEqual(notifier.order, ["toast"])

    def test_level_mapping_covers_three_levels(self) -> None:
        for level in (Notifier.LEVEL_INFO, Notifier.LEVEL_WARN, Notifier.LEVEL_ERROR):
            self.assertIn(level, Notifier._LEVEL_FLAGS)
            self.assertIn(level, Notifier._LEVEL_ICONS)

    def test_duration_is_passed_to_toast(self) -> None:
        """停留时长要能按严重程度调整：报错文案更长，需要看得更久。"""
        seen: List[int] = []

        def toast(title: str, message: str, duration_ms: int = 4000) -> None:
            seen.append(duration_ms)

        notifier = Notifier(
            logger=_quiet_logger(),
            icon_getter=lambda: None,
            toast=toast,
            enabled=lambda: True,
        )
        notifier.send("t", "m", duration_ms=7000)
        self.assertEqual(seen, [7000])


class TestNotifierPystrayChannel(unittest.TestCase):
    """通道 2 的真实判定条件：图标存在 **且** 可见。"""

    def _notifier(self, icon: Optional[object], enabled: bool = True) -> Notifier:
        return Notifier(
            logger=_quiet_logger(),
            icon_getter=lambda: icon,
            toast=lambda title, message, duration_ms=4000: (_ for _ in ()).throw(
                RuntimeError("no tk")
            ),
            enabled=lambda: enabled,
        )

    def test_missing_icon_degrades(self) -> None:
        self.assertFalse(self._notifier(None)._try_pystray("t", "m"))

    def test_invisible_icon_degrades(self) -> None:
        """pystray 的 icon 对象存在但托盘没真正起来时 visible=False。"""
        self.assertFalse(self._notifier(_FakeIcon(visible=False))._try_pystray("t", "m"))

    def test_visible_icon_is_used(self) -> None:
        icon = _FakeIcon(visible=True)
        self.assertTrue(self._notifier(icon)._try_pystray("标题", "正文"))
        self.assertEqual(icon.calls, [("标题", "正文")])

    def test_icon_exception_degrades_instead_of_raising(self) -> None:
        self.assertFalse(self._notifier(_FakeIcon(raises=True))._try_pystray("t", "m"))

    def test_send_falls_through_when_icon_raises(self) -> None:
        notifier = self._notifier(_FakeIcon(visible=True, raises=True))
        notifier._try_balloon = lambda *a, **kw: True  # type: ignore[assignment]
        self.assertEqual(notifier.send("t", "m"), "systray")


class TestNotifyIconDataStruct(unittest.TestCase):
    """NOTIFYICONDATAW 的布局必须能被 Shell 按 V2 解析。"""

    def test_cbsize_matches_struct(self) -> None:
        data = notify_module.NOTIFYICONDATAW()
        data.cbSize = 952
        self.assertEqual(
            data.cbSize,
            notify_module.ctypes.sizeof(notify_module.NOTIFYICONDATAW),
        )

    def test_text_fields_truncate_safely(self) -> None:
        data = notify_module.NOTIFYICONDATAW()
        data.szInfoTitle = "标" * (notify_module._TITLE_MAX - 1)
        data.szInfo = "文" * (notify_module._INFO_MAX - 1)
        self.assertEqual(len(data.szInfoTitle), notify_module._TITLE_MAX - 1)
        self.assertEqual(len(data.szInfo), notify_module._INFO_MAX - 1)

    def test_balloon_reports_failure_without_raising(self) -> None:
        """Shell_NotifyIcon 失败时必须返回 False 而不是抛出去。"""
        notifier = Notifier(
            logger=_quiet_logger(),
            icon_getter=lambda: None,
            toast=lambda title, message, duration_ms=4000: None,
            enabled=lambda: True,
        )
        notifier._ensure_icon = lambda: True  # type: ignore[assignment]
        notifier._hwnd = 0
        original = notify_module.shell32.Shell_NotifyIconW
        notify_module.shell32.Shell_NotifyIconW = lambda *_a: 0  # type: ignore[assignment]
        try:
            self.assertFalse(notifier._try_balloon("t", "m", Notifier.LEVEL_INFO))
        finally:
            notify_module.shell32.Shell_NotifyIconW = original  # type: ignore[assignment]


class _PresentStub:
    """只为调用 App._present 而存在的最小依赖集合。"""

    def __init__(self, notify_enabled: bool = True, copy_enabled: bool = True) -> None:
        self.cfg = {"notify": notify_enabled, "copy_to_clipboard": copy_enabled,
                    "sound": False, "hotkey_snip": "ctrl+alt+e"}
        self.logger = _quiet_logger()
        self.sent: List[Tuple[str, str, bool]] = []
        self.durations: List[int] = []
        self.beeps: List[bool] = []

    def _notify(self, title: str, message: str, force: bool = False,
                level: str = Notifier.LEVEL_INFO, duration_ms: int = 4000) -> str:
        self.sent.append((title, message, force))
        self.durations.append(duration_ms)
        return "toast"

    def _beep(self, success: bool) -> None:
        self.beeps.append(success)


class TestPresentOutcomes(unittest.TestCase):
    """三种结局都必须产生一条可见通知，且标题互不相同。"""

    def _present(self, stub: _PresentStub, **kwargs) -> None:
        qrclip.App._present(
            stub,  # type: ignore[arg-type]
            kwargs.get("texts", []),
            kwargs.get("elapsed_ms", 120),
            kwargs.get("size", (3840, 2160)),
            kwargs.get("error", ""),
        )

    def test_not_recognized_notifies_with_image_size(self) -> None:
        stub = _PresentStub()
        self._present(stub, texts=[], size=(3840, 2160))
        self.assertEqual(len(stub.sent), 1)
        title, message, force = stub.sent[0]
        self.assertEqual(title, "未识别到二维码")
        self.assertIn("已截取 3840x2160", message)
        self.assertTrue(force, "无结果必须能穿透 notify=false")
        self.assertEqual(stub.beeps, [False])

    def test_not_recognized_still_notifies_when_notify_disabled(self) -> None:
        """notify=false 时用户按了快捷键，也必须知道"截到了但没码"。"""
        stub = _PresentStub(notify_enabled=False)
        self._present(stub, texts=[])
        self.assertEqual(len(stub.sent), 1)
        self.assertTrue(stub.sent[0][2])

    def test_recognition_error_is_distinct_from_not_found(self) -> None:
        stub = _PresentStub()
        self._present(stub, texts=[], error="截屏失败：句柄无效")
        title, message, force = stub.sent[0]
        self.assertEqual(title, "识别失败")
        self.assertIn("截屏失败", message)
        self.assertTrue(force)

    def test_success_uses_paste_ready_title(self) -> None:
        stub = _PresentStub(copy_enabled=False)
        self._present(stub, texts=["https://workbuddy.cn/docs"])
        title, message, force = stub.sent[0]
        self.assertEqual(title, "识别到 1 个二维码")
        self.assertIn("https://workbuddy.cn/docs", message)
        self.assertFalse(force, "成功提示可以尊重 notify=false")

    def test_multi_result_mentions_count(self) -> None:
        stub = _PresentStub(copy_enabled=False)
        self._present(stub, texts=["a", "b", "c"])
        title, message, _force = stub.sent[0]
        self.assertEqual(title, "识别到 3 个二维码")
        self.assertIn("共 3 个", message)

    def test_titles_of_three_outcomes_are_all_different(self) -> None:
        not_found = _PresentStub()
        self._present(not_found, texts=[])
        failed = _PresentStub()
        self._present(failed, texts=[], error="识别过程出错：engine boom")
        ok = _PresentStub(copy_enabled=False)
        self._present(ok, texts=["x"])
        titles = {not_found.sent[0][0], failed.sent[0][0], ok.sent[0][0]}
        self.assertEqual(len(titles), 3, f"三种结局标题必须可区分，实际：{titles}")

    def test_failure_notices_stay_on_screen_longer(self) -> None:
        """失败/无结果的文案更长，停留时间必须比成功提示久，否则用户没看完就没了。"""
        not_found = _PresentStub()
        self._present(not_found, texts=[])
        ok = _PresentStub(copy_enabled=False)
        self._present(ok, texts=["x"])
        self.assertGreater(not_found.durations[0], 4000)
        self.assertGreater(not_found.durations[0], 4000)
        self.assertEqual(ok.durations[0], 4000)


class TestNotifierWiring(unittest.TestCase):
    """App 侧接线：通知必须与托盘解耦，且 force 语义固定下来。"""

    def test_toast_callback_marshals_to_tk_thread(self) -> None:
        scheduled: List[object] = []

        class _Root:
            def after(self, _delay, callback):
                scheduled.append(callback)

        class _App:
            _tk_thread_id = -1  # 与当前线程必然不同，强制走 after 分支
            root = _Root()

            _toast_on_main = qrclip.App._toast_on_main

        _App._toast_on_main(_App(), "t", "m")  # type: ignore[arg-type]
        self.assertEqual(len(scheduled), 1)
        self.assertTrue(callable(scheduled[0]))

    def test_config_exposes_notify_default(self) -> None:
        """notify 开关必须来自 config 且有默认值，改配置立即生效。"""
        self.assertIn("notify", config_module.DEFAULT_CONFIG)
        self.assertTrue(config_module.DEFAULT_CONFIG["notify"])
        self.assertTrue(hasattr(qrclip.App, "_announce_startup"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
