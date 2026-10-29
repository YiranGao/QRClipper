"""打包脚本：把 QRClipper 打包成单个 exe（无需安装 Python 即可运行）。

    python build.py

产物：dist\\QRClipper.exe
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys

import pyzbar
from ui import make_tray_image

NAME = "QRClipper"


def build_icon() -> str:
    """生成托盘/可执行文件图标（不依赖外部素材）。"""
    os.makedirs("assets", exist_ok=True)
    path = os.path.abspath(os.path.join("assets", "tray.ico"))
    image = make_tray_image(256)
    image.save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def main() -> int:
    icon_path = build_icon()
    pyzbar_dir = os.path.dirname(pyzbar.__file__)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        NAME,
        "--icon",
        icon_path,
        "--collect-submodules",
        "pyzbar",
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "unittest",
    ]

    # pyzbar 依赖的原生 DLL 必须一起打包（放到 pyzbar 包目录内）
    for dll in glob.glob(os.path.join(pyzbar_dir, "*.dll")):
        command += ["--add-binary", f"{dll};pyzbar"]

    command.append("qrclip.py")
    print("打包命令：", " ".join(command))
    code = subprocess.call(command)

    exe = os.path.abspath(os.path.join("dist", f"{NAME}.exe"))
    if code == 0 and os.path.exists(exe):
        size_mb = os.path.getsize(exe) / 1024 / 1024
        print(f"打包完成：{exe}（{size_mb:.1f} MB）")
    else:
        print("打包失败")
    return code


if __name__ == "__main__":
    sys.exit(main())
