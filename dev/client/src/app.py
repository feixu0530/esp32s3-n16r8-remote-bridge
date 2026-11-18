"""
客户端入口。

    python dev/client/src/app.py

绿色版由 PyInstaller 打包成 onedir，双击 EXE 启动。
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

# 允许从源码直接运行
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from PySide6.QtCore import QUrl, Slot
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMessageBox

from core import logging_setup, resources
from core.controller import (BridgeController, MappingModel,
                             RemoteListModel, ScanModel)
from core.resources import qml_dir
from core.tray import AppShell, TrayIcon



def selftest() -> int:
    """
    无界面自检：连上开发板，跑一遍协议，把结果写进 device.log。

    用途：
      · 验证**打包版**跟真机的通信链路（不用点 GUI）
      · 现场排障时一条命令就能拿到完整协议记录

    用法：
        ESP32S3RemoteBridge.exe --selftest [--port COM12]
    """
    import argparse

    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--port", default="")
    ap.add_argument("--scan-seconds", type=float, default=15.0)
    args, _unknown = ap.parse_known_args(sys.argv[1:])

    logging_setup.setup("DEBUG")
    log = logging_setup.get_logger("app")
    device_log = logging_setup.get_logger("device")

    from core.controller import BridgeController

    controller = BridgeController()

    ports = controller.availablePorts()
    if not ports:
        print("找不到串口 —— 开发板插上了吗？")
        return 2

    target = args.port
    if not target:
        # 优先 ESP32-S3 原生 USB（VID 0x303A）
        for p in ports:
            if "303A" in p.get("label", "").upper():
                target = p.get("device", "")
                break
    if not target:
        target = ports[0].get("device", "")

    print(f"自检：连接 {target}")
    # ★ 控制器一初始化就可能已经自动连上了 ★
    #   （_poll_ports 里发现唯一的协议通道就自动连）
    #   这时候再 connectDevice 同一个口会「端口被占用」，
    #   打包版退出时还可能撞 Qt 析构竞态。
    #   所以先看连上没有，没连才连。
    if not controller.connected:
        if not controller.connectDevice(target):
            print("串口打开失败")
            controller.disconnectDevice()
            return 3
    else:
        print(f"已经自动连上 {target}")

    print("已连接，发 INFO / BONDED / REMOTES …")
    time.sleep(1.0)

    # 扫一轮，把候选全记下来
    print(f"扫描 {args.scan_seconds} 秒 …")
    controller.startScan()
    time.sleep(args.scan_seconds)
    controller.stopScan()
    controller.refreshRemotes()
    time.sleep(1.0)

    device_log.info("=== selftest 结束 ===")
    controller.disconnectDevice()
    # ★ 打包版退出时必须让 Qt 对象安静地死 ★
    #   控制器的 QTimer 是它的子对象，解释器退出时析构顺序不保证，
    #   偶尔会撞出 access violation。这里显式释放。
    del controller
    import gc
    gc.collect()

    # 把结果打印出来
    log_dir = resources.logs_dir()
    print()
    print("=" * 62)
    print(f"日志：{log_dir}")
    print("=" * 62)
    try:
        for line in (log_dir / "device.log").read_text(encoding="utf-8",
                                                       errors="replace").splitlines():
            print(line)
    except OSError as exc:
        print(f"读日志失败：{exc}")
        return 4
    return 0



def install_qml_error_trap() -> None:
    """
    把 QML 的报错和警告抓进日志。

    ★ 为什么必须做这个 ★
    打包成 --windowed 之后**没有控制台**，QML 里抛的异常
    只会打印到一个不存在的 stderr —— 现场完全看不到。
    之前就因为 QML 调用槽函数参数不匹配（TypeError）导致
    「点了配对没反应」，而日志里一条线索都没有。
    """
    from PySide6.QtCore import qInstallMessageHandler

    def handler(mode, context, message):
        level = {0: "debug", 1: "info", 2: "warning", 3: "critical",
                 4: "critical"}.get(mode, "info")
        log = logging_setup.get_logger("app")
        try:
            log.log(logging.ERROR if level == "critical" else logging.INFO,
                    "QML[%s] %s", level, message)
        except Exception:
            pass

    qInstallMessageHandler(handler)


def _focus_existing_window() -> None:
    """
    已经有实例在跑：把它的窗口从托盘 / 后台叫到前台。

    ★ 不叫到前台，用户只会看到「已在运行」的弹窗，却找不到那个窗口 ★
      窗口缩进托盘后依然存在，FindWindowW 找得到，
      ShowWindow(SW_RESTORE) 能把它拉回来。
    """
    import ctypes
    from core.version import APP_TITLE
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, APP_TITLE)
    if not hwnd:
        return
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.SetForegroundWindow(hwnd)


def main() -> int:
    # 打包版：快捷键录制的子进程用同一个 exe 换个参数再跑一遍
    if "--capture-hotkey-worker" in sys.argv:
        from platforms.hotkey_capture_process import child_main
        return child_main()

    if "--selftest" in sys.argv:
        return selftest()

    logging_setup.setup("INFO")
    log = logging_setup.get_logger("app")
    install_qml_error_trap()

    # 开机自启的启动项如果指向旧 exe，自动重绑
    try:
        from core import startup
        state = startup.rebind_if_needed()
        log.info("开机自启状态：%s (%s)", state.enabled, state.detail)
    except Exception as exc:
        log.warning("开机自启检查失败：%s", exc)

    QGuiApplication.setAttribute(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.AA_ShareOpenGLContexts)
    # 用 QApplication 而不是 QGuiApplication —— 托盘（QSystemTrayIcon）在 QtWidgets 里
    app = QApplication(sys.argv)
    QQuickStyle.setStyle("Fusion")

    # ★ 应用图标（任务栏 / Alt-Tab / 窗口左上角）★
    #   以前是 Qt 默认图标，绿色版 EXE 也是 Python 默认图标。
    icon = QIcon(str(resources.assets_dir() / "icons" / "app.ico"))
    if icon.isNull():
        icon = QIcon(str(resources.assets_dir() / "icons" / "app.svg"))
    if not icon.isNull():
        app.setWindowIcon(icon)

    # ★ 单实例 ★
    #   两个客户端会抢同一个串口 → 后启动的那个「点连接没反应」
    #   （用户实测：串口打开失败 PermissionError(13) 拒绝访问 = 端口被占）。
    #
    #   ★ 用 Win32 命名互斥量，不要用 QSharedMemory ★
    #     QSharedMemory.tryLock() 在「共享段刚被创建」时返回 false ——
    #     第一个启动的实例会把自己判成「已有实例」而弹窗退出（踩过）。
    import ctypes
    _mutex = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\ESP32S3RemoteBridge-single")
    _already = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    if _already:
        _focus_existing_window()
        QMessageBox.information(
            None, "已在运行",
            "已经有一个客户端在运行了。\n"
            "把它从任务栏右下角的托盘里退出（或在任务管理器里结束），再打开这个。")
        return 0

    controller = BridgeController()
    shell = AppShell()

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("Bridge", controller)
    engine.rootContext().setContextProperty("App", shell)
    engine.rootContext().setContextProperty("RemoteListModel", controller.remoteModel)
    engine.rootContext().setContextProperty("ScanModel", controller.scanModel)
    engine.rootContext().setContextProperty("MappingModel", controller.mappingModel)

    qml = qml_dir() / "main.qml"
    if not qml.is_file():
        log.error("找不到 main.qml：%s", qml)
        print(f"找不到 main.qml: {qml}", file=sys.stderr)
        return 2

    engine.load(QUrl.fromLocalFile(str(qml)))
    if not engine.rootObjects():
        log.error("QML 加载失败")
        return 3

    # 托盘：关窗口时缩到右下角，托盘菜单可以再拉回来或彻底退出
    window = engine.rootObjects()[0]
    # ★ 窗口图标也要单独设 ★
    #   只设 app.setWindowIcon 的话，QQuickWindow 还是 Qt 默认图标，
    #   任务栏那一格不会变。
    if not icon.isNull():
        window.setIcon(icon)
    tray = TrayIcon(on_show=shell.showWindow, on_quit=shell.quit)
    shell.attach(window, tray)

    # ★ 退出前必须收尾 ★
    #   解释器退出时 QApplication 先死、控制器（含它的 QTimer）后死 ——
    #   QTimer 析构时去访问已经销毁的事件分发器 → access violation。
    app.aboutToQuit.connect(controller.shutdown)

    log.info("客户端启动，版本 %s", controller.appVersion)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
