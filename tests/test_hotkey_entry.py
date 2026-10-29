"""快捷键输入框测试：验证捕获时修饰键（尤其是 Alt）能被正确识别。

由于自动化环境无法注入真实按键，这里用 Tk 的 event_generate 模拟带 state 的
按键事件，覆盖 event.state 兜底路径；真实按键由 GetKeyState 实时查询保证。

    python tests/test_hotkey_entry.py
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotkey import parse_hotkey  # noqa: E402
from ui import (  # noqa: E402
    HotkeyEntry,
    keysym_to_key_name,
    pressed_key_vk,
    pressed_modifiers,
    vk_to_key_name,
)


class TestPressedModifiers(unittest.TestCase):
    def test_alt_state_bits(self):
        self.assertEqual(pressed_modifiers(0x0008), ["alt"])
        self.assertEqual(pressed_modifiers(0x20000), ["alt"])

    def test_order_and_combinations(self):
        self.assertEqual(pressed_modifiers(0x0004 | 0x0008), ["ctrl", "alt"])
        self.assertEqual(
            pressed_modifiers(0x0004 | 0x0001 | 0x0008 | 0x0040),
            ["ctrl", "alt", "shift", "win"],
        )

    def test_no_modifier(self):
        self.assertEqual(pressed_modifiers(0), [])


class TestKeysymMapping(unittest.TestCase):
    def test_special_keys(self):
        self.assertEqual(keysym_to_key_name("Escape"), "esc")
        self.assertEqual(keysym_to_key_name("semicolon"), ";")
        self.assertEqual(keysym_to_key_name("F1"), "f1")
        self.assertEqual(keysym_to_key_name("Q"), "q")


class FakeEvent:
    """模拟 Tk 按键事件（ttk 组件不响应 event_generate，故直接驱动回调）。"""

    def __init__(self, keysym: str, state: int = 0) -> None:
        self.keysym = keysym
        self.state = state


class TestVirtualKeyMapping(unittest.TestCase):
    """轮询路径使用的虚拟键码映射，必须与 parse_hotkey 兼容。"""

    def test_letters_and_digits(self):
        self.assertEqual(vk_to_key_name(0x51), "q")
        self.assertEqual(vk_to_key_name(0x31), "1")

    def test_function_keys(self):
        self.assertEqual(vk_to_key_name(0x70), "f1")
        self.assertEqual(vk_to_key_name(0x74), "f5")
        self.assertEqual(vk_to_key_name(0x87), "f24")

    def test_special_keys(self):
        self.assertEqual(vk_to_key_name(0x1B), "esc")
        self.assertEqual(vk_to_key_name(0x20), "space")
        self.assertEqual(vk_to_key_name(0x2E), "delete")

    def test_oem_symbols(self):
        self.assertEqual(vk_to_key_name(0xBA), ";")
        self.assertEqual(vk_to_key_name(0xBF), "/")

    def test_mapped_values_are_parseable(self):
        for vk in (0x51, 0x31, 0x70, 0x1B, 0xBA):
            with self.subTest(vk=hex(vk)):
                self.assertIsNotNone(parse_hotkey(vk_to_key_name(vk)))

    def test_unknown_key(self):
        self.assertIsNone(vk_to_key_name(0x01))

    def test_no_key_pressed(self):
        """自动化测试环境没有按键按下，轮询应返回 None。"""
        self.assertIsNone(pressed_key_vk())


class TestHotkeyEntryCapture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.top = tk.Toplevel(cls.root)
        cls.top.geometry("+50+50")
        cls.top.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.top.destroy()
            cls.root.destroy()
        except tk.TclError:
            pass

    def _capture(self, keysym: str, state: int) -> str:
        entry = HotkeyEntry(self.top, "测试", "ctrl+alt+q")
        entry.pack()
        self.top.update()
        entry._capturing = True
        entry._on_key(FakeEvent(keysym, state))
        value = entry.value
        entry.destroy()
        return value

    def test_alt_combo(self):
        self.assertEqual(self._capture("q", 0x0008), "alt+q")

    def test_ctrl_alt_combo(self):
        self.assertEqual(self._capture("w", 0x0004 | 0x0008), "ctrl+alt+w")

    def test_ctrl_function_key(self):
        self.assertEqual(self._capture("F5", 0x0004), "ctrl+f5")

    def test_win_shift_combo(self):
        self.assertEqual(self._capture("s", 0x0001 | 0x0040), "shift+win+s")

    def test_captured_value_is_parseable(self):
        """捕获结果必须能被热键解析器接受，否则保存后无法注册。"""
        for keysym, state in (("q", 0x0008), ("w", 0x0004 | 0x0008), ("F5", 0x0004)):
            value = self._capture(keysym, state)
            with self.subTest(value=value):
                self.assertIsNotNone(parse_hotkey(value))

    def test_modifier_only_is_ignored(self):
        entry = HotkeyEntry(self.top, "测试", "ctrl+alt+q")
        entry.pack()
        self.top.update()
        entry._capturing = True
        entry._on_key(FakeEvent("Alt_L", 0x0008))
        self.top.update()
        self.assertEqual(entry.value, "ctrl+alt+q")  # 只按修饰键不改变原值
        entry.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
