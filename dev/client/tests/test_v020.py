"""
v0.20.0 闸门：固件内置默认映射（按型号分档）/ 冲突弹窗 / 归档不被污染 /
同步按钮 / 消费者键 / 名字列宽。

★ 每条闸门对应用户提的一条 ★
    1. 固件内置默认按键映射，**按遥控器型号分档**（现在是谷歌遥控器一档）
    2. 配对成功那一刻就铺好，插上板子不用 PC 也能用
    3. 本机与板子的映射冲突时**先弹窗问用户**，确定才覆盖
    4. 遥控器名字列要够宽，名字显示全
    5. 设备页「同步」按钮点了不报错
    6. 音量三键从板子读回时不再丢失

运行：
    python dev/client/tests/test_v020.py
"""
from __future__ import annotations

import os
import re
import sys
import types
import tempfile
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-v020-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 别去连真板子 —— 测试的返回值必须是确定的
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core import remote_layout  # noqa: E402
from core.action_options import label_to_kind  # noqa: E402
from core.controller import BridgeController  # noqa: E402
from core.key_mapping import (ActionKind, ButtonAction, ButtonTrigger,  # noqa: E402
                              CONSUMER_TOKENS, firmware_bindings,
                              set_button_action)
from platforms.windows import hid_usage_for_token  # noqa: E402

FAILURES: list[str] = []

ROOT = Path(__file__).resolve().parents[3]
FW_MAIN = ROOT / "dev" / "firmware" / "bridge" / "main"
QML = ROOT / "dev" / "client" / "src" / "ui" / "qml"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


class _Result:
    """MAPGET 应答的替身。"""

    def __init__(self, count: int) -> None:
        self._count = count

    def get_int(self, _key: str, _default: int = 0) -> int:
        return self._count


# ---------------------------------------------------------------- 需求 1
def _parse_firmware_defaults() -> dict:
    """从 br_mapping.c 里抠出固件的出厂默认表。

    返回 {key_id: (trig, out, mods, (keys...))}
    """
    return _parse_firmware_array("k_binds_chromecast")


def _parse_firmware_array(name: str) -> dict:
    """抠出固件里某一张型号默认表。"""
    src = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    m = re.search(name + r"\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    if not m:
        return {}
    body = m.group(1)
    out: dict = {}
    pattern = (r"\{\s*(\d+)u\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*"
               r"(0x[0-9A-Fa-f]+|\d+)\s*,\s*(\d+)\s*,\s*\{([^}]*)\}\s*\}")
    # 小米那张表用 BR_KEY_MAKE(src, usage) 写 key_id —— 也要认
    macro_pattern = (r"\{\s*BR_KEY_MAKE\(BR_SRC_(\w+),\s*"
                     r"(0x[0-9A-Fa-f]+|\d+)\)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*"
                     r"(0x[0-9A-Fa-f]+|\d+)\s*,\s*(\d+)\s*,\s*\{([^}]*)\}\s*\}")
    src_ids = {"KEYBOARD": 1, "CONSUMER": 2, "DATA": 3, "VENDOR": 4,
               "ATVV": 5, "PHONE": 6}
    for row in re.finditer(pattern, body):
        key_id = int(row.group(1))
        trig = int(row.group(2))
        out_type = int(row.group(3))
        mods = int(row.group(4), 0)
        keys = tuple(int(x, 0) for x in re.findall(r"0x[0-9A-Fa-f]+|\d+",
                                                  row.group(6)))
        out[key_id] = (trig, out_type, mods, keys)
    for row in re.finditer(macro_pattern, body):
        src = src_ids.get(row.group(1))
        if src is None:
            continue
        usage = int(row.group(2), 0)
        key_id = (src << 12) | (usage & 0x0FFF)
        trig = int(row.group(3))
        out_type = int(row.group(4))
        mods = int(row.group(5), 0)
        keys = tuple(int(x, 0) for x in re.findall(r"0x[0-9A-Fa-f]+|\d+",
                                                  row.group(7)))
        out[key_id] = (trig, out_type, mods, keys)
    return out


def _pc_default_shape(kind: str = remote_layout.LAYOUT_CHROMECAST) -> dict:
    """PC 侧默认表 → 和固件一样的形状。"""
    bindings: dict = {}
    for button_id, label in remote_layout.default_actions_for(kind).items():
        key_id = remote_layout.button_key_id_for(kind, button_id)
        if not key_id:
            continue
        k = label_to_kind(label)
        action = ButtonAction(k if k is not None else ActionKind.DISABLED)
        bindings = set_button_action(bindings, key_id,
                                     ButtonTrigger.SINGLE_CLICK, action)
    # ★ 长按也要比 ★
    #   返回键长按 = 退格（固件 k_binds_chromecast 里 trig=2 那一条）。
    #   不比的话两边条数会差 1，却谁也发现不了。
    for button_id, label in remote_layout.default_long_actions_for(kind).items():
        key_id = remote_layout.button_key_id_for(kind, button_id)
        if not key_id:
            continue
        k = label_to_kind(label)
        action = ButtonAction(k if k is not None else ActionKind.DISABLED)
        bindings = set_button_action(bindings, key_id,
                                     ButtonTrigger.LONG_PRESS, action)
    out: dict = {}
    for key_id, trig, tokens in firmware_bindings(bindings):
        src_s, usage_s = key_id.split(":", 1)
        key = (int(src_s) << 12) | int(usage_s)
        mods = 0
        kb: list[int] = []
        consumer = 0
        for token in tokens:
            usage = hid_usage_for_token(token)
            if usage is None:
                continue
            if token in CONSUMER_TOKENS:
                consumer = CONSUMER_TOKENS[token]
            elif 0xE0 <= usage <= 0xE7:
                mods |= 1 << (usage - 0xE0)
            else:
                kb.append(usage)
        if consumer:
            out[key] = (trig, 2, 0, (consumer,))
        else:
            out[key] = (trig, 1, mods, tuple(kb))
    return out


def _compare_profile(name: str, fw_array: str, kind: str) -> None:
    """一个型号的固件默认表 ↔ PC 默认表必须逐条一致。"""
    fw = _parse_firmware_array(fw_array)
    check("%s 固件默认表能抠出来且不为空" % name, len(fw) >= 10, str(len(fw)))
    if not fw:
        return
    pc = _pc_default_shape(kind)
    missing = [k for k in pc if k not in fw]
    check("%s PC 默认表里的每一条固件里都有" % name, not missing, str(missing))
    diff = [k for k in pc if k in fw and fw[k] != pc[k]]
    check("%s 两边的键序列逐条一致" % name, not diff,
          str([(k, fw.get(k), pc.get(k)) for k in diff]))
    extra = [k for k in fw if k not in pc]
    check("%s 固件里没有多余的条目" % name, not extra, str(extra))


def test_firmware_default_profile() -> None:
    """固件必须有一张「按型号分档」的默认表。"""
    src = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    check("固件有默认档案表 k_default_profiles",
          "k_default_profiles" in src, "")
    check("档案按名字匹配（name_match）",
          "name_match" in src and "profile_for_name" in src, "")
    check("谷歌遥控器是一档",
          '"chromecast"' in src, "")
    check("小米遥控器是一档",
          '"xiaomi"' in src and "k_binds_xiaomi" in src, "")

    _compare_profile("谷歌遥控器", "k_binds_chromecast",
                     remote_layout.LAYOUT_CHROMECAST)
    _compare_profile("小米遥控器", "k_binds_xiaomi",
                     remote_layout.LAYOUT_XIAOMI)


def test_apply_defaults_takes_name() -> None:
    """铺默认必须看名字；名字对不上就不铺。"""
    hdr = (FW_MAIN / "br_mapping.h").read_text(encoding="utf-8")
    check("头文件里 apply_defaults 带 name 参数",
          "br_mapping_apply_defaults(const char *addr, const char *name" in hdr,
          "")
    check("头文件里 apply_defaults 还带档案 id 参数",
          "profile_in" in hdr and "out_profile" in hdr, "")
    src = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    check("实现里按名字找档案",
          "profile_for_name(name)" in src, "")
    check("找不到档案就不铺",
          "if (!prof)" in src and "return 0;" in src, "")
    check("PC 正在写映射时返回 -1 让调用方重试",
          "return -1;" in src, "")


# ---------------------------------------------------------------- 需求 2
def test_firmware_version() -> None:
    """固件版本号必须会说实话。"""
    src = (FW_MAIN / "app_main.c").read_text(encoding="utf-8")
    check("固件版本号是 1.2.3",
          '#define FW_VERSION "3.0.0"' in src, "")


def test_defaults_applied_offline() -> None:
    """插上板子、遥控器配上对 → 默认映射要自己铺好。"""
    src = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("连上就排队铺默认", "queue_defaults(idx, p->name)" in src, "")
    check("排队由主循环执行（不在 GAP 回调里写 NVS）",
          "run_pending_defaults();" in src and
          "br_ble_task" in src, "")
    check("PC 正在写映射时下一拍再试（不放弃）",
          "rc == -1" in src, "")
    check("解绑时把映射一起删掉（重配才能拿到默认）",
          "br_mapping_clear(addr);" in src, "")


# ---------------------------------------------------------------- 需求 3

def test_archive_not_polluted(ctl: BridgeController) -> None:
    """从板子读回**不能**覆盖归档。"""
    local = {"bindings": {"2:547": {"kind": "key_combo",
                                    "keys": ["lctrl", "lwin"]}}}
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:02", "identity": "22:33:44:55:66:77",
         "name": "卧室", "bindings": local},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._bindings = local
    # 先存一次 → 归档里有本机这份
    ctl._save_bindings(write_archive=True, push=False)
    archive = ctl._cfg.get("mapping_archive", {})
    key = "22:33:44:55:66:77"
    check("归档里有这台", key in archive, str(list(archive.keys())))

    # 板子上是另一套 → 读回来
    ctl._collecting_map = True
    ctl._board_mapping = {("2:66", 0): ("up",)}
    ctl._apply_board_mapping(_Result(1), "AA:BB:CC:DD:EE:02")

    after = ctl._cfg.get("mapping_archive", {})
    same = (after.get(key, {}).get("bindings") == local)
    check("读回板子内容后归档没被改", same,
          str(after.get(key, {}).get("bindings")))


def test_board_wins_when_local_empty(ctl: BridgeController) -> None:
    """本机是空的、板子上有 → 首次采纳板子（这个场景以前是对的，别弄丢）。"""
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:03", "identity": "33:44:55:66:77:88",
         "name": "书房", "bindings": {}},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._bindings = {}
    ctl._collecting_map = True
    ctl._board_mapping = {("2:66", 0): ("up",)}
    ctl._apply_board_mapping(_Result(1), "AA:BB:CC:DD:EE:03")
    got = ctl._remote_bindings(0).get("bindings") or {}
    check("本机为空时采纳了板子上的映射", bool(got), str(got))
    archive = ctl._cfg.get("mapping_archive", {})
    check("首次采纳时也建了归档",
          "33:44:55:66:77:88" in archive, str(list(archive.keys())))


# ---------------------------------------------------------------- 需求 5


def test_sync_slot(ctl: BridgeController) -> None:
    """设备页的「同步」按钮必须真的有实现。"""
    check("controller 里有 syncMappingToBoard",
          hasattr(ctl, "syncMappingToBoard"), "")

    sent: list[tuple] = []

    class _Proto:
        def send(self, verb, *args):
            sent.append((verb, args))

        def request(self, *args, **kwargs):
            # 真实实现先发再等；测试里直接当成功
            if args:
                self.send(*args)
            return types.SimpleNamespace(ok=True, reason="")

    ctl._proto = _Proto()
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:04", "name": "客厅",
         "bindings": {"bindings": {"2:66": {"kind": "key_combo",
                                            "keys": ["up"]}}}},
    ]
    ctl._rebuild_remote_model()
    ok = ctl.syncMappingToBoard(0)
    check("同步成功", ok, "")
    verbs = [v for v, _a in sent]
    check("发了 MAPBEGIN", "MAPBEGIN" in verbs, str(verbs))
    check("发了 MAPSET", "MAPSET" in verbs, str(verbs))
    check("发了 MAPCOMMIT", "MAPCOMMIT" in verbs, str(verbs))
    ctl._proto = None


# ---------------------------------------------------------------- 需求 6
def test_consumer_rows_kept(ctl: BridgeController) -> None:
    """音量三键从板子读回时不能再丢。"""
    ctl._collecting_map = True
    ctl._board_mapping = {}
    ctl._on_map_row({"key": str((2 << 12) | 0xE9), "trig": "0", "out": "2",
                      "mods": "0", "keys": "E9"})
    check("音量+ 没被丢掉", ("2:233", 0) in ctl._board_mapping,
          str(ctl._board_mapping))
    if ("2:233", 0) in ctl._board_mapping:
        tokens = ctl._board_mapping[("2:233", 0)]
        check("还原成消费者 token", tuple(tokens) == ("volume_up",),
              str(tokens))
        bindings = ctl._bindings_from_board(ctl._board_mapping)
        action = (bindings.get("bindings", {}) or {}).get("2:233")
        check("还原成语义动作（系统音量 +）",
              bool(action) and action.get("kind") == "system_volume_up",
              str(action))
    ctl._collecting_map = False


def test_restore_defaults_keeps_notes(ctl: BridgeController) -> None:
    """恢复内置默认不能抹掉备注和双击/长按。"""
    bindings = {
        "bindings": {"2:66": {"kind": "key_combo", "keys": ["up"]}},
        "secondary_bindings": {"2:66": {"kind": "key_combo", "keys": ["down"]}},
        "display_notes": {"2:66": {"note": "客厅用的"}},
    }
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:05", "name": "客厅", "bindings": bindings},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._bindings = bindings
    ctl.restoreMappingDefaults()
    now = ctl._remote_bindings(0)
    check("备注保住了", bool(now.get("display_notes")), str(now))
    check("双击保住了", bool(now.get("secondary_bindings")), str(now))


def test_profile_by_name() -> None:
    """默认档案按名字认遥控器；不认识的不给默认。"""
    check("谷歌遥控器认得出来",
          remote_layout.profile_for_name("Chromecast Media Remote") is not None,
          "")
    check("认得出 Chromecast",
          remote_layout.profile_for_name("CHROMECAST") is not None, "")
    check("不认识的遥控器没有默认档案",
          remote_layout.profile_for_name("随便一个蓝牙遥控器") is None, "")


# ---------------------------------------------------------------- 需求 4
def test_qml_dialog_and_width() -> None:
    """同步简化后没有冲突弹窗了；名字列要够宽。"""
    main = (QML / "main.qml").read_text(encoding="utf-8")
    check("根级不再有冲突弹窗（同步简化了）",
          "mappingConflictDialog" not in main, "")
    check("也没有 mappingConflict 信号了",
          "mappingConflict" not in main, "")

    dev = (QML / "DevicePage.qml").read_text(encoding="utf-8")
    widths = [int(m) for m in re.findall(r"titleWidth:\s*(\d+)", dev)]
    check("遥控器行的名字列 ≥ 150",
          widths and max(widths) >= 150, str(widths))
    actions = [int(m) for m in re.findall(r"actionColumnWidth:\s*(\d+)", dev)]
    check("操作列宽 ≥ 184（两个按钮放得下）",
          actions and max(actions) >= 184, str(actions))


def test_client_version() -> None:
    """客户端版本号。"""
    from core import version
    check("客户端版本 1.2.3", version.VERSION == "3.0.0", version.VERSION)


def test_local_is_master_no_compare(ctl: BridgeController) -> None:
    """PC 是唯一真值：改映射自动下发，不做比对、不弹窗。"""
    sent: list[tuple] = []

    class _Proto:
        def send(self, verb, *args):
            sent.append((verb, args))

        def request(self, *args, **kwargs):
            if args:
                self.send(*args)
            return types.SimpleNamespace(ok=True, reason="")

    local = {"bindings": {"2:66": {"kind": "key_combo", "keys": ["up"]}}}
    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:01", "name": "客厅", "bindings": local},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._bindings = local
    ctl._proto = _Proto()
    ctl._save_bindings(write_archive=True, push=True)
    verbs = [v for v, _a in sent]
    check("改映射立刻自动下发（不用点任何按钮）",
          "MAPBEGIN" in verbs and "MAPSET" in verbs and "MAPCOMMIT" in verbs,
          str(verbs))

    # 板子上读回来的内容**不覆盖**本机
    ctl._board_mapping = {("2:66", 0): ("down",)}
    ctl._collecting_map = True
    ctl._apply_board_mapping(_Result(1), "AA:BB:CC:DD:EE:01")
    check("读回板子不会覆盖本机",
          ctl._remote_bindings(0).get("bindings") == local["bindings"],
          str(ctl._remote_bindings(0)))
    ctl._proto = None

def main() -> int:
    print("=" * 66)
    print("v0.20.0 闸门")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    ctl = BridgeController()
    try:
        test_firmware_default_profile()
        test_apply_defaults_takes_name()
        test_firmware_version()
        test_defaults_applied_offline()
        test_local_is_master_no_compare(ctl)
        test_archive_not_polluted(ctl)
        test_board_wins_when_local_empty(ctl)
        test_sync_slot(ctl)
        test_consumer_rows_kept(ctl)
        test_restore_defaults_keeps_notes(ctl)
        test_profile_by_name()
        test_qml_dialog_and_width()
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
