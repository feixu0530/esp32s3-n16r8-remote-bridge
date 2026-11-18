"""
把仓库里的 *.b64 资源还原成二进制文件（图标、遥控器照片）。

为什么有这一步：
    图标 .ico 和遥控器照片 .png 是二进制，Git 里存成 base64 文本更方便审阅与克隆；
    还原后程序才能找到 assets/ 下的真实文件。

用法（仓库根目录）：
    python tools/restore_assets.py
"""
from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def restore() -> list[Path]:
    written: list[Path] = []
    for b64 in ROOT.rglob("*.b64"):
        target = b64.with_suffix("")          # 去掉 .b64
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(b64.read_text(encoding="ascii")))
        written.append(target)
        print(f"restored {target.relative_to(ROOT)}")
    if not written:
        print("everything already restored")
    return written


if __name__ == "__main__":
    restore()
