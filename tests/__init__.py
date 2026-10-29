"""QRClipper 测试包。

加了这个 __init__.py 之后，既可以直接运行单个文件：

    python tests/test_smoke.py

也可以用 unittest 的模块名 / 自动发现方式在仓库根目录运行：

    python -m unittest tests.test_smoke tests.test_hotkey_entry
    python -m unittest discover -s tests

各测试文件自己会把仓库根目录插入 sys.path，因此两种用法都能正常 import
qrclip.py 同级的源码模块。

注意：tests 目录下包含 GUI 与端到端用例（test_gui.py、test_hotkey_e2e.py），
会真的弹出窗口、注册全局热键，无人值守环境下会自动跳过或需要人工配合，
因此日常回归建议只显式列出非交互的两个模块。
"""
