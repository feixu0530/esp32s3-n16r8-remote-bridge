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
    # ★ 顺序要和 main.qml 的 pages 一致 ★
    #   设备 / 按键 / 语音 —— 「手机」独立页在 v1.2.9 取消：
    #   手机已经是蓝牙设备，真实机型名/地址显示在设备页的「手机（…）」行。
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

    # ---------------------------------------------------------- 小米遥控器布局
    print("")
    print("[小米遥控器布局]")
    controller._cfg["remotes"] = [{
        "addr": "44:28:AA:BB:CC:01", "identity": "44:28:AA:BB:CC:01",
        "type": 0, "name": "MI RC", "image": "", "markers": [],
        "kind": "xiaomi",
        "bindings": {"bindings": {}, "secondary_bindings": {}},
    }]
    controller.selectRemote(0)
    win.setProperty("currentPage", 1)
    pump(16)

    check("小米遥控器用的是小米那张实物图",
          controller.remoteImageSource(0).endswith("Xiaomi-remote.png"),
          controller.remoteImageSource(0))
    xiaomi_spots = controller.hotspots
    check("小米热点 12 个（含语音键）",
          len(xiaomi_spots) == 12, str(len(xiaomi_spots)))
    check("小米热点里有一个语音键",
          sum(1 for s in xiaomi_spots if s.get("is_voice")) == 1,
          str([s["button_id"] for s in xiaomi_spots]))
    check("小米热点坐标都在图内",
          all(0 <= s["x"] <= 1 and 0 <= s["y"] <= 1
              and 0 < s["width"] <= 1 and 0 < s["height"] <= 1
              for s in xiaomi_spots),
          str(xiaomi_spots[:2]))
    x_rows = [r["button_id"] for r in controller.buttonList]
    check("小米按键行 12 个",
          len(x_rows) == 12, str(x_rows))
    check("小米按键行含语音键",
          "mic" in x_rows, str(x_rows))
    up_card = items_named(win.contentItem(), "mappingKeyCell_up")
    check("小米的「上」卡片存在", bool(up_card))
    missing_x = items_named(win.contentItem(), "photoMissingHint")
    check("小米布局不显示「实物图缺失」提示",
          not missing_x or not bool(missing_x[0].property("visible")))
    win.grabWindow().save(str(out_dir / "ui_xiaomi_keys.png"))

    # ---------------------------------------------------------- 手机布局 · 自定义键
    print("")
    print("[手机布局 · 自定义键]")
    controller._cfg["remotes"] = [{
        "addr": "50:50:50:50:50:50", "identity": "50:50:50:50:50:50",
        "type": 0, "name": "手机（临时遥控器）", "image": "", "markers": [],
        "kind": "phone",
        "custom_keys": [
            {"id": 1, "name": "截图", "col": 0, "row": 0},
            {"id": 2, "name": "打开记事本", "col": 2, "row": 1},
            {"id": 3, "name": "演示", "col": 1, "row": 2},
        ],
        "bindings": {"bindings": {}, "secondary_bindings": {}},
    }]
    controller.selectRemote(0)
    win.setProperty("currentPage", 1)
    pump(16)

    canvas_bg = items_named(win.contentItem(), "phoneCanvasBg")
    check("手机布局用绘制的快捷键页画布",
          bool(canvas_bg) and bool(canvas_bg[0].property("visible")))
    shots_img = items_named(win.contentItem(), "remotePhoto")
    check("手机布局不显示遥控器实物图",
          not shots_img or not bool(shots_img[0].property("visible")))

    missing = items_named(win.contentItem(), "photoMissingHint")
    check("手机布局不显示「实物图缺失」提示",
          not missing or not bool(missing[0].property("visible")))
    keycell = items_named(win.contentItem(), "mappingKeyCell_ck1")
    check("卡片显示自定义名而不是 ck1",
          bool(keycell) and qml_value(keycell[0], "text") == "截图",
          str(qml_value(keycell[0], "text")) if keycell else "无卡片")

    shot_keys = out_dir / "ui_phone_keys.png"
    win.grabWindow().save(str(shot_keys))

    check("手机背景图存在", bool(controller.phoneShotSource)
          and controller.phoneShotSource.endswith("phone-shot.jpg"),
          controller.phoneShotSource)
    spots = controller.hotspots
    check("自定义键热点带行列（3 个，3×N 网格）",
          len(spots) == 3
          and sorted((s["col"], s["row"]) for s in spots) == [(0, 0), (1, 2), (2, 1)]
          and all(s["rows"] >= 4 for s in spots),
          str([(s["col"], s["row"], s["rows"]) for s in spots]))

    opts = controller.primaryActionOptionsFor("ck1")
    check("自定义键的动作清单与普通键一致", len(opts) >= 10, str(len(opts)))

    card, spot = settled_button("editMapping_ck1", 0)
    check("自定义键卡片存在且坐标可点", card is not None, "找不到 editMapping_ck1")
    if card is not None and spot is not None:
        QTest.mouseClick(win, Qt.LeftButton, Qt.NoModifier, spot)
        opened = wait_for(
            "动作编辑器打开",
            lambda: any(bool(d.property("opened"))
                        for d in win.findChildren(QObject, "actionEditorDialog")))
        check("点自定义键卡片能打开动作编辑器", opened)
        if opened:
            dlg = [d for d in win.findChildren(QObject, "actionEditorDialog")
                   if d.property("opened")][0]
            pump(8)
            win.grabWindow().save(str(out_dir / "ui_phone_editor.png"))
            combos = dlg.findChildren(QObject, "actionEditorPrimaryCombo")
            check("编辑器能找到单击动作输入框", bool(combos))
            if combos:
                combos[0].setProperty("editText", "ctrl+shift+p")
                pump(4)
                saves = dlg.findChildren(QObject, "actionEditorSaveButton")
                check("编辑器能找到「完成」按钮", bool(saves))
                if saves:
                    QMetaObject.invokeMethod(saves[0], "click")
                    pump(10)
            rows = [r for r in controller.buttonList if r["button_id"] == "ck1"]
            check("自定义键的动作保存成功",
                  bool(rows) and rows[0]["single"] == "ctrl+shift+p",
                  rows[0]["single"] if rows else "无行")

    # 拖动换位：ck1(0,0) → (2,3)，应吸附到最后一格并换到右侧
    pump(6)
    moved = controller.moveCustomKey("ck1", 2, 3)
    pump(10)
    rows = [r for r in controller.buttonList if r["button_id"] == "ck1"]
    ck1 = next((k for k in controller.customKeys if k["button_id"] == "ck1"), None)
    check("自定义键可移动到格点",
          moved and bool(rows) and rows[0]["side"] == "right"
          and ck1 is not None and (ck1["col"], ck1["row"]) == (2, 3),
          str((rows[0] if rows else None, ck1)))
    win.grabWindow().save(str(out_dir / "ui_phone_moved.png"))

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
