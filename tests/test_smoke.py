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


class TestClipboard(unittest.TestCase):
    def test_write_and_read(self):
        probe = f"QRClipper-{time.time()}"
        self.assertTrue(clipboard.set_text(probe))
        self.assertEqual(clipboard.get_text(), probe)

    def test_unicode(self):
        probe = "中文✅ emoji 🚀"
        self.assertTrue(clipboard.set_text(probe))
        self.assertEqual(clipboard.get_text(), probe)


if __name__ == "__main__":
    unittest.main(verbosity=2)
