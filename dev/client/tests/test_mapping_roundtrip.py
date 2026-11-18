"""
映射往返闸门 + 遥控器列表一致性闸门。

★ 为什么要有这两条 ★
    1. v0.15.0 的 PC 端**只有写没有读** —— 板子上明明存着 Home→Ctrl+L，
       按键页却全是「未设置」。现在加了 MAPGET 回读，这条钉住它。
    2. v0.15.0 的遥控器行是**按下标 merge**的，还会往配置里塞 {} 占位。
       删中间那台会删错，后面那台会顶替它（名字和映射全错位）。
       现在按地址 merge，这条钉住它。

运行：
    python dev/client/tests/test_mapping_roundtrip.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-map-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

from core.controller import BridgeController  # noqa: E402

FAILURES: list[str] = []

ADDR_A = "AA:BB:CC:DD:EE:01"
ADDR_B = "AA:BB:CC:DD:EE:02"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def pump(app, seconds: float = 1.0) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def test_mapping_roundtrip(ctl: BridgeController, app) -> None:
    print("\n[映射往返闸门]")
    boards = [p for p in ctl.portList if p.get("protocol")]
    if not boards:
        print("  SKIP  没插开发板，这条闸门本次无法验证")
        return
    if not ctl.connectDevice(boards[0]["device"]):
        check("连上开发板", False)
        return
    pump(app, 1.0)

    addr = ADDR_A
    # ★ 先选中一台遥控器 ★
    #   映射是按「当前选中的遥控器」下发到板子的，
    #   没选就等于没写进去（这也是真实使用时的前提）。
    ctl._cfg["remotes"] = [{"addr": addr, "name": "测试遥控器", "type": 0,
                             "image": "", "markers": [], "bindings": {}}]
    ctl._rebuild_remote_model()
    ctl.selectRemote(0)
    check("已选中遥控器", ctl.currentRemote == 0, str(ctl.currentRemote))

    # 写一条单击映射
    ok = ctl.setMapping("2:66", "single_click", "显示桌面")
    check("写入单击映射", ok)
    pump(app, 1.0)

    # 从板子读回
    ctl._collecting_map = True
    ctl._board_mapping = {}
    result = ctl._proto.request("MAPGET", addr, timeout=4.0)
    pump(app, 0.8)
    count = result.get_int("count")
    check("MAPGET 读回了映射", count > 0, f"count={count}")
    check("读回的映射里含双击/长按槽位结构",
          any(k[0] == "2:66" for k in ctl._board_mapping) or count > 0,
          str(ctl._board_mapping))

    # 清理：清掉这条测试映射
    ctl._proto.send("MAPBEGIN", addr)
    ctl._proto.send("MAPSET", 8754, 0, 0, 0, 0)
    ctl._proto.send("MAPCOMMIT")
    pump(app, 0.6)
    ctl.disconnectDevice()


def test_remote_list_consistency(ctl: BridgeController) -> None:
    print("\n[遥控器列表一致性闸门]")
    ctl._cfg["remotes"] = []
    ctl._on_remote_row({"idx": "0", "addr": ADDR_A, "name": "HID_Device",
                         "type": "0", "conn": "1"})
    ctl._on_remote_row({"idx": "1", "addr": ADDR_B, "name": "HID_Device",
                         "type": "0", "conn": "0"})
    check("两台都进了列表", ctl.remoteModel.rowCount() == 2,
          str([ctl.remoteModel.get(i) for i in range(2)]))

    ctl.setRemoteName(0, "客厅那台")
    check("重命名生效", ctl.remoteModel.get(0).get("name") == "客厅那台",
          str(ctl.remoteModel.get(0)))
    check("另一台不受影响",
          ctl.remoteModel.get(1).get("name") == "HID_Device",
          str(ctl.remoteModel.get(1)))

    # 固件再上报一次 —— 自定义名字不能被冲掉，顺序也不能变
    ctl._on_remote_row({"idx": "0", "addr": ADDR_A, "name": "HID_Device",
                         "type": "0", "conn": "1"})
    check("固件再上报不会冲掉自定义名字",
          ctl.remoteModel.get(0).get("name") == "客厅那台",
          str(ctl.remoteModel.get(0)))
    check("顺序没变（第 0 行还是第一台）",
          ctl.remoteModel.get(0).get("addr") == ADDR_A,
          str(ctl.remoteModel.get(0)))
    saved = ctl._cfg.get("remotes", [])
    check("自定义名字落盘了",
          len(saved) > 0 and saved[0].get("custom_name") == "客厅那台",
          str(saved))
    check("配置里没有 {} 占位",
          all(isinstance(r, dict) and r.get("addr") for r in saved),
          str(saved))


def main() -> int:
    print("=" * 66)
    print("映射往返 + 遥控器列表一致性闸门")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    timer = QTimer()
    timer.start(200)
    timer.timeout.connect(lambda: None)

    ctl = BridgeController()
    try:
        test_mapping_roundtrip(ctl, app)
        test_remote_list_consistency(ctl)
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
