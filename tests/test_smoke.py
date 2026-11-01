"""QRClipper 冒烟测试：不依赖图形界面，可在命令行直接运行。

    python tests/test_smoke.py

覆盖：快捷键解析、配置读写、二维码识别（多引擎多尺度）、剪贴板读写。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capture  # noqa: E402
import clipboard  # noqa: E402
import config as config_module  # noqa: E402
from decoder import decode_image  # noqa: E402
from hotkey import format_hotkey, parse_hotkey  # noqa: E402
from PIL import Image  # noqa: E402


class TestHotkeyParsing(unittest.TestCase):
    def test_common_combinations(self):
        cases = {
            "ctrl+alt+q": (0x0002 | 0x0001, ord("Q")),
            "ctrl+shift+f1": (0x0002 | 0x0004, 0x70),
            "win+alt+s": (0x0008 | 0x0001, ord("S")),
            "f5": (0, 0x74),
            "ctrl+1": (0x0002, ord("1")),
            "ctrl+;": (0x0002, 0xBA),  # 符号键：VK 不等于 ASCII
            "alt+/": (0x0001, 0xBF),
            "ctrl+alt+[": (0x0002 | 0x0001, 0xDB),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_hotkey(text), expected)

    def test_invalid_input(self):
        for text in ("", "ctrl", "ctrl+", "ctrl+nonexistentkey"):
            with self.subTest(text=text):
                self.assertIsNone(parse_hotkey(text))

    def test_format(self):
        self.assertEqual(format_hotkey("ctrl+alt+q"), "Ctrl+Alt+Q")
        self.assertEqual(format_hotkey("win+shift+f5"), "Shift+Win+F5")
        self.assertEqual(format_hotkey("ctrl+;"), "Ctrl+;")
        self.assertEqual(format_hotkey("alt+esc"), "Alt+Esc")


class TestConfig(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        cfg = config_module.Config()
        cfg.load()
        self.assertIn("hotkey_full", cfg.as_dict())
        original = cfg.get("hotkey_full")
        cfg.set("hotkey_full", "ctrl+alt+z", autosave=False)
        self.assertEqual(cfg.get("hotkey_full"), "ctrl+alt+z")
        cfg.set("hotkey_full", original, autosave=False)

    def test_autostart_command_contains_entry(self):
        command = config_module.autostart_command()
        self.assertTrue(command.startswith('"'))


class TestDecoder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import qrcode

        cls.qrcode = qrcode
        cls.tmp_dir = tempfile.mkdtemp(prefix="qrclip_test_")

    def _make_qr(self, content: str, box_size: int = 8) -> Image.Image:
        qr = self.qrcode.QRCode(box_size=box_size, border=4)
        qr.add_data(content)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB")

    def test_plain_url(self):
        image = self._make_qr("https://workbuddy.cn")
        self.assertEqual(decode_image(image), ["https://workbuddy.cn"])

    def test_chinese_text(self):
        image = self._make_qr("你好，屏幕二维码识别")
        self.assertEqual(decode_image(image), ["你好，屏幕二维码识别"])

    def test_qr_on_large_background(self):
        """模拟真实场景：小二维码贴在整屏背景上。"""
        qr = self._make_qr("ScreenQR-12345", box_size=4)
        background = Image.new("RGB", (1600, 900), (235, 235, 235))
        background.paste(qr, (640, 300))
        self.assertEqual(decode_image(background), ["ScreenQR-12345"])

    def test_small_qr_needs_upscale(self):
        """屏幕上很小的二维码（模块仅 2px）也应识别成功。"""
        qr = self._make_qr("tiny", box_size=2)
        background = Image.new("RGB", (800, 600), (255, 255, 255))
        background.paste(qr, (100, 100))
        results = decode_image(background)
        self.assertIn("tiny", results)

    def test_no_qr_returns_empty(self):
        background = Image.new("RGB", (300, 300), (255, 255, 255))
        self.assertEqual(decode_image(background), [])


class TestCapture(unittest.TestCase):
    def test_virtual_screen_and_grab(self):
        rect = capture.virtual_screen_rect()
        self.assertGreaterEqual(len(rect), 4)
        self.assertGreater(rect[2], 0)
        self.assertGreater(rect[3], 0)

        image = capture.grab_all()
        self.assertEqual(image.mode, "RGB")
        # 截图尺寸应与虚拟屏幕一致（允许 1px 误差）
        self.assertAlmostEqual(image.width, rect[2], delta=2)
        self.assertAlmostEqual(image.height, rect[3], delta=2)

    def test_region_grab(self):
        left, top, _, _ = capture.virtual_screen_rect()
        image = capture.grab((left + 10, top + 10, left + 110, top + 110))
        self.assertEqual(image.size, (100, 100))

    def test_normalize_region_rejects_tiny(self):
        with self.assertRaises(ValueError):
            capture.normalize_region(10, 10, 12, 12)


class TestDpi(unittest.TestCase):
    """DPI 感知与坐标系：框选坐标错位的根因排查面。"""

    def test_enable_dpi_awareness_reports_real_level(self):
        """返回值必须是真实生效的感知级别，不能谎报成功。"""
        level = capture.enable_dpi_awareness()
        self.assertIn(
            level,
            ("PER_MONITOR_AWARE", "PER_MONITOR_AWARE_V2", "SYSTEM_AWARE", "UNAWARE", "UNKNOWN"),
        )
        # 声明过之后再次查询，结果应当已知且非 UNKNOWN
        self.assertNotEqual(capture.current_dpi_awareness(), "UNKNOWN")

    def test_virtual_screen_matches_system_metrics(self):
        left, top, width, height = capture.virtual_screen_rect()
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        summary = capture.dpi_summary()
        self.assertEqual(summary["virtual_screen"], (left, top, width, height))
        self.assertGreater(summary["system_dpi"], 0)
        self.assertIsInstance(summary["monitors"], list)

    def test_cursor_pos_is_physical(self):
        """GetCursorPos 的结果必须落在虚拟屏幕范围内（物理像素口径）。"""
        position = capture.cursor_pos()
        if position is None:  # 无会话环境（如服务账户）允许拿不到
            self.skipTest("当前环境无法获取鼠标位置")
        x, y = position
        left, top, width, height = capture.virtual_screen_rect()
        self.assertGreaterEqual(x, left)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(x, left + width)
        self.assertLessEqual(y, top + height)


class TestHotkeyManagerReload(unittest.TestCase):
    """热键重载是异步的：判断注册结果前必须先 wait_bindings()。

    曾经的 bug：update_bindings() 只投递 WM_APP_RELOAD，调用方立刻读
    registered_bindings() 拿到空字典，于是每次启动都误报
    「没有任何快捷键注册成功」，反而误导正在排查热键问题的用户。
    """

    @staticmethod
    def _manager(bindings):
        from hotkey import HotKeyManager

        manager = HotKeyManager(lambda _action: None)
        manager.start()
        return manager

    def test_wait_bindings_then_all_registered(self):
        bindings = {"full": "ctrl+alt+shift+f15", "snip": "ctrl+alt+shift+f16"}
        manager = self._manager(bindings)
        try:
            self.assertTrue(manager.wait_ready(3.0))
            manager.update_bindings(bindings)
            self.assertTrue(manager.wait_bindings(2.0))
            self.assertEqual(manager.registered_bindings(), bindings)
        finally:
            manager.stop()

    def test_invalid_binding_is_reported_after_wait(self):
        """组合非法时 wait_bindings 仍要返回，且该项确实不在注册结果里。"""
        bindings = {"full": "ctrl+nonexistentkey", "snip": "ctrl+alt+shift+f17"}
        manager = self._manager(bindings)
        try:
            self.assertTrue(manager.wait_ready(3.0))
            manager.update_bindings(bindings)
            self.assertTrue(manager.wait_bindings(2.0))
            active = manager.registered_bindings()
            self.assertNotIn("full", active)
            self.assertIn("snip", active)
        finally:
            manager.stop()

    def test_wait_bindings_without_update_returns_immediately(self):
        manager = self._manager({})
        try:
            self.assertTrue(manager.wait_ready(3.0))
            self.assertTrue(manager.wait_bindings(0.1))
            self.assertEqual(manager.registered_bindings(), {})
        finally:
            manager.stop()


class TestClipboard(unittest.TestCase):
    def test_write_and_read(self):
        probe = f"QRClipper-{time.time()}"
        self.assertTrue(clipboard.set_text(probe))
        self.assertEqual(clipboard.get_text(), probe)

    def test_unicode(self):
        probe = "中文✅ emoji 🚀"
        self.assertTrue(clipboard.set_text(probe))
        self.assertEqual(clipboard.get_text(), probe)

    def test_verify_text_after_write(self):
        """写入后回读校验：刚写完立刻回读也必须拿到内容（旧实现会误报失败）。"""
        probe = f"VERIFY-{time.time()}"
        self.assertTrue(clipboard.set_text(probe))
        self.assertTrue(clipboard.verify_text(probe))

    def test_verify_text_detects_mismatch(self):
        self.assertTrue(clipboard.set_text("SENTINEL-OK"))
        self.assertFalse(clipboard.verify_text("SOMETHING-ELSE"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
