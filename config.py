"""QRClipper 配置管理。

配置保存在 %APPDATA%\\QRClipper\\config.json，同时负责开机自启动（注册表 Run 项）
与运行日志。所有键都有默认值，配置文件损坏时自动回退默认值。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict

APP_NAME = "QRClipper"
RUN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_CONFIG: Dict[str, Any] = {
    # 全屏识别：截取所有显示器画面并识别其中的二维码
    "hotkey_full": "ctrl+alt+q",
    # 框选识别：拖拽选择屏幕区域后再识别
    "hotkey_snip": "ctrl+alt+w",
    # 识别完成后是否弹出气泡提示
    "notify": True,
    # 开机自启动
    "autostart": True,
    # 识别成功后是否写入剪贴板（关闭则只在通知里显示）
    "copy_to_clipboard": True,
    # 识别失败/成功时的提示音
    "sound": False,
}


def app_dir() -> str:
    """返回数据目录（%APPDATA%\\QRClipper），不存在则创建。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def log_path() -> str:
    return os.path.join(app_dir(), "qrclip.log")


def setup_logging() -> logging.Logger:
    """配置日志：同时写文件与（有控制台时）控制台。"""
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    file_handler = logging.FileHandler(log_path(), encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if not getattr(sys, "frozen", False):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        logger.addHandler(stream)
    return logger


class Config:
    """读写 JSON 配置，缺失或损坏的键自动用默认值补齐。"""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        try:
            with open(config_path(), "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if key in DEFAULT_CONFIG:
                        self._data[key] = value
        except FileNotFoundError:
            self.save()
        except (json.JSONDecodeError, OSError):
            self._data = dict(DEFAULT_CONFIG)

    def save(self) -> None:
        try:
            with open(config_path(), "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            return self._data[key]
        return DEFAULT_CONFIG.get(key, default)

    def set(self, key: str, value: Any, autosave: bool = True) -> None:
        self._data[key] = value
        if autosave:
            self.save()

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)


# ---------- 开机自启动 ----------
def autostart_command() -> str:
    """返回用于自启动的完整命令行（区分打包 exe 与源码运行）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qrclip.py")
    return f'"{pythonw}" "{entry}"'


def is_autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """写入或删除 HKCU Run 项。返回是否成功。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
