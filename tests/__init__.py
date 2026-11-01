"""QRClipper 测试包。

加了这个 __init__.py 之后，既可以直接运行单个文件：

    python tests/test_smoke.py

也可以用 unittest 的模块名 / 自动发现方式在仓库根目录运行：

    python -m unittest tests.test_smoke tests.test_hotkey_entry tests.test_decoder_tiles tests.test_notify
    python -m unittest discover -s tests

各测试文件自己会把仓库根目录插入 sys.path，因此两种用法都能正常 import
qrclip.py 同级的源码模块。

注意：test_gui.py、test_hotkey_e2e.py、test_settings_reload.py 并非
unittest.TestCase 子类，而是以 `if __name__ == "__main__"` 收尾的独立脚本
（会真的弹出窗口、注册全局热键）。它们对 unittest 贡献 0 个用例，既不会被
自动发现也不会被执行——不是"被跳过"，需要手动运行，例如：

    python tests/test_gui.py

所以 `discover` 只跑出 78 个用例（test_smoke 26 + test_hotkey_entry 17 +
test_decoder_tiles 10 + test_notify 25）是正常现象，不要误以为 GUI 用例被
静默跳过了。日常回归显式列出这四个非交互模块即可。

test_decoder_tiles.py 是 4K 大图分块扫描的回归：合成 3840x2160 大图并贴入
小二维码，验证「整体识别失败 -> 分块滑窗兜底」这条链路。

test_notify.py 是通知降级链路的回归：pystray -> Shell_NotifyIcon 气泡 -> Tk
浮窗。核心断言是「托盘完全没有时通知仍然要弹出来」，以及「未识别到二维码 /
识别失败必须无视 notify=false」（用户关掉通知只是想少看成功提示，不是想在
出问题时两眼一抹黑）。

另有两个需要真实桌面的手动脚本（不参与 unittest）：

    python tests/diag_notify.py        # 逐个通道弹一次并抓右下角截图，人眼确认
    python tests/verify_real_env.py    # 向热键窗口投递 WM_HOTKEY，抓通知并对比日志

它们的存在原因是：本项目的两次"假通过"都源于只读日志不看屏幕——Shell 返回
成功不代表用户看得见。
"""
