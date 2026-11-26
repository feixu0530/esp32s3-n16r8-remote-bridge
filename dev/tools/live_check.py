"""
真机联调验证。

两件事：
    1. 连上开发板的 USB CDC，走一遍真实协议往返
       （INFO / BONDED / REMOTES / SCAN），打印设备实际回什么
    2. 用**真实窗口**（不是 offscreen）启动 QML 界面，截图存盘
       —— 验证中文能正常显示、布局在真实 DPI 下不出错

用法：
    python dev/tools/live_check.py --port COM12
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client" / "src"))

from platforms.base import SerialPort  # noqa: E402
from core.protocol import ProtocolClient  # noqa: E402


def check_protocol(port_name: str, baud: int = 115200) -> bool:
    from platforms.windows import SerialPortWin

    print("=" * 64)
    print(f"[1/2] 协议联调 —— {port_name}")
    print("=" * 64)

    port = SerialPortWin()
    try:
        port.open(port_name, baud)
    except Exception as exc:
        print(f"  打开串口失败：{exc}")
        return False

    cli = ProtocolClient(port)
    lines: list[str] = []
    cli.on_raw(lambda t: lines.append(t))
    cli.start()
    time.sleep(0.3)

    ok = True
    for verb, args in (("INFO", ()), ("BONDED", ()), ("REMOTES", ()),
                       ("STATE", ())):
        before = len(lines)
        cli.send(verb, *args)
        time.sleep(0.6)
        got = lines[before:]
        print(f"\n  > {verb}")
        for g in got[:12]:
            print(f"    {g}")
        if not got:
            print("    （无应答）")
            ok = False

    # 扫一轮，看有没有设备
    print("\n  > SCAN 2（扫一轮）")
    before = len(lines)
    cli.send("SCAN", 2)
    time.sleep(2.5)
    got = lines[before:]
    scans = [g for g in got if g.startswith("EV SCAN ")]
    print(f"    扫到 {len(scans)} 个设备")
    for s in scans[:8]:
        print(f"      {s}")

    cli.stop()
    port.close()
    return ok


def check_ui() -> bool:
    print()
    print("=" * 64)
    print("[2/2] 真实窗口渲染")
    print("=" * 64)

    from PySide6.QtCore import QUrl, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtQuickControls2 import QQuickStyle

    from core.controller import (BridgeController, MappingModel, MarkerModel,
                                 RemoteListModel, ScanModel)
    from core.resources import qml_dir

    app = QGuiApplication(sys.argv)
    QQuickStyle.setStyle("Fusion")

    controller = BridgeController()
    engine = QQmlApplicationEngine()
    warnings: list[str] = []
    engine.warnings.connect(lambda ws: warnings.extend(w.toString() for w in ws))

    engine.rootContext().setContextProperty("Bridge", controller)
    engine.rootContext().setContextProperty("RemoteListModel", controller.remoteModel)
    engine.rootContext().setContextProperty("ScanModel", controller.scanModel)
    engine.rootContext().setContextProperty("MappingModel", controller.mappingModel)
    engine.rootContext().setContextProperty("MarkerModel", controller.markerModel)

    engine.load(QUrl.fromLocalFile(str(qml_dir() / "main.qml")))
    roots = engine.rootObjects()
    if not roots:
        print("  QML 加载失败")
        for w in warnings[:10]:
            print("   ", w)
        return False

    win = roots[0]
    if isinstance(win, QQuickWindow):
        win.resize(1180, 760)
        win.show()
        win.raise_()

    out_dir = Path(__file__).resolve().parents[1] / "client" / "temp"
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = ["蓝牙配对", "遥控器设置", "语音", "固件", "设置"]
    for i, label in enumerate(pages):
        win.setProperty("currentPage", i)
        for _ in range(20):
            app.processEvents()
            time.sleep(0.01)
        shot = out_dir / f"live_page{i}.png"
        win.grabWindow().save(str(shot))
        size = shot.stat().st_size if shot.is_file() else 0
        print(f"  第 {i + 1} 页「{label}」截图 {shot.name}  {size} 字节")

    print(f"  QML 警告 {len(warnings)} 条")
    for w in warnings[:5]:
        print("   ", w)

    print("\n  窗口保持打开 8 秒（可以自己看一眼），然后自动关闭")
    t0 = time.time()
    while time.time() - t0 < 8:
        app.processEvents()
        time.sleep(0.02)

    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM12")
    ap.add_argument("--skip-protocol", action="store_true")
    args = ap.parse_args()

    ok = True
    if not args.skip_protocol:
        ok = check_protocol(args.port)
    check_ui()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
