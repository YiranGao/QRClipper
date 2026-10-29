# QRClipper — 屏幕二维码识别小工具

一个 Windows 10 后台常驻的轻量小工具：按快捷键，识别当前屏幕上的二维码，把内容直接复制到剪贴板。

- 常驻系统托盘，不占用任务栏，不弹窗打扰
- **两组可自定义的全局快捷键**：全屏识别 / 框选识别
- 多显示器、高 DPI 屏幕均可用
- 开机自启动可选
- 识别引擎双保险（ZXing-C++ + Zbar），中文二维码不乱码

---

## 快速开始

1. 双击 `dist\QRClipper.exe`（单文件，无需安装 Python）
2. 屏幕右下角托盘区出现二维码图标，程序即进入后台
3. 屏幕上出现二维码时按 **Ctrl + Alt + Q**，内容立刻进入剪贴板，直接 Ctrl+V 粘贴

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl + Alt + Q` | 全屏识别：截取所有显示器画面并识别其中的二维码 |
| `Ctrl + Alt + W` | 框选识别：拖拽框选屏幕区域后再识别（二维码密集时更准） |

识别成功会弹出气泡提示；失败会提示"未识别到二维码"。

## 托盘菜单

右键托盘图标：

- **全屏识别 / 框选识别** —— 与快捷键等效（双击图标 = 全屏识别）
- **设置…** —— 修改快捷键、开关气泡提示与提示音
- **开机自动启动** —— 勾选后写入 `HKCU\...\Run`，下次开机自动常驻
- **退出**

## 修改快捷键

托盘 → 设置 → 点击输入框 → 直接按下新的组合键（如 `Ctrl + Alt + E`）→ 保存。
修改即时生效，无需重启程序。若组合键已被其他软件占用，程序会在日志中记录并跳过该热键。

修饰键（Ctrl / Alt / Shift / Win）通过实时读取键盘状态识别，不依赖 Tk 的按键事件，
因此 `Alt + 字母` 这类会被系统菜单拦截的组合也能正常捕获；符号键（`; / = [ ] \` 等）
按实际虚拟键码处理，设置后可直接生效。

## 命令行参数

```bat
QRClipper.exe --scan       :: 立即全屏识别一次，结果进剪贴板并退出
QRClipper.exe --snip       :: 弹出框选一次，识别后退出
QRClipper.exe --selftest   :: 自检（生成测试二维码验证识别链路，并检查剪贴板读写）
QRClipper.exe --no-tray    :: 不显示托盘图标（常驻但不占托盘）
```

`--scan` 可以挂到别的自动化流程里用；同一时刻只允许一个实例常驻（重复启动会自动退出）。

## 数据与日志

- 配置：`%APPDATA%\QRClipper\config.json`
- 日志：`%APPDATA%\QRClipper\qrclip.log`

配置示例：

```json
{
  "hotkey_full": "ctrl+alt+q",
  "hotkey_snip": "ctrl+alt+w",
  "notify": true,
  "autostart": true,
  "copy_to_clipboard": true,
  "sound": false
}
```

直接改这个文件再重启程序同样生效。

## 源码运行 / 二次打包

```bat
pip install pillow pyzbar zxing-cpp pystray
python qrclip.py            :: 常驻模式
python qrclip.py --selftest :: 自检
python tests\test_smoke.py  :: 单元测试（15 项）
python build.py             :: 用 PyInstaller 打包成 dist\QRClipper.exe
```

> 需要带 tkinter 的 Python（官方安装包默认自带）。打包环境：Python 3.11.9 + PyInstaller 6.22。

## 目录结构

```
QRClipper/
├─ qrclip.py       主程序：托盘、热键调度、命令行模式
├─ config.py       配置读写 + 开机自启动（注册表）
├─ capture.py      Win32 GDI 截图（多显示器虚拟屏幕、高 DPI）
├─ decoder.py      二维码识别（双引擎 + 多尺度重试）
├─ clipboard.py    Win32 剪贴板读写（UTF-16，支持中文/emoji）
├─ hotkey.py       全局热键（RegisterHotKey，无需管理员权限，支持热重载）
├─ ui.py           框选遮罩、设置窗口、托盘图标
├─ build.py        打包脚本
└─ tests/          冒烟测试与 GUI 端到端测试
```

## 常见问题

**识别不出来？**
- 优先试框选识别（`Ctrl+Alt+W`），只框住二维码，成功率最高
- 二维码太小或被缩放模糊时，程序会自动放大重试；仍失败可放大页面后再识别
- 屏幕上有多个二维码时会全部识别，多行内容一起复制到剪贴板

**快捷键没反应？**
- 说明该组合被其他软件抢先注册，换一个组合即可（推荐带 `Win` 或 `Shift` 的组合）
- 查看 `%APPDATA%\QRClipper\qrclip.log` 会有"注册热键失败"记录

**开机自启动没生效？**
- 部分安全软件会拦截注册表 Run 项写入；可手动把 exe 快捷方式放进
  `shell:startup` 目录

**内存占用？**
- 常驻约 25–40 MB，空闲时几乎不占用 CPU，只在按下快捷键的瞬间工作。
