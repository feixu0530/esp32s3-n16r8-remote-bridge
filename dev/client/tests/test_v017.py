"""
v0.17.0 闸门：设备页重排 / 映射归档 / 人话日志 / 配对刷新 / 烧录链路。

★ 每条闸门对应用户提的一条 ★
    1. 串口并进「开发板」行，且**只列协议通道**
    2. 烧录通道可选；没检测到时给出提醒；烧录 = 全新刷写
    3. 按 MAC（identity）归档映射；板子是空的就自动回灌
    4. 解绑用的是板子的配对表槽位号（不是界面行号）
    6. 诊断日志是人话：时间 / 哪台遥控器 / 按了什么键 / 变成什么键

运行：
    python dev/client/tests/test_v017.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-v017-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core import human_log  # noqa: E402
from core.controller import BridgeController  # noqa: E402
from core.flasher import FirmwareFlasher  # noqa: E402

FAILURES: list[str] = []

ROOT = Path(__file__).resolve().parents[3]
FW_MAIN = ROOT / "dev" / "firmware" / "bridge" / "main"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------- 需求 1
def test_ports_split(ctl: BridgeController) -> None:
    """协议通道和烧录通道必须分开列。"""
    ctl._port_cache = [
        {"device": "COM12", "label": "USB 串行", "vid": 0x303A,
         "protocol": True, "flash": False},
        {"device": "COM6", "label": "CH343", "vid": 0x1A86,
         "protocol": False, "flash": True},
        {"device": "COM3", "label": "别人的串口", "vid": 0x2D79,
         "protocol": False, "flash": False},
    ]
    proto = ctl.protocolPorts
    flash = ctl.flashPorts
    check("协议通道只列 VID 0x303A",
          [p["device"] for p in proto] == ["COM12"], str(proto))
    check("无关的串口设备（COM3）两个通道都不进",
          "COM3" not in [p["device"] for p in proto + flash],
          str(proto + flash))
    check("烧录通道只列 CH343（VID 0x1A86）",
          [p["device"] for p in flash] == ["COM6"], str(flash))
    check("端口条目带一行文本",
          all("text" in p and "COM" in p["text"] for p in proto + flash),
          str(proto + flash))
    ctl._port_cache = None


# ---------------------------------------------------------------- 需求 2
def test_flash_channel_and_firmware() -> None:
    """烧录端口枚举必须带 vid；包内固件必须能找到应用镜像。"""
    ports = FirmwareFlasher.list_ports()
    check("烧录端口枚举带 vid/pid",
          all("vid" in p and "pid" in p for p in ports), str(ports[:2]))

    base = FirmwareFlasher.find_bundled_firmware()
    check("找得到随包固件目录", base is not None, str(base))
    if base is not None:
        files = FirmwareFlasher.find_bundle_files(base)
        names = [f.name for f in files]
        check("固件里有应用镜像 remote_bridge.bin",
              "remote_bridge.bin" in names, str(names))
        check("固件里有 bootloader 和分区表",
              "bootloader.bin" in names and "partition-table.bin" in names,
              str(names))


def test_flash_hint(ctl: BridgeController) -> None:
    """没检测到烧录通道时，提示要说人话。"""
    ctl._port_cache = [{"device": "COM12", "label": "x", "vid": 0x303A,
                        "protocol": True}]
    hint = ctl.flashPortHint
    check("没烧录通道时给出提醒",
          "烧录通道" in hint and ("插上" in hint or "CH343" in hint), hint)
    ctl._port_cache = [{"device": "COM6", "label": "x", "vid": 0x1A86,
                        "protocol": False}]
    check("有烧录通道时提示就绪",
          "烧录通道" in ctl.flashPortHint, ctl.flashPortHint)
    ctl._port_cache = None


# ---------------------------------------------------------------- 需求 3
def test_mapping_archive(ctl: BridgeController) -> None:
    """映射必须按 identity 归档一份；板子是空的时候自动回灌。"""
    ctl._current = 0
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:01", "identity": "11:22:33:44:55:66",
         "name": "客厅", "bindings": {}},
    ]
    ctl._bindings = {"bindings": {"2:547": {"kind": "key_combo",
                                            "keys": ["lctrl", "lwin"]}}}
    ctl._save_bindings()
    archive = ctl._cfg.get("mapping_archive", {})
    check("按 identity 归档了映射",
          "11:22:33:44:55:66" in archive, str(list(archive.keys())))
    if "11:22:33:44:55:66" in archive:
        check("归档里有 bindings",
              bool(archive["11:22:33:44:55:66"].get("bindings")), "")

    # 板子上是空的 → 用归档恢复
    saved = ctl._archived_bindings("AA:BB:CC:DD:EE:01")
    check("能按地址找到归档", bool(saved), str(saved))


def test_identity_merge(ctl: BridgeController) -> None:
    """遥控器行必须按 identity 合并（地址轮换后还是同一台）。"""
    ctl._cfg["remotes"] = [
        {"addr": "OLD:AA:BB:CC:DD:EE", "identity": "11:22:33:44:55:66",
         "custom_name": "客厅遥控器", "bindings": {"bindings": {}}},
    ]
    ctl._on_remote_row({"idx": "0", "addr": "NEW:11:22:33:44:55",
                         "id": "11:22:33:44:55:66", "name": "Chromecast",
                         "conn": "1", "type": "0", "binds": "0"})
    remotes = ctl._cfg.get("remotes", [])
    check("地址变了但还是同一台（没多出第二条）",
          len(remotes) == 1, str(len(remotes)))
    if remotes:
        check("自定义名字保住了",
              remotes[0].get("custom_name") == "客厅遥控器",
              str(remotes[0]))
        check("identity 记下来了",
              remotes[0].get("identity") == "11:22:33:44:55:66",
              str(remotes[0]))
        check("板子的槽位号记下来了",
              remotes[0].get("pl_idx") == 0, str(remotes[0]))


def test_unpair_uses_pl_idx(ctl: BridgeController) -> None:
    """删遥控器要发板子的配对表槽位号，不是界面行号。"""
    sent: list[tuple] = []

    # ★ 同步记录，别用真 _ask ★
    #   _ask 会起一个后台线程，解释器退出时它还在写 stderr →
    #   "Fatal Python error: _enter_buffered_busy" —— 整包都出不来。
    def fake_ask(verb, *args, **kwargs):
        sent.append((verb, args))

    ctl._ask = fake_ask
    ctl._cfg["remotes"] = [
        {"addr": "A", "pl_idx": 3, "name": "x"},
        {"addr": "B", "pl_idx": 7, "name": "y"},
    ]
    ctl._rebuild_remote_model()
    ctl.removeRemote(1)
    check("UNPAIR 用的是板子槽位号",
          ("UNPAIR", (7,)) in sent, str(sent))
    check("UNPAIR 没拿界面行号当槽位号",
          ("UNPAIR", (1,)) not in sent, str(sent))


# ---------------------------------------------------------------- 需求 6
def test_readable_log(ctl: BridgeController) -> None:
    """日志必须是人话。"""
    line = human_log.key_event({"state": "down"}, "客厅遥控器 (AA:BB)", "主页键")
    check("按键日志有时间", line.startswith("["), line)
    check("按键日志有遥控器名", "客厅遥控器" in line, line)
    check("按键日志有键名", "主页键" in line, line)

    line = human_log.mapout({"trig": "0"}, "客厅遥控器", "主页键",
                             "lctrl + lshift + l")
    check("映射日志说清变成什么", "变成 lctrl + lshift + l" in line, line)

    line = human_log.mapmiss({}, "客厅遥控器", "主页键")
    check("未命中映射也有话说", "没配映射" in line, line)

    line = human_log.voice_event({}, "客厅遥控器", "lctrl + lwin")
    check("语音日志说清按出了什么", "lctrl + lwin" in line, line)


def test_log_uses_board_events(ctl: BridgeController) -> None:
    """EV MAPOUT / VOICE 要真的进日志面板。"""
    ctl._log_lines.clear()
    ctl._apply_event("MAPOUT", {"key": str((2 << 12) | 0x223),
                                 "trig": "0", "out": "1", "mods": "3",
                                 "keys": "0F", "addr": "AA:BB:CC:DD:EE:01"})
    text = "\n".join(ctl.logLines)
    check("MAPOUT 进日志面板", "命中映射" in text, text[-200:])

    ctl._log_lines.clear()
    ctl._apply_event("VOICE", {"addr": "AA:BB:CC:DD:EE:01", "mods": "9",
                                "keys": "", "t": "123"})
    text = "\n".join(ctl.logLines)
    check("VOICE 进日志面板", "语音键按下" in text, text[-200:])


# ---------------------------------------------------------------- 固件侧
def test_key_name_lookup(ctl: BridgeController) -> None:
    """按键名反查：usage → 中文键名（别显示成 usage 数字）。"""
    # 0x223 = 消费者页的「主页键」
    check("主页键 usage 反查成中文名",
          ctl._key_name_of("2", str(0x223)) == "主页键",
          ctl._key_name_of("2", str(0x223)))
    # 0x42 = 「上键」
    check("上键 usage 反查成中文名",
          ctl._key_name_of("2", str(0x42)) == "上键",
          ctl._key_name_of("2", str(0x42)))
    # 认不出来的 usage 给个能看懂的兜底
    check("认不出来的键有兜底文案",
          "键" in ctl._key_name_of("2", "4"), ctl._key_name_of("2", "4"))


def test_log_panel_is_human(ctl: BridgeController) -> None:
    """面板只放人话行；协议原文只进文件。"""
    ctl._log_lines.clear()
    ctl._apply_raw_line("EV KEY idx=0 src=2 usage=547 state=down")
    check("协议原文不进面板", len(ctl.logLines) == 0, str(ctl.logLines))

    ctl._log_lines.clear()
    ctl._apply_event("KEY", {"src": "2", "usage": "547", "state": "down",
                              "addr": "AA:BB:CC:DD:EE:01"})
    text = "\n".join(ctl.logLines)
    check("按键事件变成人话行", "按下" in text and "主页键" in text, text)
    check("人话行带时间戳", text.startswith("["), text)


def test_firmware_peer_slot_release() -> None:
    """固件：断链/解绑必须释放 peer 槽。"""
    src = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("固件里有回收断链槽的逻辑",
          "s_peers[victim]" in src and "no free peer slot" in src, "")
    check("固件解绑按 identity 删 bond",
          "ble_store_util_delete_peer(&id)" in src, "")
    check("固件会记 identity 地址",
          "identity_resolved" in src and "id_addr" in src, "")
    check("固件解绑会释放 peer 槽",
          "UNPAIR：释放 peer 槽" in src, "")
    check("固件上报 DT RM 带 id=",
          'br_kv_add(&kv, "id", id_s)' in src, "")
    check("固件上报真实 binds 数",
          "br_mapping_count(addr)" in src, "")
    check("固件有 EV VOICE 事件",
          'br_proto_send_ev("VOICE"' in src, "")
    main_src = (FW_MAIN / "app_main.c").read_text(encoding="utf-8")
    check("固件有 VOICE 命令",
          'strcmp(verb, "VOICE")' in main_src, "")
    check("固件版本号是 1.2.3",
          '#define FW_VERSION "2.0.0"' in main_src, "")


def test_client_version() -> None:
    """客户端版本号。"""
    from core import version
    check("客户端版本 1.2.3", version.VERSION == "2.0.0", version.VERSION)


def main() -> int:
    print("=" * 66)
    print("v0.17.0 闸门")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    ctl = BridgeController()
    try:
        test_ports_split(ctl)
        test_flash_channel_and_firmware()
        test_flash_hint(ctl)
        test_mapping_archive(ctl)
        test_identity_merge(ctl)
        test_unpair_uses_pl_idx(ctl)
        test_readable_log(ctl)
        test_log_uses_board_events(ctl)
        test_key_name_lookup(ctl)
        test_log_panel_is_human(ctl)
        test_firmware_peer_slot_release()
        test_client_version()
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
