"""修复 controller.py 里被误插入的两行。"""
import pathlib

p = pathlib.Path("src/core/controller.py")
s = p.read_text(encoding="utf-8")

bad = """                self._learning_marker = ""
        self._show_all = False
        self._scan_items: list[dict] = []
                self._save_markers()"""
good = """                self._learning_marker = ""
                self._save_markers()"""
assert bad in s, "误插入的块没找到"
s = s.replace(bad, good)

# 正确地把字段加到 __init__
anchor = """        self._learning_marker = ""
        self._hotkey_capturing = False"""
if anchor not in s:
    # 找 __init__ 里的那个
    import re
    m = re.search(r"self\._learning_marker = \"\"\n", s)
    assert m, "找不到 __init__ 里的 _learning_marker"
    s = s[:m.end()] + "        self._show_all = False\n        self._scan_items: list[dict] = []\n" + s[m.end():]
else:
    s = s.replace(anchor, """        self._learning_marker = ""
        self._show_all = False
        self._scan_items: list[dict] = []
        self._hotkey_capturing = False""")

p.write_text(s, encoding="utf-8")
print("controller.py 修复完成")
