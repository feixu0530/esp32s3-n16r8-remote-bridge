"""
系统托盘 —— 关闭窗口时隐藏到任务栏。

参考项目 windows-remote-mic-app 的 bridge_tray_windows.py，
但我们用的是 Qt 自带的 QSystemTrayIcon（PySide6 已经在包里）。

行为：
    - 关闭窗口 → 隐藏到托盘（closeBehavior = "hide"）
    - 托盘图标双击 → 弹出主界面
    - 托盘菜单：显示主界面 / 退出
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QMenu, QSystemTrayIcon)


def _make_icon() -> QIcon:
    """托盘图标 —— 跟窗口用同一张，文件缺失才回退到手绘的。"""
    from core import resources
    path = resources.assets_dir() / "icons" / "app.ico"
    if path.is_file():
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon

    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#087cf0"), 3))
    painter.drawRoundedRect(4, 12, 24, 12, 3, 3)
    painter.setBrush(QColor("#087cf0"))
    painter.drawEllipse(6, 4, 8, 8)
    painter.drawEllipse(18, 4, 8, 8)
    painter.end()
    return QIcon(pixmap)


class TrayIcon:
    """
    托盘图标。

    show_window / hide_window / quit_app 由 app.py 注入。
    """

    def __init__(self, on_show, on_quit,
                 parent=None,
                 title: str = "",
                 tooltip: str = "") -> None:
        from core.version import APP_TITLE
        title = title or APP_TITLE
        tooltip = tooltip or APP_TITLE
        self._on_show: Callable[[], None] = on_show
        self._on_quit: Callable[[], None] = on_quit
        self._tray: Optional[QSystemTrayIcon] = None

        if not QSystemTrayIcon.isSystemTrayAvailable():
            # 没有托盘（比如某些精简系统）就退回「关窗即退出」
            return

        self._tray = QSystemTrayIcon(_make_icon(), parent)
        self._tray.setToolTip(tooltip)

        menu = QMenu()
        action_show = QAction("显示主界面", menu)
        action_show.triggered.connect(self._on_show)
        action_quit = QAction("退出", menu)
        action_quit.triggered.connect(self._on_quit)
        menu.addAction(action_show)
        menu.addSeparator()
        menu.addAction(action_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    def _on_activated(self, reason) -> None:
        # 双击或单击都把主界面拉回来
        try:
            from PySide6.QtWidgets import QSystemTrayIcon as T
        except Exception:
            return
        if reason in (T.ActivationReason.Trigger,
                      T.ActivationReason.DoubleClick):
            self._on_show()

    def notify(self, title: str, message: str) -> None:
        if self._tray is not None:
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4000)

    def hide(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    @property
    def available(self) -> bool:
        return self._tray is not None


class AppShell(QObject):
    """
    应用外壳 —— QML 通过 `App` 这个上下文属性跟它打交道。

    负责托盘的创建、窗口的显示/隐藏、以及「彻底退出」。
    """

    trayAvailableChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._window = None
        self._tray: Optional[TrayIcon] = None

    @Property(bool, notify=trayAvailableChanged)
    def trayAvailable(self) -> bool:
        """系统托盘可用吗？不可用时关闭窗口就必须是直接退出。"""
        return self._tray is not None and self._tray.available

    def attach(self, window, tray: Optional[TrayIcon]) -> None:
        self._window = window
        self._tray = tray
        self.trayAvailableChanged.emit()

    @Slot()
    def showWindow(self) -> None:
        if self._window is None:
            return
        self._window.show()
        self._window.showNormal()
        self._window.raise_()
        self._window.requestActivate()

    @Slot()
    def quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()
