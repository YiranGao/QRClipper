"""设置保存 -> 热键热重载 链路测试。

模拟用户在设置窗口里把快捷键改成含 Alt 的组合，验证保存后热键确实重新注册成功。
测试结束会还原原有配置（包括开机自启动注册表项）。

    python tests/test_settings_reload.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
import qrclip  # noqa: E402

NEW_BINDINGS = {
    "hotkey_full": "alt+k",
    "hotkey_snip": "ctrl+shift+;",  # 同时覆盖 Alt 组合与符号键
}


def main() -> int:
    app = qrclip.App(show_tray=False)
    app.root.title("QRClipper 设置重载测试")

    original = app.cfg.as_dict()
    original_autostart = config_module.is_autostart_enabled()
    result = {"bindings": {}, "ok": False, "restored": False}

    def step_apply():
        values = dict(original)
        values.update(NEW_BINDINGS)
        values["autostart"] = False  # 测试期间不要动自启动，最后统一还原
        app._save_settings(values)
        app.root.after(600, step_check)

    def step_check():
        result["bindings"] = app.hotkeys.registered_bindings()
        result["ok"] = (
            result["bindings"].get("full") == NEW_BINDINGS["hotkey_full"]
            and result["bindings"].get("snip") == NEW_BINDINGS["hotkey_snip"]
        )
        app.root.after(100, step_restore)

    def step_restore():
        # 还原配置与注册表
        for key, value in original.items():
            app.cfg.set(key, value, autosave=False)
        app.cfg.save()
        config_module.set_autostart(original_autostart)
        app._reload_hotkeys()
        app.root.after(500, step_done)

    def step_done():
        result["restored_bindings"] = app.hotkeys.registered_bindings()
        result["restored"] = result["restored_bindings"].get("full") == original["hotkey_full"]

        print("修改后实际注册:", result["bindings"])
        print("还原后实际注册:", result["restored_bindings"])
        print("修改生效:", result["ok"], "| 配置已还原:", result["restored"])
        passed = bool(result["ok"]) and bool(result["restored"])
        print("结果:", "PASS" if passed else "FAIL")
        app.quit()
        sys.exit(0 if passed else 1)

    app.root.after(800, step_apply)
    app.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
