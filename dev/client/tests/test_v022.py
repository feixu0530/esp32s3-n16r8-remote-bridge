"""
v0.22.1 闸门：映射快照不丢多手势 + 语音通道状态同步 + 语音日志说实话。

★ 每条闸门对应用户实测报的一条 ★
    1. 重新配对的老遥控器 → 提示「不一致」但数量对不上
    2. 重新配对的映射有一部分显示「没有映射」
    3. 语音键失效：日志显示触发了，但输入法没收到

运行：
    python dev/client/tests/test_v022.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-v022-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core import human_log  # noqa: E402
from core.controller import BridgeController  # noqa: E402
from core.key_mapping import ButtonTrigger, firmware_bindings  # noqa: E402

FAILURES: list[str] = []

ROOT = Path(__file__).resolve().parents[3]
FW_MAIN = ROOT / "dev" / "firmware" / "bridge" / "main"
CLIENT = ROOT / "dev" / "client"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def _row(key: int, trig: int, out: int = 1, mods: int = 0,
         keys: str = "") -> dict:
    """固件 DT MAP 的一行。"""
    return {"key": str(key), "trig": str(trig), "out": str(out),
            "mods": str(mods), "keys": keys}


# ---------------------------------------------------------------- 需求 1/2
def test_snapshot_keeps_multi_trigger(ctl: BridgeController) -> None:
    """同一个键的单击和长按，快照里都要在（不能互相覆盖）。"""
    ctl._collecting_map = True
    ctl._board_mapping = {}
    ctl._on_map_row(_row((2 << 12) | 548, 0, keys="2A"))   # 返回 单击
    ctl._on_map_row(_row((2 << 12) | 548, 2, keys="2A"))   # 返回 长按
    keys = set(ctl._board_mapping.keys())
    check("单击和长按都在快照里（没被覆盖）", len(keys) == 2, str(keys))
    check("两个手势分别是单击和长按",
          keys == {("2:548", 0), ("2:548", 2)}, str(keys))


def test_merge_keeps_local_single_click(ctl: BridgeController) -> None:
    """板子上有单击和长按时，本机的单击不能被当成「板子上没有」删掉。"""
    local = {"bindings": {"2:548": {"kind": "key_combo", "keys": ["backspace"]}},
             "secondary_bindings": {"2:548": {"long_press": {
                 "kind": "key_combo", "keys": ["backspace"]}}}}
    board = {("2:548", 0): ("backspace",), ("2:548", 2): ("backspace",)}
    merged = ctl._merge_board_into_local(local, board)
    single = (merged.get("bindings") or {}).get("2:548")
    longp = (merged.get("secondary_bindings") or {}).get("2:548")
    check("本机的单击还在", bool(single), str(merged))
    check("本机的长按还在", bool(longp), str(merged))
    check("两边一致",
          ctl._same_as_board(merged, board), str(merged))


def test_repaired_remote_has_back_single(ctl: BridgeController) -> None:
    """重配后「返回」键的单击必须还在（以前被合并逻辑误删了）。"""
    ctl._collecting_map = True
    ctl._board_mapping = {}
    ctl._on_map_row(_row((2 << 12) | 548, 0, keys="2A"))
    ctl._on_map_row(_row((2 << 12) | 548, 2, keys="2A"))
    ctl._on_map_row(_row((2 << 12) | 563, 0, mods=1, keys=""))
    board = dict(ctl._board_mapping)
    bindings = ctl._bindings_from_board(board)
    check("采纳板子后「返回」单击在",
          "2:548" in (bindings.get("bindings") or {}), str(bindings))
    check("采纳板子后「返回」长按也在",
          "2:548" in (bindings.get("secondary_bindings") or {}), str(bindings))
    view = {(k, t) for k, t, _ in firmware_bindings(bindings)}
    check("固件视角下两个手势都在",
          view == {("2:548", 0), ("2:548", 2), ("2:563", 0)}, str(view))


# ---------------------------------------------------------------- 需求 3
def test_voice_state_pushed_on_connect() -> None:
    """PC 连上板子必须把语音通道的开关状态推过去。"""
    src = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("连上时会发 VOICE 1/0",
          'send("VOICE", 1 if self._cfg.get("voice_enabled"' in src, "")

    voice = (FW_MAIN / "br_voice.c").read_text(encoding="utf-8")
    check("语音通道开关持久化到 NVS",
          'nvs_get_u8(nvs, "enabled"' in voice and
          'nvs_set_u8(nvs, "enabled"' in voice, "")
    check("热键按下成功与否会返回给调用方",
          "bool voice_hotkey_press(void)" in voice, "")

    core = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("语音事件带 sent（有没有真的按出去）",
          '"sent", voice_pressed ? 1 : 0' in core, "")
    check("语音事件带 on（通道开没开）",
          '"on", br_voice_enabled() ? 1 : 0' in core, "")


def test_voice_log_tells_truth() -> None:
    """热键没按出去时，日志不能还说「板子替电脑按出 …」。"""
    kv = {"sent": "0", "on": "0"}
    text = human_log.voice_event(kv, "客厅遥控器", "左Ctrl+左Win")
    check("没按热键时日志说「没按热键」", "没按热键" in text, text)
    check("并说明原因（语音通道被关了）", "语音通道被关了" in text, text)
    ok = human_log.voice_event({"sent": "1", "on": "1"}, "客厅遥控器",
                                "左Ctrl+左Win")
    check("正常时还是原来的说法", "板子替电脑按出" in ok, ok)


def main() -> int:
    print("=" * 66)
    print("v0.22.1 闸门")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    ctl = BridgeController()
    try:
        test_snapshot_keeps_multi_trigger(ctl)
        test_merge_keeps_local_single_click(ctl)
        test_repaired_remote_has_back_single(ctl)
        test_voice_state_pushed_on_connect()
        test_voice_log_tells_truth()
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
