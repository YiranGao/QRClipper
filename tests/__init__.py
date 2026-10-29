"""QRClipper 测试包。

加了这个 __init__.py 之后，既可以直接运行单个文件：

    python tests/test_smoke.py

也可以用 unittest 的模块名 / 自动发现方式在仓库根目录运行：

    python -m unittest tests.test_smoke tests.test_hotkey_entry
    python -m unittest discover -s tests

各测试文件自己会把仓库根目录插入 sys.path，因此两种用法都能正常 import
qrclip.py 同级的源码模块。

注意：test_gui.py、test_hotkey_e2e.py、test_settings_reload.py 并非
unittest.TestCase 子类，而是以 `if __name__ == "__main__"` 收尾的独立脚本
（会真的弹出窗口、注册全局热键）。它们对 unittest 贡献 0 个用例，既不会被
自动发现也不会被执行——不是"被跳过"，需要手动运行，例如：

    python tests/test_gui.py

所以 `discover` 只跑出 32 个用例（15 + 17）是正常现象，不要误以为 GUI 用例
被静默跳过了。日常回归显式列出两个非交互模块即可。
"""
