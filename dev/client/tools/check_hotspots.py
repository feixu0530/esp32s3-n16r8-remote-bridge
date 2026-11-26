"""把按键热点画到遥控器实物图上，用来核对坐标（离线核对用）。

用法：
    python tools/check_hotspots.py
产出：
    dev/client/temp/xiaomi-hotspots.png
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent
sys.path.insert(0, str(CLIENT / "src"))

from PySide6.QtCore import QRect, Qt          # noqa: E402
from PySide6.QtGui import (QColor, QFont, QImage, QPainter,  # noqa: E402
                           QPen)

from core import remote_layout as rl           # noqa: E402


def draw(kind: str, src: Path, dst: Path) -> None:
    im = QImage(str(src)).convertToFormat(QImage.Format_ARGB32)
    p = QPainter(im)
    pen = QPen(QColor(255, 0, 0))
    pen.setWidth(1)
    p.setPen(pen)
    f = QFont()
    f.setPixelSize(9)
    p.setFont(f)
    for h in rl.hotspots_for(kind):
        x = int(h.x * im.width())
        y = int(h.y * im.height())
        w = max(2, int(h.width * im.width()))
        hh = max(2, int(h.height * im.height()))
        p.drawRect(QRect(x, y, w, hh))
        p.drawText(x + 1, y + 9, h.button_id)
    p.end()
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(str(dst))
    print("%s -> %s (%dx%d)" % (kind, dst, im.width(), im.height()))


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # 画文字要用字体数据库 → 必须有 QGuiApplication
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])
    out = CLIENT / "temp"
    draw(rl.LAYOUT_XIAOMI,
         CLIENT / "src" / "assets" / "remotes" / "Xiaomi-remote.png",
         out / "xiaomi-hotspots.png")
    draw(rl.LAYOUT_CHROMECAST,
         CLIENT / "src" / "assets" / "remotes" / "Chromecast-remote-photo.png",
         out / "chromecast-hotspots.png")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.exit(main())
