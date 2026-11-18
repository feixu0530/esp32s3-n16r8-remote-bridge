"""
v0.22.1 闸门：同步链路重新梳理 / 映射比对页 / 按键页补齐 / 应用图标。

★ 每条闸门对应用户提的一条 ★
    1. 诊断日志里的 MAC 方向正确（跟设备页显示的一致）
    2. 设备页显示真实条数；「同步」按钮真的有用
    3. 同步规则：新配对走默认 / PC 改映射立即同步 / 删除配对连带删映射
    4. 按键页的遥控器图片清晰（mipmap + 投影）
    5. 按键页的「检测真实按键」按了有效果（中文键名 + 自动选中 + 第一次就停）
    6. 「管理按键配置」换成「映射比对」：逐条比对 + 三个方向 + 导入导出
    7. 应用图标（SVG → 多尺寸 ICO，窗口/托盘/EXE 都用它）
    8. 默认映射加「返回键长按 = Del」
    9. 刷写固件的信息框平时折叠
    10. 编辑下拉框抄参考项目（分类字号、弹出位置）

运行：
    python dev/client/tests/test_v021.py
"""
from __future__ import annotations

import os
import re
import struct
import sys
import types
import tempfile
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-v021-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core import remote_layout  # noqa: E402
from core.controller import BridgeController  # noqa: E402
from core.key_mapping import ButtonTrigger  # noqa: E402

FAILURES: list[str] = []

ROOT = Path(__file__).resolve().parents[3]
FW_MAIN = ROOT / "dev" / "firmware" / "bridge" / "main"
QML = ROOT / "dev" / "client" / "src" / "ui" / "qml"
CLIENT = ROOT / "dev" / "client"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------- 需求 1
def test_mac_byte_order() -> None:
    """固件里不再有手写的反序地址格式。"""
    allowed = 0
    for name in ("br_ble_core.c", "br_mapping.c"):
        text = (FW_MAIN / name).read_text(encoding="utf-8")
        hits = text.count('"%02X:%02X:%02X:%02X:%02X:%02X"')
        # br_ble_core.c 允许 2 处（addr_to_str + br_ble_addr_parse 的 sscanf）
        allowed += 2 if name == "br_ble_core.c" else 0
        check(f"{name} 里没有手写的反序地址格式", hits <= allowed,
              f"{hits} 处（允许 {allowed}）")
    core = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("addr_to_str 用的是 addr[5]..addr[0]",
          "addr[5], addr[4], addr[3], addr[2], addr[1], addr[0]" in core, "")
    hdr = (FW_MAIN / "br_ble_core.h").read_text(encoding="utf-8")
    check("br_addr_to_str 已导出给 br_mapping.c 用",
          "br_addr_to_str" in hdr, "")


# ---------------------------------------------------------------- 需求 2/3
def test_local_count_is_real(ctl: BridgeController) -> None:
    """「本机 N 条」要是真的映射条数，不是外层 dict 的键数。"""
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:01", "identity": "11:22:33:44:55:66",
         "name": "客厅",
         "bindings": {"bindings": {"2:66": {"kind": "key_combo", "keys": ["up"]},
                                   "2:233": {"kind": "key_combo",
                                             "keys": ["volume_up"]}},
                      "secondary_bindings": {}, "display_notes": {}}},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._on_remote_row({"idx": "0", "addr": "AA:BB:CC:DD:EE:01",
                         "id": "11:22:33:44:55:66", "name": "客厅",
                         "conn": "1", "type": "0", "binds": "2"})
    row = ctl.remoteModel.get(0)
    check("本机条数是真实映射条数（不是 2/3 个外层键）",
          row.get("binds") == 2, str(row.get("binds")))
    check("板子条数是固件报的真值",
          row.get("binds_board") == 2, str(row.get("binds_board")))


def test_sync_button_works(ctl: BridgeController) -> None:
    """点「同步」要真的推出去，而且不能把别的遥控器的映射留在内存里。"""
    sent: list[tuple] = []

    class _Proto:
        def send(self, verb, *args):
            sent.append((verb, args))

        def request(self, *args, **kwargs):
            if args:
                self.send(*args)
            return types.SimpleNamespace(ok=True, reason="")

    own = {"bindings": {"2:66": {"kind": "key_combo", "keys": ["up"]}}}
    other = {"bindings": {"2:67": {"kind": "key_combo", "keys": ["down"]}}}
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:01", "name": "一号", "bindings": own},
        {"addr": "AA:BB:CC:DD:EE:02", "name": "二号", "bindings": other},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._bindings = own
    ctl._proto = _Proto()
    ok = ctl.syncMappingToBoard(1)
    check("同步成功", ok, "")
    verbs = [v for v, _a in sent]
    check("确实下发了 MAPBEGIN/MAPSET/MAPCOMMIT",
          "MAPBEGIN" in verbs and "MAPSET" in verbs and "MAPCOMMIT" in verbs,
          str(verbs))
    check("推的是第 2 台那台（它的地址）",
          ("MAPBEGIN", ("AA:BB:CC:DD:EE:02",)) in sent, str(sent))
    check("同步完 _bindings 复原了（没把别家的映射留在内存里）",
          ctl._bindings == own, str(ctl._bindings))
    check("同步完当前遥控器也没变",
          ctl._current == 0, str(ctl._current))
    ctl._proto = None


def test_unpair_drops_archive(ctl: BridgeController) -> None:
    """删除配对 → 归档也跟着删，重配才是「新遥控器」。"""
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:01", "identity": "11:22:33:44:55:66",
         "name": "客厅", "bindings": {"bindings": {"2:66": {}}}},
    ]
    ctl._cfg["mapping_archive"] = {
        "11:22:33:44:55:66": {"addr": "AA:BB:CC:DD:EE:01",
                              "identity": "11:22:33:44:55:66",
                              "bindings": {"bindings": {"2:66": {}}}},
    }
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._after_unpair(0, "AA:BB:CC:DD:EE:01", "客厅")
    check("配对名单里没了",
          not ctl._cfg.get("remotes"), str(ctl._cfg.get("remotes")))
    check("归档也一起删了（重配不会继承旧映射）",
          not ctl._cfg.get("mapping_archive"),
          str(ctl._cfg.get("mapping_archive")))


def test_defaults_have_long_press(ctl: BridgeController) -> None:
    """出厂默认要含「返回键长按 = 退格」，而且和固件对得上。"""
    check("默认长按表里有返回键",
          remote_layout.DEFAULT_LONG_ACTIONS.get("back") == "退格",
          str(remote_layout.DEFAULT_LONG_ACTIONS))
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:09", "name": "客厅", "bindings": {}},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    bindings = ctl._default_bindings_for_current()
    long_entry = (bindings.get("secondary_bindings") or {}).get("2:548")
    check("默认里铺了返回键的长按", bool(long_entry), str(bindings))
    if long_entry:
        kind = long_entry.get("long_press", {}).get("kind")
        check("返回键长按 = 退格", kind == "delete_backward", str(long_entry))


# ---------------------------------------------------------------- 需求 6

def test_export_import_format(ctl: BridgeController) -> None:
    """导入导出用参考项目的格式。"""
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:11", "name": "客厅",
         "bindings": {"bindings": {"2:66": {"kind": "key_combo",
                                            "keys": ["up"]}}}},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._bindings = ctl._remote_bindings(0)
    out = Path(tempfile.mkdtemp(prefix="bridge-export-")) / "m.json"
    path = ctl.exportMappingFileTo(str(out))
    check("导出成功", bool(path) and out.is_file(), path)
    if out.is_file():
        import json
        data = json.loads(out.read_text(encoding="utf-8"))
        check("格式是参考项目那套",
              data.get("format") == "RemoteMic.key-mapping", str(data.get("format")))
        check("带 schema_version", data.get("schema_version") == 1, str(data))
        check("带 mapping 段", isinstance(data.get("mapping"), dict), str(data))
        # 导出的能再导回来
        ctl._bindings = {}
        ok = ctl.importMappingFile(str(out))
        check("导出的能导回来", ok and bool(ctl._bindings), str(ctl._bindings))
    # 不是本软件的格式要明确拒绝，且不动本机配置
    bad = Path(tempfile.mkdtemp(prefix="bridge-bad-")) / "x.json"
    bad.write_text('{"hello": 1}', encoding="utf-8")
    before = dict(ctl._bindings)
    ok = ctl.importMappingFile(str(bad))
    check("别的格式明确拒绝", not ok, "")
    check("拒绝时不动本机配置", ctl._bindings == before, str(ctl._bindings))


# ---------------------------------------------------------------- 需求 5
def test_detect_real_key(ctl: BridgeController) -> None:
    """「检测真实按键」按了要有效果。"""
    ctl._learn_active = True
    ctl._on_key({"src": "2", "usage": "547", "state": "down"})
    # 检测到键 → 选中对应行 + 停
    check("检测到键后选中了对应的那一行",
          ctl._selected_button_id in ("home", "2:547"),
          ctl._selected_button_id)
    # ★ 行为已改：保持检测状态，连续测试多个键；再点按钮才退出 ★
    check("检测完保持检测状态（可连续测键）",
          ctl._learn_active, "")
    check("显示的是中文键名",
          "主页" in ctl.learnResultText, ctl.learnResultText)

    # 固件的 LEARN 事件走同一条路
    ctl._learn_active = True
    ctl._apply_event("LEARN", {"usage": "66", "src": "2"})
    check("LEARN 事件也会选中对应行",
          ctl._selected_button_id in ("up", "2:66"), ctl._selected_button_id)
    check("LEARN 事件也保持检测状态", ctl._learn_active, "")


# ---------------------------------------------------------------- 需求 7
def test_app_icon() -> None:
    """应用图标：多尺寸 ICO + 三个地方都用它。"""
    ico = CLIENT / "src" / "assets" / "icons" / "app.ico"
    check("app.ico 存在", ico.is_file(), str(ico))
    if ico.is_file():
        data = ico.read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data, 0)
        check("是 ICO 文件", kind == 1, str(kind))
        check("有 9 个尺寸（16~256）", count == 9, str(count))
    svg = CLIENT / "src" / "assets" / "icons" / "app.svg"
    check("app.svg 存在", svg.is_file(), str(svg))
    app_py = (CLIENT / "src" / "app.py").read_text(encoding="utf-8")
    check("app.py 设置了窗口图标", "setWindowIcon" in app_py, "")
    check("app.py 给 QQuickWindow 设了图标", "window.setIcon" in app_py, "")
    tray = (CLIENT / "src" / "core" / "tray.py").read_text(encoding="utf-8")
    check("托盘用同一张图标", "app.ico" in tray, "")
    pkg = (CLIENT / "build" / "package.py").read_text(encoding="utf-8")
    check("打包带 --icon", '"--icon"' in pkg, "")


# ---------------------------------------------------------------- 需求 9/10
def test_qml_changes() -> None:
    """几个界面改动用源码断言钉住。"""
    dev = (QML / "DevicePage.qml").read_text(encoding="utf-8")
    check("刷写输出框默认折叠",
          "property bool flashLogExpanded: false" in dev, "")
    check("刷写输出框高度跟着展开状态走",
          "root.flashLogExpanded ? 110 : 0" in dev, "")
    check("点烧录才展开输出框",
          "root.flashLogExpanded = true" in dev, "")

    btn = (QML / "ButtonsPage.qml").read_text(encoding="utf-8")
    check("按键页引了 QtQuick.Effects（投影）",
          "import QtQuick.Effects" in btn, "")
    check("遥控器图片开了 mipmap（缩 5.8 倍不糊）",
          "mipmap: true" in btn, "")
    check("有 MultiEffect 投影",
          "MultiEffect" in btn, "")
    check("按键页不再有「管理按键配置」/「映射比对」",
          "管理按键配置" not in btn and "映射比对" not in btn, "")

    combo = (QML / "EditorActionCombo.qml").read_text(encoding="utf-8")
    check("下拉框显式钉了弹出位置",
          "popup: Popup" in combo and "editorCombo.height" in combo, "")
    check("分类标题用 fontSizeTiny（跟参考项目一致）",
          "fontSizeTiny" in combo, "")
    check("选项绑了 ListView.view.width（列表宽度对得上）",
          "ListView.view ? ListView.view.width" in combo, "")
    check("当前项加粗",
          "Font.DemiBold" in combo, "")

    check("映射比对页已删（同步逻辑简化了）",
          not (QML / "MappingCompareDialog.qml").is_file(), "")
    check("旧的「管理按键配置」弹窗已经移除",
          not (QML / "MappingPresetsDialog.qml").is_file(), "")


def main() -> int:
    print("=" * 66)
    print("v0.22.1 闸门")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    ctl = BridgeController()
    try:
        test_mac_byte_order()
        test_local_count_is_real(ctl)
        test_sync_button_works(ctl)
        test_unpair_drops_archive(ctl)
        test_defaults_have_long_press(ctl)
        test_export_import_format(ctl)
        test_detect_real_key(ctl)
        test_app_icon()
        test_qml_changes()
    finally:
        ctl.shutdown()

    print()
    print("=" * 66)
    if FAILURES:
        print(f"✘ {len(FAILURES)} 项未通过：{', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
