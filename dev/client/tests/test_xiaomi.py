"""
小米蓝牙语音遥控器一代 —— 专项闸门。

★ 这些断言都是「真机实测出来的结论」★
   证据：dev/docs/xiaomi-gen1-capture.md
   改动键位表/默认档案/布局时，这个闸门必须一起改，否则说明有人改了没验证的事实。

1. 广播名认得出这台遥控器（"MI RC" / "小米蓝牙语音遥控器"）
2. 按键走**键盘页**（source=1），键位 usage 是实测那一张
3. 固件出厂默认表 ↔ 客户端默认档案逐条一致（12 条，含返回键长按）
4. 键位空间与谷歌遥控器隔离（1: vs 2:）
5. 实物图存在、热点覆盖 12 个键（含语音键）

运行：
    python dev/client/tests/test_xiaomi.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-xiaomi-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import remote_layout  # noqa: E402

FAILURES: list[str] = []

ROOT = Path(__file__).resolve().parents[3]
FW_MAIN = ROOT / "dev" / "firmware" / "bridge" / "main"
ASSETS = ROOT / "dev" / "client" / "src" / "assets" / "remotes"
CLIENT_SRC = ROOT / "dev" / "client" / "src"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ★ 真机实测的键位表（2026-11-24 抓包）★
MEASURED_USAGES = {
    "power": 0x66,
    "up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4F,
    "ok": 0x28,
    "home": 0x4A,
    "back": 0xF1,
    "menu": 0x65,
    "volume_up": 0x80, "volume_down": 0x81,
}


def test_names() -> None:
    """广播名认得出这台遥控器。"""
    for name in ("MI RC", "mi rc", "小米蓝牙语音遥控器", "Xiaomi Bluetooth Remote 2 Pro"):
        prof = remote_layout.profile_for_name(name)
        check("认得出 %s" % name,
              prof is not None and prof.get("id") == remote_layout.LAYOUT_XIAOMI,
              str(prof))
    check("谷歌遥控器不受影响",
          (remote_layout.profile_for_name("Chromecast Remote") or {}).get("id")
          == remote_layout.LAYOUT_CHROMECAST)


def test_usages() -> None:
    """键位 usage 就是实测那一张。"""
    check("usage 表与实测一致",
          remote_layout.XIAOMI_USAGES == MEASURED_USAGES,
          str(remote_layout.XIAOMI_USAGES))
    check("按键走键盘页（source=1）",
          remote_layout.button_key_id_for(remote_layout.LAYOUT_XIAOMI, "up")
          == "1:82",
          remote_layout.button_key_id_for(remote_layout.LAYOUT_XIAOMI, "up"))
    check("语音键没有可配的 usage",
          remote_layout.button_key_id_for(remote_layout.LAYOUT_XIAOMI, "mic") == "")
    check("语音键被标成语音键",
          remote_layout.is_voice_button_for(remote_layout.LAYOUT_XIAOMI, "mic"))
    check("谷歌遥控器仍走消费者页（source=2）",
          remote_layout.button_key_id_for(remote_layout.LAYOUT_CHROMECAST, "up")
          == "2:66")


def test_key_space_isolated() -> None:
    """两台遥控器的 key_id 空间必须物理隔离。"""
    xiaomi = set(remote_layout.button_key_id_for(remote_layout.LAYOUT_XIAOMI, b)
                 for b in MEASURED_USAGES)
    google = set(remote_layout.button_key_id_for(remote_layout.LAYOUT_CHROMECAST, b)
                 for b in ("up", "down", "left", "right", "ok", "home", "back"))
    check("小米全是键盘页前缀", all(k.startswith("1:") for k in xiaomi), str(xiaomi))
    check("谷歌全是消费者页前缀", all(k.startswith("2:") for k in google), str(google))
    check("两边没有交集", not (xiaomi & google), str(xiaomi & google))


def test_firmware_profile() -> None:
    """固件里必须有这张型号档案。"""
    src = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    check("固件有小米型号档案", '"xiaomi"' in src and "k_binds_xiaomi" in src)
    check("档案按名字匹配（多个名字用 | 分隔）",
          "mi rc|小米蓝牙语音遥控器" in src)
    check("档案名匹配支持多个 token", "profile_name_matches" in src)
    check("小米档案走键盘页 source",
          "BR_KEY_MAKE(BR_SRC_KEYBOARD" in src)


def test_firmware_recognizes_xiaomi() -> None:
    """固件扫描阶段要认得小米这个名字。"""
    src = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("名字 token 表里有小米", '"mi rc"' in src and '"小米"' in src)
    check("小米那一台放开数据通道订阅",
          "br_hid_set_xiaomi" in src and "is_xiaomi_name" in src)


def test_firmware_xiaomi_voice_connection_params() -> None:
    """小米语音使用 7.5ms 间隔，但必须保留遥控器要求的 latency 299 / 12s。"""
    src = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    request = src.split("static void request_conn_params", 1)[1].split(
        "/* ------------------------------------------------------------ 连接与事件 */", 1)[0]
    check("小米连接标记为语音低延迟参数", "p->fast_params" in request)
    check("小米语音连接间隔请求 7.5ms",
          "p->fast_params ? 6 : 24" in request)
    check("小米语音 latency 与 Windows 记录一致（299）",
          "p->fast_params ? 299 : 8" in request)
    check("小米语音 supervision timeout 与 Windows 记录一致（12s）",
          "p->fast_params ? 1200 : 500" in request)
    check("HID 通道打开后重新请求小米语音参数",
          "p->fast_params = true" in src and "p->params_pending = true" in src)
    update = src.split("case BLE_GAP_EVENT_CONN_UPDATE", 1)[1][:1800]
    check("小米参数被遥控器改坏后会重新请求",
          "p->fast_params" in update and "p->params_pending = true" in update)
    check("重新请求前检查实际 interval/latency/timeout",
          "desc.conn_itvl != 6" in update
          and "desc.conn_latency != 299" in update
          and "desc.supervision_timeout != 1200" in update)
    check("参数回退重试有节流",
          "next_params_retry_us" in src)


def test_firmware_logs_data_length() -> None:
    """60 字节语音报告需要足够 Rx payload，必须记录实际 DLE 协商。"""
    src = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("固件处理数据长度变化事件", "case BLE_GAP_EVENT_DATA_LEN_CHG" in src)
    event = src.split("case BLE_GAP_EVENT_DATA_LEN_CHG", 1)[-1].split(
        "case BLE_GAP_EVENT_NOTIFY_RX", 1)[0]
    check("日志包含主机 Rx octets/time",
          "event->data_len_chg.max_rx_octets" in event
          and "event->data_len_chg.max_rx_time" in event)
    check("日志包含主机 Tx octets/time",
          "event->data_len_chg.max_tx_octets" in event
          and "event->data_len_chg.max_tx_time" in event)
    check("DLE 结果透传到协议日志",
          'br_proto_send_ev("DLE"' in event
          and '"rx_octets"' in event and '"tx_octets"' in event)


def test_firmware_requests_xiaomi_data_length() -> None:
    """小米音频报告需要 DLE，主机应主动发起标准 LE Set Data Length。"""
    src = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    open_channels = src.split("static void open_channels", 1)[1].split(
        "static void ensure_encrypted", 1)[0]
    check("仅对小米连接请求大数据长度",
          "if (xiaomi)" in open_channels
          and "ble_gap_set_data_len" in open_channels)
    check("请求 251 字节 LL payload",
          "ble_gap_set_data_len(p->conn_handle, 251, 2120)" in open_channels)
    check("DLE 请求结果写入协议日志",
          'br_proto_send_ev("DLEREQ"' in open_channels and '"rc"' in open_channels)


def test_hidmap_parser() -> None:
    """Report Map 解析器不能把报告的 page/usage 丢掉。"""
    src = (FW_MAIN / "br_hidmap.c").read_text(encoding="utf-8")
    check("首次见到报告时记下 page/usage",
          "if (!e->seen)" in src and "e->page =" in src)
    check("赋值发生在标记 seen 之前",
          src.index("if (!e->seen)") < src.index("e->page ="),
          "顺序不对")


def test_firmware_hid_write_properties():
    """HID Control Point 是 Write Without Response，不能发 Write Request。"""
    src = (FW_MAIN / "br_hid.c").read_text(encoding="utf-8")
    probe_write = src.split("bool br_hid_probe_write", 1)[1].split("static void emit_key", 1)[0]
    cp_write = src.split("case STEP_DONE", 1)[1].split("if (!c->verify_tried)", 1)[0]
    check("诊断写会读取已发现的特征属性",
          "br_ble_peer_char_at" in probe_write and ".props" in probe_write)
    check("Write Without Response 特征使用 Write Command",
          "BLE_GATT_CHR_PROP_WRITE_NO_RSP" in probe_write
          and "ble_gattc_write_no_rsp_flat" in probe_write)
    check("自动 Exit Suspend 也按特征属性选择写方式",
          "BLE_GATT_CHR_PROP_WRITE_NO_RSP" in cp_write
          and "ble_gattc_write_no_rsp_flat" in cp_write)
    check("Exit Suspend 控制值是 0x01",
          "exit_suspend[1] = {0x01}" in cp_write)


def test_firmware_xiaomi_audio_enable() -> None:
    """TV 实测的开麦路径：Report ID 4 + rid1 的 0x3E。"""
    src = (FW_MAIN / "br_hid.c").read_text(encoding="utf-8")
    check("按 Report ID 4 找开麦控制", "r->report_id == 4" in src)
    check("开麦载荷第 3 字节 0x80", "pkt[3] = 0x80" in src)
    check("开麦第 4 字节随按键 01/00",
          "pkt[4] = c->audio_enable_on ? 0x01 : 0x00" in src)
    check("键盘报告 0x3E 是语音键主判据", "data[2] == 0x3E" in src)
    check("键盘与控制报告去重", "voice_down" in src)
    check("开麦命令在 GATT 空闲时补发", "audio_enable_pending" in src)
    check("音频大包只把前 N 条发给协议侧",
          "r->raw_total <= BR_HID_RAW_LOG_MAX" in src)
    check("不再用 raw_logged 做永久发送判据",
          "r->raw_logged <= BR_HID_RAW_LOG_MAX" not in src)


def test_firmware_feature_reports_never_decode_keys() -> None:
    """小米语音控制/音频是 Feature 报告，不能落入键盘差分。"""
    src = (FW_MAIN / "br_hid.c").read_text(encoding="utf-8")
    notify = src.split("void br_hid_on_notify", 1)[1]
    guard_at = notify.find("r->report_type != 1")
    switch_at = notify.find("switch (role)")
    check("小米非 Input 报告在按键解析前直接返回",
          guard_at >= 0 and switch_at >= 0 and guard_at < switch_at,
          f"guard={guard_at} switch={switch_at}")
    check("Feature 报告守卫只对小米模式生效",
          "c->xiaomi_mode && r->report_type != 1" in notify)


def test_button_page_order() -> None:
    """按键页两列都按实物从上到下排。"""
    left = remote_layout.XIAOMI_LEFT_BUTTON_IDS
    right = remote_layout.XIAOMI_RIGHT_BUTTON_IDS
    check("左列从电源到菜单",
          left == ("power", "up", "left", "down", "home", "back", "menu"),
          str(left))
    check("右列从语音到音量减",
          right == ("mic", "ok", "right", "volume_up", "volume_down"),
          str(right))
    left_rows = [remote_layout.XIAOMI_VISUAL_ROWS[b] for b in left]
    right_rows = [remote_layout.XIAOMI_VISUAL_ROWS[b] for b in right]
    check("左列行号严格递增", left_rows == sorted(left_rows) and
          len(set(left_rows)) == len(left_rows), str(left_rows))
    check("右列行号严格递增", right_rows == sorted(right_rows) and
          len(set(right_rows)) == len(right_rows), str(right_rows))


def test_voice_row_shows_voice_page_key() -> None:
    """语音键行只显示语音页录制值，不显示三连击。"""
    qml = (CLIENT_SRC / "ui" / "qml" / "MappingCard.qml").read_text(
        encoding="utf-8")
    check("语音行不再有语音模式下暂停",
          'qsTr("语音模式下暂停")' not in qml
          and 'qsTr("双击 / 长按")' not in qml)
    check("语音行显示语音页录制值",
          "mappingVoiceKeyText_" in qml and "root.singleText" in qml)
    check("语音行提示去语音页修改",
          "修改语音键映射到语音页录制" in qml)
    ctl = (CLIENT_SRC / "core" / "controller.py").read_text(encoding="utf-8")
    check("VOICEKEY 回读后刷新语音键行",
          "_refresh_voice_button_row" in ctl)
    push = ctl.split("def _push_voice_hotkey_to_firmware", 1)[1].split(
        "\n    @", 1)[0]
    check("语音页改完后按键页立即同步",
          "_refresh_voice_button_row" in push)


def test_layout() -> None:
    """实物图 + 热点。"""
    img = ASSETS / "Xiaomi-remote.png"
    check("小米实物图存在", img.is_file(), str(img))
    hotspots = remote_layout.hotspots_for(remote_layout.LAYOUT_XIAOMI)
    check("热点 12 个（含语音键）", len(hotspots) == 12, str(len(hotspots)))
    ids = {h.button_id for h in hotspots}
    check("热点覆盖所有可配键 + 语音键",
          ids == set(MEASURED_USAGES) | {"mic"}, str(ids))
    check("坐标都在 0..1",
          all(0 <= h.x <= 1 and 0 <= h.y <= 1 and
              0 < h.width <= 1 and 0 < h.height <= 1 for h in hotspots))
    check("只有一个语音键",
          sum(1 for h in hotspots if h.is_voice) == 1)


def test_kind_detection() -> None:
    """按型号选布局。"""
    check("kind=xiaomi → 小米布局",
          remote_layout.layout_kind_for_remote({"kind": "xiaomi"})
          == remote_layout.LAYOUT_XIAOMI)
    check("kind=phone → 手机布局",
          remote_layout.layout_kind_for_remote({"kind": "phone"})
          == remote_layout.LAYOUT_PHONE)
    check("没写 kind 但名字是 MI RC → 小米布局",
          remote_layout.layout_kind_for_remote({"name": "MI RC"})
          == remote_layout.LAYOUT_XIAOMI)
    check("没写 kind 的老配置仍当谷歌遥控器",
          remote_layout.layout_kind_for_remote({})
          == remote_layout.LAYOUT_CHROMECAST)


def make_probe(**overrides):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from xiaomi_probe import Probe

    probe = Probe.__new__(Probe)
    values = dict(read_handles=[], read_every=0.4, write_voice="",
                  write_every=0.4, stop=threading.Event())
    values.update(overrides)
    probe.args = SimpleNamespace(**values)
    probe.log = io.StringIO()
    probe.lock = threading.Lock()
    probe.paired = False
    probe.connected = False
    probe.gatt_done = False
    probe.encrypted = False
    probe.hid_done = False
    probe.dumped = False
    probe.writing = False
    probe.serial_port = None
    probe.next_read = 0.0
    probe.next_write = 0.0
    probe.read_index = 0
    probe.write_handle = "0"
    probe.write_payload = "01"
    probe.say = lambda msg: None
    return probe


def ready_probe():
    probe = make_probe()
    probe.handle("EV CONN idx=0")
    probe.handle("EV GATTDONE idx=0 svcs=6 chrs=62 atvv=0")
    probe.handle("EV ENC idx=0 status=0 enc=1")
    probe.handle("EV HID idx=0 phase=done reports=28 ok=10")
    return probe


def test_probe_waits_for_complete_setup():
    probe = make_probe()
    probe.handle("EV CONN idx=0")
    check("仅连接不算取证就绪", not probe.ready())
    check("连接后不发发现命令", probe.commands_due(10.0) == [])
    probe.handle("EV GATTDONE idx=0 svcs=6 chrs=62 atvv=0")
    check("发现完成但未加密不算就绪", not probe.ready())
    probe.handle("EV ENC idx=0 status=0 enc=1")
    check("加密完成但 HID 未订阅不算就绪", not probe.ready())
    check(" setup 未完成时命令仍为空", probe.commands_due(10.0) == [])
    probe.handle("EV HID idx=0 phase=done reports=28 ok=10")
    check("发现、加密、订阅完成后就绪", probe.ready())
    check("默认只读报告表", probe.commands_due(10.0) == ["HIDREPORT 0"])
    check("报告表只取一次", probe.commands_due(10.1) == [])


def test_probe_rejects_failed_encryption_and_resets_on_disconnect():
    probe = make_probe()
    for line in ("EV CONN idx=0", "EV GATTDONE idx=0",
                 "EV ENC idx=0 status=5 enc=0",
                 "EV HID idx=0 phase=done reports=28 ok=10"):
        probe.handle(line)
    check("加密失败不会开始取证", not probe.ready())
    check("加密失败时没有命令", probe.commands_due(10.0) == [])
    probe.handle("EV ENC idx=0 status=0 enc=1")
    check("加密成功后恢复就绪", probe.ready())
    probe.handle("EV DISC idx=0 reason=0")
    check("断链清除就绪状态", not probe.ready())
    check("断链后不再发报告表", probe.commands_due(10.2) == [])


def test_probe_optional_io_waits_and_is_spaced():
    probe = make_probe(read_handles=[70, 74], write_voice="139:01")
    check("可选读写在就绪前不执行",
          probe.commands_due(10.0) == [])
    for line in ("EV CONN idx=0", "EV GATTDONE idx=0",
                 "EV ENC idx=0 status=0 enc=1",
                 "EV HID idx=0 phase=done reports=28 ok=10"):
        probe.handle(line)
    commands = probe.commands_due(10.0)
    check("就绪后先取报告表", commands[0] == "HIDREPORT 0")
    check("同一轮只读一个特征", commands.count("HIDREAD 0 70") == 1)
    check("没有硬编码批量读取", "HIDREAD 0 74" not in commands)
    check("显式写操作此时才执行", "HIDWRITE 0 139 01" in commands)
    check("0.4 秒内不重复读或写", probe.commands_due(10.2) == [])
    later = probe.commands_due(10.5)
    check("间隔后读取下一个特征", "HIDREAD 0 74" in later)
    check("读操作结束后不再硬编码读取",
          "HIDREAD 0 74" not in probe.commands_due(11.0))


def test_probe_does_not_treat_connfail_as_connected():
    probe = ready_probe()
    check("连接失败前处于就绪状态", probe.ready())
    probe.handle("EV CONNFAIL status=13")
    check("CONNFAIL 不会被误报为已连接", not probe.connected)
    check("CONNFAIL 清除就绪状态", not probe.ready())
    check("CONNFAIL 后不发送取证命令", probe.commands_due(20.0) == [])


def test_probe_rejects_google_atvv_peer():
    probe = make_probe(read_handles=[70], write_voice="139:01")
    for line in ("EV CONN idx=0",
                 "EV GATTDONE idx=0 svcs=8 chrs=36 atvv=1",
                 "EV ENC idx=0 status=0 enc=1",
                 "EV HID idx=0 phase=done reports=9 ok=8"):
        probe.handle(line)
    check("发现 ATVV 的 Google 遥控器不会进入小米取证", not probe.ready())
    check("错误设备不会收到读取或写入命令", probe.commands_due(10.0) == [])


def main() -> int:
    print("=== 小米遥控器一代 · 专项闸门 ===")
    test_names()
    test_usages()
    test_key_space_isolated()
    test_firmware_profile()
    test_firmware_recognizes_xiaomi()
    test_firmware_xiaomi_voice_connection_params()
    test_firmware_logs_data_length()
    test_firmware_requests_xiaomi_data_length()
    test_hidmap_parser()
    test_firmware_hid_write_properties()
    test_firmware_xiaomi_audio_enable()
    test_firmware_feature_reports_never_decode_keys()
    test_button_page_order()
    test_voice_row_shows_voice_page_key()
    test_layout()
    test_kind_detection()
    test_probe_waits_for_complete_setup()
    test_probe_rejects_failed_encryption_and_resets_on_disconnect()
    test_probe_optional_io_waits_and_is_spaced()
    test_probe_does_not_treat_connfail_as_connected()
    test_probe_rejects_google_atvv_peer()
    print()
    if FAILURES:
        print("✘ %d 项未通过：%s" % (len(FAILURES), "、".join(FAILURES)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
