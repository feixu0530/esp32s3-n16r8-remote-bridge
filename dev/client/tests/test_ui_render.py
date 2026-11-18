"""
QML 离屏渲染验证。

★ 这道测试是 v0.7.1 的直接产物 ★
   教训：v0.7.0 打包后「按键映射的部分」整个消失 ——
   原因是改 QML 结构后**没实际看界面**，只做了语法检查。

所以这里做三件事：
    1. 离屏加载 main.qml，**零警告**（抓「属性缺失」「信号接不上」）
    2. 逐页切换，检查每页真的有内容（不只是加载成功）
    3. 存截图，出问题时能看

运行：
    python dev/client/tests/test_ui_render.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ★ 必须在 import core.* 之前 ★
#   BridgeController 一初始化就 load_config()，改名/删除都会 save_config()。
#   不重定向数据根，跑一次界面测试就动了用户真机配好的遥控器表。
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-uirender-")

os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import (QCoreApplication, QMetaObject, QPoint,  # noqa: E402
                            QPointF, QRect, QObject, Qt, QTimer, QUrl)
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from core.controller import (BridgeController, MappingModel,  # noqa: E402
                             RemoteListModel, ScanModel)
from core.key_mapping import ButtonTrigger, firmware_bindings  # noqa: E402
from core.resources import qml_dir  # noqa: E402

FAILURES: list[str] = []
WARNINGS: list[str] = []
WIN = None            # main() 里加载完的 ApplicationWindow，helper 要用


class _Result:
    """MAPGET 应答的替身。"""

    def __init__(self, count: int) -> None:
        self._count = count

    def get_int(self, _key: str, _default: int = 0) -> int:
        return self._count


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def items_named(root_item, object_name: str) -> list:
    """
    按 objectName 找出所有子孙视觉项。

    ★ 为什么不用 findChildren ★
        Repeater 造出来的 delegate 只有**视觉父项**，没有 QObject 父项
        （item.parent() 是 None），findChildren 顺着 QObject 树走，
        永远找不到列表里的按钮。这里递归 childItems()。
        模型一 reset（比如改完名重建列表），delegate 就被销毁，
        所以每次用之前都得重新找一遍。
    """
    found: list = []

    def walk(item) -> None:
        for child in item.childItems():
            if child.objectName() == object_name:
                found.append(child)
            walk(child)

    walk(root_item)
    return found


def qml_value(obj, name: str):
    """读 QML 属性，兼容 PySide6 有时返回 QVariant、有时返回原生值。"""
    v = obj.property(name)
    to_variant = getattr(v, "toVariant", None)
    return to_variant() if callable(to_variant) else v


def to_window(item, local_x: float, local_y: float) -> QPoint:
    """
    item 内部坐标 → 窗口坐标。

    QTest.mouseClick 只认 QWidget 或 QWindow，QML 项不是；
    所以得把点换算成窗口坐标，再对窗口发消息（和 test_marker_geometry 一样）。
    """
    p: QPointF = item.mapToItem(None, QPointF(local_x, local_y))
    return QPoint(int(round(p.x())), int(round(p.y())))


def wait_for(desc: str, cond, timeout_ms: int = 3000) -> bool:
    """
    等到条件成立，别靠「多打几帧」猜时间。

    ★ 为什么需要它 ★
        切回蓝牙页时 StackLayout 重新显示这一页，Flickable 里 delegate 的
        几何要等下一次布局才算得出来。以前这里只 processEvents 若干次，
        同一份代码有时点得中「重命名」、有时点空（真机复现：6 次里 1 次失败）。
        界面测试最怕的就是这种「看运气的绿」，所以一律改成等条件。
        （QTest.qWait 会转事件循环，比空转 processEvents 真跑得动布局。）
    """
    waited = 0
    while True:
        try:
            if cond():
                return True
        except RuntimeError:
            pass                      # delegate 被模型 reset 销毁了，下一轮重新找
        if waited >= timeout_ms:
            print(f"  FAIL  {desc}（等 {timeout_ms}ms 没等到）")
            FAILURES.append(desc)
            return False
        QTest.qWait(20)
        waited += 20


def settled_button(object_name: str, which: int):
    """
    找第 which 个 objectName 项，等它的窗口坐标**连续两次一致且落在窗口内**。

    返回 (item, QPoint)；等不到返回 (None, None)。
    每一轮都重新找：delegate 会随模型 reset 销毁，攥着旧引用必炸。
    """
    prev = None
    waited = 0
    while waited < 3000:
        QTest.qWait(20)
        waited += 20
        found = items_named(WIN.contentItem(), object_name)
        if len(found) <= which:
            prev = None
            continue
        item = found[which]
        cur = to_window(item, item.width() / 2.0, item.height() / 2.0)
        inside = QRect(0, 0, WIN.width(), WIN.height()).contains(cur)
        if prev is not None and cur == prev and inside:
            return item, cur
        prev = cur
    return None, None


def main() -> int:
    print("=" * 60)
    print("QML 渲染验证")
    print("=" * 60)

    app = QGuiApplication(sys.argv)
    # ★ 必须设样式，否则 Fusion 之外的原生样式不允许自定义控件外观 ★
    QQuickStyle.setStyle("Fusion")

    controller = BridgeController()

    engine = QQmlApplicationEngine()
    engine.warnings.connect(
        lambda ws: WARNINGS.extend(w.toString() for w in ws))

    engine.rootContext().setContextProperty("Bridge", controller)
    engine.rootContext().setContextProperty("RemoteListModel", controller.remoteModel)
    engine.rootContext().setContextProperty("ScanModel", controller.scanModel)
    engine.rootContext().setContextProperty("MappingModel", controller.mappingModel)

    qml = qml_dir() / "main.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))
    roots = engine.rootObjects()
    check("main.qml 加载成功", bool(roots), str(qml))
    if not roots:
        for w in WARNINGS:
            print("    警告:", w)
        return 1

    win = roots[0]
    global WIN
    WIN = win
    if isinstance(win, QQuickWindow):
        win.resize(1080, 720)
        win.show()

    # 让事件循环跑几帧，等布局稳定
    app.processEvents()

    def pump(frames: int = 8) -> None:
        for _ in range(frames):
            app.processEvents()

    pump(20)

    # ---- 逐页检查 ----
    pages = ["设备", "按键", "语音"]
    for i, label in enumerate(pages):
        win.setProperty("currentPage", i)
        pump(12)
        # 以前这里写了个「永远为真」的检查（check(..., True)），
        # 那是假绿 —— 改成真的读回 currentPage。
        check(f"第 {i + 1} 页「{label}」可切换",
              qml_value(win, "currentPage") == i,
              str(qml_value(win, "currentPage")))

    # ---- 导航项齐全（直接读 main.qml 暴露的 pages 属性）----
    page_prop = win.property("pages")
    try:
        page_list = page_prop.toVariant()
    except AttributeError:
        page_list = page_prop
    check("导航项齐全",
          isinstance(page_list, list) and len(page_list) == len(pages),
          str(page_list))

    # ---- 截图 ----
    shots: list[int] = []
    out_dir = Path(__file__).resolve().parents[1] / "temp"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, label in enumerate(pages):
        win.setProperty("currentPage", i)
        pump(12)
        shot = out_dir / f"ui_page{i}.png"
        win.grabWindow().save(str(shot))
        check(f"第 {i + 1} 页截图", shot.is_file() and shot.stat().st_size > 0,
              str(shot))
        shots.append(shot.stat().st_size)

    # 每页内容应当不同（否则说明切页没生效）
    check("各页内容互不相同", len(set(shots)) == len(shots), str(shots))

    # ---------------------------------------------------------- 配对页重命名
    #
    # ★ 为什么要这一大段 ★
    #   「重命名」这个功能逻辑部分在 test_scan_list.py 里已经测过了，
    #   但真机崩的都是 **QML 那一头**：按钮找不找得到、弹窗知不知道自己
    #   改的是哪一台、字段文本能不能传进 setRemoteName。
    #   这里全程走界面：鼠标点按钮 → 弹窗 → 填名字 → accept。
    print("\n[配对页 · 重命名交互]")
    win.setProperty("currentPage", 0)
    pump(12)

    # 模拟固件上报两台同名的 Chromecast（广播名全是 HID_Device）
    for i, addr in enumerate(("AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02")):
        controller._on_remote_row({"idx": str(i), "addr": addr,
                                   "name": "HID_Device", "type": "0",
                                   "conn": "1"})
    pump(16)

    def row_name(r: int) -> str:
        return controller.remoteModel.data(controller.remoteModel.index(r, 0), 257)

    check("两台同名遥控器都进了列表",
          controller.remoteModel.rowCount() == 2
          and row_name(0) == "HID_Device" and row_name(1) == "HID_Device",
          f"{row_name(0)} / {row_name(1)}")

    # 等按钮出现 **并且位置算稳了**（切页回来的那一帧几何还没算完，
    # 拿旧坐标点上去就是点空，这就是以前 6 次里错 1 次的原因）
    btn, spot = settled_button("renameRemote", 1)
    check("列表每行都有「重命名」按钮，且坐标稳定可点",
          btn is not None and spot is not None, str(spot))

    if btn is not None:
        # 点**第二台**的按钮：弹窗得记住自己改的是哪一行
        QTest.mouseClick(win, Qt.LeftButton, Qt.NoModifier, spot)

        dialogs = win.findChildren(QObject, "remoteNameDialog")
        check("弹出重命名对话框", len(dialogs) == 1, str(len(dialogs)))
        if dialogs:
            dlg = dialogs[0]
            check("弹窗打开",
                  wait_for("弹窗打开",
                           lambda: qml_value(dlg, "visible") is True),
                  str(qml_value(dlg, "visible")))
            check("弹窗知道改的是第 1 台",
                  qml_value(dlg, "remoteIndex") == 1,
                  str(qml_value(dlg, "remoteIndex")))
            check("弹窗回填当前名字",
                  qml_value(dlg, "currentName") == "HID_Device",
                  str(qml_value(dlg, "currentName")))

            fields = dlg.findChildren(QObject, "remoteNameField")
            check("输入框在弹窗里", len(fields) == 1, str(len(fields)))
            if fields:
                fields[0].setProperty("text", "卧室的")
                # accept() 是 Dialog 的方法，会走 onAccepted → setRemoteName
                QMetaObject.invokeMethod(dlg, "accept", Qt.DirectConnection)
                check("改名生效（第 1 台）",
                      wait_for("改名生效（第 1 台）",
                               lambda: row_name(1) == "卧室的"),
                      row_name(1))
                check("另一台没被误改", row_name(0) == "HID_Device",
                      row_name(0))
                check("弹窗已关闭", qml_value(dlg, "visible") is False,
                      str(qml_value(dlg, "visible")))
                remotes = controller._cfg.get("remotes", [])
                check("custom_name 落盘",
                      len(remotes) == 2
                      and remotes[1].get("custom_name") == "卧室的",
                      str(remotes))
                # 改名会 reset 模型 → 旧 delegate 已销毁，必须重新找
                check("改名后列表按钮重建",
                      wait_for("改名后列表按钮重建",
                               lambda: len(items_named(
                                   win.contentItem(), "renameRemote")) == 2))

    # ---- 警告 ----
    print()
    if WARNINGS:
        print(f"发现 {len(WARNINGS)} 条 QML 警告：")
        for w in WARNINGS[:40]:
            print("   ", w)
        FAILURES.append(f"{len(WARNINGS)} 条 QML 警告")
    else:
        print("  PASS  零 QML 警告")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
