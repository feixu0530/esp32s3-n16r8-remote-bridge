"""
把项目的应用图标 SVG 渲染成 Android 各密度的 PNG。

给 APK 重打包用（阶段 6）：movo 的图标换成我们自己的。

用法：
    python dev/client/build/make_android_icons.py <输出目录>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent
SVG = CLIENT / "src" / "assets" / "icons" / "app.svg"

# Android 各密度对应的启动图标边长（像素）
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}


def render_png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


def main() -> int:
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "android_icons")
    outdir.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        print(f"SVG 读不出来：{SVG}")
        return 1
    for density, size in DENSITIES.items():
        data = render_png(renderer, size)
        path = outdir / f"ic_launcher_{density}.png"
        path.write_bytes(data)
        print(f"  {path.name}  {size}x{size}  {len(data)} 字节")
    print(f"图标已生成 → {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
