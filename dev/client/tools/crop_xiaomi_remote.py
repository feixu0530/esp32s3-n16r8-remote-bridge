#!/usr/bin/env python3
"""
从 reference/小米语音遥控器.jpg 裁出遥控器本体，生成按键页用的实物图。

产出：dev/client/src/assets/remotes/Xiaomi-remote.png
     （白边 → 透明，遥控器本体放大到至少 600px 高，热点坐标按这张图标定）

图像处理用 PySide6 的 QImage —— 客户端本来就依赖它，不用额外装 Pillow。

用法：
    python tools/crop_xiaomi_remote.py [--src <jpg>] [--dst <png>]
"""
from __future__ import annotations

import argparse
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.dirname(HERE)          # dev/client
DEV = os.path.dirname(CLIENT)           # dev
ROOT = os.path.dirname(DEV)             # 仓库根（reference/ 在这里）

DEFAULT_SRC = os.path.join(ROOT, "reference", "小米语音遥控器.jpg")
DEFAULT_DST = os.path.join(CLIENT, "src", "assets", "remotes", "Xiaomi-remote.png")

# 目标高度：按键页里遥控器显示高度约 250px，2 倍余量
TARGET_H = 640


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--dst", default=DEFAULT_DST)
    ap.add_argument("--threshold", type=int, default=245,
                    help="比这个亮度更暗的像素算「实物」")
    args = ap.parse_args()

    im = QImage(args.src)
    if im.isNull():
        raise SystemExit("打不开源图: %s" % args.src)
    im = im.convertToFormat(QImage.Format_RGB32)
    w, h = im.width(), im.height()

    # 找非白区域的外接矩形（遥控器是深色的，背景是纯白）
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            c = im.pixelColor(x, y)
            if min(c.red(), c.green(), c.blue()) < args.threshold:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        raise SystemExit("没找到非白区域，阈值可能不对")

    pad = 2
    x0 = max(0, minx - pad)
    y0 = max(0, miny - pad)
    x1 = min(w, maxx + 1 + pad)
    y1 = min(h, maxy + 1 + pad)
    box = (x0, y0, x1, y1)
    crop = im.copy(x0, y0, x1 - x0, y1 - y0)

    # 白底转透明（按键页背景是深色，白块会很扎眼）
    cw, ch = crop.width(), crop.height()
    out = QImage(cw, ch, QImage.Format_ARGB32)
    out.fill(Qt.transparent)
    for y in range(ch):
        for x in range(cw):
            c = crop.pixelColor(x, y)
            if min(c.red(), c.green(), c.blue()) >= args.threshold:
                continue
            out.setPixelColor(x, y, c)

    scale = max(1, TARGET_H // ch)
    out = out.scaled(cw * scale, ch * scale,
                     Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    os.makedirs(os.path.dirname(args.dst), exist_ok=True)
    if not out.save(args.dst):
        raise SystemExit("保存失败: %s" % args.dst)

    print("源图 %dx%d  裁剪框 %s  →  %dx%d  %s"
          % (w, h, box, out.width(), out.height(), args.dst))


if __name__ == "__main__":
    main()
