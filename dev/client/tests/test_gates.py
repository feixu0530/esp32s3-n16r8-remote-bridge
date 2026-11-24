"""
连接闸门 + 按键页闸门。

★ 为什么要有这两条 ★
    v0.14.1 交付时，程序**从来没连过串口**：
    页面显示「开发板已插入」，固件版本却写着「未知」、遥控器「已配对 0 台」、
    蓝牙配对点了没反应 —— 因为一条命令都没发出去。
    根因是「口在不在」和「通道通不通」被混为一谈。

    所以这里把两件事各自钉死：
      1. **连接闸门**：启动后必须真的连上，并且真的读到固件版本和遥控器列表
      2. **按键页闸门**：按键页必须有完整的一列键（固定档案），
         改一个映射必须真的往开发板发 MAPSET

    这两条以后再漏会立刻红。

★ 只用一个控制器 ★
    多个控制器各自带 QTimer 和协议线程，解释器退出时会撞 Qt 析构竞态。

运行：
    python dev/client/tests/test_gates.py
"""
from __future__ import annotations

import faulthandler
import gc
import os
import sys
import tempfile
import time
from pathlib import Path

faulthandler.enable()

# ★ 不要关掉自动连接 ★
#   这条测试就是要验「自动连上并读到东西」。
#   配置用临时目录，别动用户真机配好的遥控器表。
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-gates-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtGui import QGuiApplication          # noqa: E402

from core import remote_layout                      # noqa: E402
from core.controller import BridgeController        # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


class FakeProto:
    """记录发出去的命令，用来断言「映射真的写回开发板」。"""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send(self, *args) -> bool:
        self.sent.append(args)
        return True

    def stop(self) -> None:
        pass

    def request(self, *args, **kwargs):
        from core.protocol import ProtocolResult
        if args:
            self.send(*args)          # 真实实现里 request 也是先发再等
        return ProtocolResult(ok=True, verb=str(args[0]) if args else "")


def wait_until(cond, timeout: float = 8.0) -> bool:
    """
    等条件成立。

    ★ 必须转事件循环 ★
      协议读线程现在只 emit 信号，真正的处理排在 GUI 线程 ——
      不 processEvents 的话应答永远送不到。
    """
    from PySide6.QtCore import QCoreApplication
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_connection_gate(ctl: BridgeController) -> None:
    print("\n[连接闸门]")
    boards = [p for p in ctl.portList if p.get("protocol")]
    if not boards:
        print("  SKIP  没插开发板，这条闸门本次无法验证")
        print("        （插上开发板再跑一次才算真的验过）")
        return

    print(f"        发现 {len(boards)} 个协议通道："
          f"{[p['device'] for p in boards]}")
    result = ctl.connectDefaultPort()
    check("connectDefaultPort 没有要求用户选口",
          result != "__ask__", repr(result))
    check("自动连上了开发板",
          ctl.connected, f"connected={ctl.connected}")

    got_version = wait_until(lambda: bool(ctl.firmwareVersion))
    check("读到了固件版本（INFO 走通了）",
          got_version, f"firmwareVersion={ctl.firmwareVersion!r}")
    if got_version:
        print(f"        固件版本 = {ctl.firmwareVersion}")

    got_remotes = wait_until(lambda: ctl.remoteModel.rowCount() > 0)
    if not got_remotes:
        # ★ 板子上一个遥控器都没配（用户刚删光）★
        #   这时列表本来就是空的，不该算失败。
        print("  SKIP  板子上没有配对任何遥控器（列表为空是正常的）")
        print("        （配上一台之后这条闸门才算真的验过）")
        return
    check("读到了已配对遥控器列表（REMOTES 走通了）",
          got_remotes, f"rows={ctl.remoteModel.rowCount()}")
    if got_remotes:
        names = [ctl.remoteModel.data(ctl.remoteModel.index(i, 0), 257)
                 for i in range(ctl.remoteModel.rowCount())]
        print(f"        遥控器 = {names}")


def test_buttons_page_gate(ctl: BridgeController) -> None:
    print("\n[按键页数据闸门]")
    # ★ 把选中项定死 ★
    #   板子现在还会报「手机（临时遥控器）」，按键页跟着选中项变。
    #   这条闸门考的是**谷歌遥控器那套固定档案**，不能「测成板子上恰好配了什么」。
    ctl._cfg["remotes"] = [{
        "addr": "AA:BB:CC:DD:EE:FF",
        "type": 0,
        "name": "Test Remote",
        "image": "",
        "markers": [],
        "bindings": {},
    }]
    ctl.selectRemote(0)

    rows = ctl.buttonList
    check("按键页有 15 行（固定档案，不是标记驱动）",
          len(rows) == 15, f"rows={len(rows)}")

    left = [r for r in rows if r["side"] == "left"]
    right = [r for r in rows if r["side"] == "right"]
    check("左列 7 行 / 右列 8 行",
          len(left) == 7 and len(right) == 8,
          f"left={len(left)} right={len(right)}")

    up = next((r for r in rows if r["button_id"] == "up"), None)
    check("「上」键的 key_id 正确（消费者页 usage 0x42）",
          up is not None and up["key_id"] == "2:66",
          repr(up["key_id"]) if up else "找不到 up")

    home = next((r for r in rows if r["button_id"] == "home"), None)
    check("「主页」键的 key_id 正确（usage 0x223 = 547）",
          home is not None and home["key_id"] == "2:547",
          repr(home["key_id"]) if home else "找不到 home")

    check("实物图热点有 15 个",
          len(ctl.hotspots) == 15, f"hotspots={len(ctl.hotspots)}")
    check("档案表与固件的 usage 表一致",
          remote_layout.CHROMECAST_CONSUMER_USAGES.get("home") == 0x0223,
          str(remote_layout.CHROMECAST_CONSUMER_USAGES.get("home")))


def test_phone_layout_gate(ctl: BridgeController) -> None:
    """
    手机（临时遥控器）那套布局。

    ★ 为什么单独考 ★
      手机是「像遥控器一样的移动端」，但它不是 BLE 遥控器：
      没有实物图、没有上/下/左/右，键位空间也与遥控器隔离。
      这些都是「两套布局不能串」的硬约束，串了就出诡异 bug。
    """
    print("\n[手机端布局闸门]")
    ctl._cfg["remotes"] = [{
        "addr": "50:50:50:50:50:50",
        "type": 0,
        "name": "手机（临时遥控器）",
        "image": "",
        "markers": [],
        "kind": "phone",
        "bindings": {},
    }]
    ctl.selectRemote(0)

    rows = ctl.buttonList
    check("手机按键页只列自定义键（默认 0 行）",
          len(rows) == 0, f"rows={len(rows)}")
    check("手机每个键都有 key_id",
          all(r["key_id"] for r in rows),
          str([r["key_id"] for r in rows]))
    check("手机 key_id 前缀是 6（与遥控器的 2 隔离）",
          all(r["key_id"].startswith("6:") for r in rows),
          str([r["key_id"] for r in rows]))
    check("手机没有实物图热点",
          len(ctl.hotspots) == 0, f"hotspots={len(ctl.hotspots)}")
    check("手机与遥控器 key_id 不重合",
          not ({r["key_id"] for r in rows}
               & {"2:%d" % u for u in
                  remote_layout.CHROMECAST_CONSUMER_USAGES.values()}))


def test_phone_first_select_gate(ctl: BridgeController) -> None:
    """首屏：_current=-1 时自动选中手机，按键行必须跟着重建（不能留谷歌键）。"""
    print("")
    print("[首屏选中闸门]")
    ctl._cfg["remotes"] = [{
        "addr": "50:50:50:50:50:50",
        "type": 0,
        "name": "手机（临时遥控器）",
        "image": "",
        "markers": [],
        "kind": "phone",
        "bindings": {"bindings": {}, "secondary_bindings": {}},
        "custom_keys": [
            {"id": 1, "name": "切换任务", "col": 0, "row": 0},
            {"id": 2, "name": "截图", "col": 2, "row": 1},
        ],
    }]
    ctl._current = -1
    ctl._rebuild_remote_model()
    ctl._ensure_current_remote()
    rows = ctl.buttonList
    check("首屏自动选中手机后按键行已重建",
          len(rows) == 2 and all(r["button_id"].startswith("ck") for r in rows),
          str([r["button_id"] for r in rows]))


def test_phone_many_keys_gate(ctl: BridgeController) -> None:
    """27 个自定义键：左右卡片不能落在同一行（会互相遮住）。"""
    print("")
    print("[手机多键分行闸门]")
    keys = [{"id": i, "name": "键 %d" % i,
             "col": (i - 1) % 3, "row": (i - 1) // 3} for i in range(1, 28)]
    ctl._cfg["remotes"] = [{
        "addr": "50:50:50:50:50:50", "type": 0, "name": "手机",
        "image": "", "markers": [], "kind": "phone",
        "bindings": {"bindings": {}, "secondary_bindings": {}},
        "custom_keys": keys,
    }]
    ctl.selectRemote(0)
    rows = ctl.buttonList
    pairs = [(r["side"], r["visual_row"]) for r in rows]
    check("27 个键全部进了卡片列表", len(rows) == 27, f"rows={len(rows)}")
    check("同侧没有重复行号", len(pairs) == len(set(pairs)), str(sorted(pairs)))


def test_mapping_writeback_gate(ctl: BridgeController) -> None:
    print("\n[映射写回开发板闸门]")
    ctl._cfg["remotes"] = [{
        "addr": "AA:BB:CC:DD:EE:FF",
        "type": 0,
        "name": "Test Remote",
        "image": "",
        "markers": [],
        "bindings": {},
    }]
    ctl.selectRemote(0)
    proto = FakeProto()
    ctl._proto = proto

    ok = ctl.setMapping("2:66", "single_click", "显示桌面")
    check("设置单击映射成功", ok)
    check("往开发板发了 MAPBEGIN",
          any(c[0] == "MAPBEGIN" for c in proto.sent), str(proto.sent))
    mapset = [c for c in proto.sent if c[0] == "MAPSET"]
    check("发了 MAPSET", len(mapset) >= 1, str(proto.sent))
    if mapset:
        args = mapset[0]
        # key = (source<<12)|usage = (2<<12)|0x42 = 8258
        check("MAPSET 的 key 是 8258（消费者页 0x42）",
              args[1] == 8258, str(args))
        check("MAPSET 的 trig 是 0（单击）", args[2] == 0, str(args))
        # 显示桌面 = win+d → 修饰键位掩码 0x08（左 Win）+ 普通键 'd'
        check("「显示桌面」按 win+d 下发（修饰键位掩码 0x08）",
              args[4] == 0x08, str(args))
    check("发了 MAPCOMMIT",
          any(c[0] == "MAPCOMMIT" for c in proto.sent), str(proto.sent))

    # 双击 / 长按也要能写出去
    #   注意：每保存一次都会把**整套**映射重发一遍（MAPBEGIN 会清空暂存区），
    #   所以这里看到 trig 0 也在列表里 —— 那是之前设的单击，不是错。
    proto.sent.clear()
    ctl.setMapping("2:66", "double_click", "方向上")
    ctl.setMapping("2:66", "long_press", "Escape")
    trigs = sorted(set(c[2] for c in proto.sent if c[0] == "MAPSET"))
    check("双击(1) 和 长按(2) 都写回了开发板",
          trigs == [0, 1, 2], str(trigs))
    double_sets = [c for c in proto.sent
                   if c[0] == "MAPSET" and c[2] == 1]
    check("双击那次的键是「方向上」（键盘键 0x52）",
          bool(double_sets) and double_sets[-1][6] == 0x52,
          str(double_sets[-1]) if double_sets else "没有")

    # 音量是消费者页，要走 out=2，不能混进键盘报告
    proto.sent.clear()
    ctl.setMapping("2:66", "single_click", "系统音量 +")
    cons = [c for c in proto.sent if c[0] == "MAPSET" and c[3] == 2]
    check("「系统音量 +」走消费者输出（out=2）",
          len(cons) == 1, str(proto.sent))
    if cons:
        check("消费者 usage 是 0x00E9（音量 +）",
              cons[0][6] == 0x00E9, str(cons[0]))

    # 固件的 trig 编号必须和板子对得上
    from core.key_mapping import TRIGGER_INDEX
    check("固件 trig 编号与板子一致（单击0/双击1/长按2）",
          sorted(TRIGGER_INDEX.values()) == [0, 1, 2],
          str(TRIGGER_INDEX))


def test_app_imports_gate() -> None:
    """
    app.py 必须能正常导入。

    ★ 为什么单独钉这一条 ★
      v0.15.0 第一次打包时，app.py 里还留着已删除的 `MarkerModel` 导入，
      源码的 compileall 和界面测试都发现不了（它们不 import app.py），
      结果**打包后的 EXE 一启动就崩**。
      这条闸门把「EXE 启动即崩」这类问题挡在打包之前。
    """
    print("\n[入口导入闸门]")
    try:
        import app  # noqa: F401
    except Exception as exc:
        check("app.py 能正常导入", False, f"{type(exc).__name__}: {exc}")
        return
    check("app.py 能正常导入", True)


def main() -> int:
    print("=" * 66)
    print("连接闸门 + 按键页闸门")
    print("=" * 66)

    app = QGuiApplication(sys.argv)

    test_app_imports_gate()

    ctl = BridgeController()
    try:
        test_connection_gate(ctl)
        test_buttons_page_gate(ctl)
        test_phone_layout_gate(ctl)
        test_phone_first_select_gate(ctl)
        test_phone_many_keys_gate(ctl)
        test_mapping_writeback_gate(ctl)
    finally:
        # 收尾：停掉协议线程和 QTimer，别让 Qt 在解释器退出时炸
        ctl.disconnectDevice()
        del ctl
        gc.collect()

    print()
    print("=" * 66)
    if FAILURES:
        print(f"✘ {len(FAILURES)} 项未通过：{', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
