"""
v0.23 闸门：USB HID 安全网 —— 板子绝不能把 PC 的键盘按死。

★ 为什么要有这条 ★
    用户实测：插上板子，PC 自己的键盘就失效；遥控器自动连上后按键也乱；
    按住返回键时输入框的文字被「一下全删」而不是缓慢连删。

    根因是 USB HID 的修饰键/按键状态是全局共享的：
      · 任何一次「按下」若丢了对应的「抬起」，那个键就永远按在 PC 上
        → 系统自动重复 → 键盘像坏了 / 文字一下全删；
      · 语音热键（左Ctrl+左Win）的抬起只等 ATVV 的 AUDIO_STOP 帧，
        遥控器不发就一直按着。

    所以固件必须有一道兜底：任何按下的键/修饰键，超时强制抬起。

运行：
    python dev/client/tests/test_v023.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-v023-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core.controller import BridgeController  # noqa: E402

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


def test_hid_watchdog_exists() -> None:
    """HID 层要有「按住超时强制抬起」的看门狗。"""
    usb = (FW_MAIN / "br_usb.c").read_text(encoding="utf-8")
    check("有按键释放看门狗函数",
          "bridge_usb_hid_watchdog" in usb, "")
    check("有按住超时常量",
          "BR_HID_HOLD_MAX_MS" in usb, "")
    check("看门狗会清空按住的键与修饰键",
          "s_kbd_stamp" in usb and "s_mod_stamp" in usb, "")
    hdr = (FW_MAIN / "br_usb.h").read_text(encoding="utf-8")
    check("头文件里有声明",
          "bridge_usb_hid_watchdog" in hdr, "")


def test_watchdog_is_called() -> None:
    """主循环必须周期调用它。"""
    core = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("主循环里调用了 HID 看门狗",
          "bridge_usb_hid_watchdog()" in core, "")
    check("主循环里调用了语音热键看门狗",
          "br_voice_watchdog()" in core, "")


def test_voice_hotkey_timeout() -> None:
    """语音热键的抬起要有兜底。"""
    voice = (FW_MAIN / "br_voice.c").read_text(encoding="utf-8")
    check("语音热键有超时释放函数",
          "br_voice_watchdog" in voice, "")
    check("语音热键有超时常量",
          "BR_VOICE_HOTKEY_MAX_MS" in voice, "")
    hdr = (FW_MAIN / "br_voice.h").read_text(encoding="utf-8")
    check("头文件里有声明",
          "br_voice_watchdog" in hdr, "")


def test_gesture_release_fallback() -> None:
    """手势状态丢了也要补一次抬起（这是「一下全删」的真凶）。"""
    gesture = (FW_MAIN / "br_gesture.c").read_text(encoding="utf-8")
    m = gesture.find("br_gesture_release")
    body = gesture[m:m + 1400]
    check("状态找不到时会补一次抬起",
          "fire_trigger" in body and "false" in body, "")
    check("补的是单击与长按两种抬起",
          "BR_GESTURE_SINGLE, false" in body and
          "BR_GESTURE_LONG, false" in body, "")


def test_default_profile_has_no_voice_key() -> None:
    """默认档案里不能有语音键那条（它跟语音热键抢同一个左Ctrl位）。"""
    mapping = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    body = mapping.split("k_binds_chromecast[]", 1)[-1].split("};", 1)[0]
    check("默认档案里没有语音键（8755）",
          "8755u" not in body, body[:160])
    rows = [ln for ln in body.splitlines() if ln.strip().startswith("{ ")]
    check("默认档案有 14 条", len(rows) == 14, str(len(rows)))
    check("保留返回键长按（连着删）",
          "8740u, 2" in body, "")


def test_empty_mapping_never_pushed(ctl: BridgeController) -> None:
    """★ 空映射绝不能下发 ★

    MAPBEGIN 会清空固件的暂存区，MAPCOMMIT 再把暂存区写进去 ——
    零条 MAPSET + 一次 MAPCOMMIT = **把开发板上的映射整个清空**。
    这个坑真的发生过：连上板子时内存里的映射还没选中遥控器（是空的），
    发出去就成了「MAPBEGIN + MAPCOMMIT」，板子上 15 条变成 0 条。
    """
    sent: list[tuple] = []

    class _Proto:
        def send(self, verb, *args):
            sent.append((verb, args))

        def request(self, *args, **kwargs):
            return None

    ctl._cfg["remotes"] = [
        {"addr": "AA:BB:CC:DD:EE:01", "name": "客厅", "bindings": {}},
    ]
    ctl._current = 0
    ctl._rebuild_remote_model()
    ctl._bindings = {}
    ctl._proto = _Proto()
    ctl._push_bindings_to_firmware()
    verbs = [v for v, _a in sent]
    check("空映射时一个命令都不发", not verbs, str(sent))
    check("尤其没有 MAPBEGIN/MAPCOMMIT",
          "MAPBEGIN" not in verbs and "MAPCOMMIT" not in verbs, str(sent))
    ctl._proto = None



def test_sync_is_simple() -> None:
    """同步逻辑简化了：PC 是主，自动下发，不比对、不弹窗。"""
    src = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("没有冲突弹窗了", "mappingConflict" not in src, "")
    check("没有映射比对视图了", "boardMappingView" not in src, "")
    check("改映射仍然立刻下发", "_push_bindings_to_firmware" in src, "")

    btn = (CLIENT / "src" / "ui" / "qml" / "ButtonsPage.qml").read_text(encoding="utf-8")
    check("按键页有导出配置入口", "exportMappingFileTo" in btn, "")
    check("按键页有导入配置入口", "importMappingFile" in btn, "")
    check("按键页没有映射比对了", "映射比对" not in btn, "")

    dev = (CLIENT / "src" / "ui" / "qml" / "DevicePage.qml").read_text(encoding="utf-8")
    check("设备页不再显示条数比对",
          "板子 " not in dev and "bindsBoard" not in dev.split("required property")[-1],
          "")

    main = (CLIENT / "src" / "ui" / "qml" / "main.qml").read_text(encoding="utf-8")
    check("根级没有冲突弹窗了", "mappingConflictDialog" not in main, "")


def main() -> int:
    print("=" * 66)
    print("v0.23 闸门：HID 安全网 + 简化同步")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    ctl = BridgeController()
    test_hid_watchdog_exists()
    test_watchdog_is_called()
    test_voice_hotkey_timeout()
    test_gesture_release_fallback()
    test_default_profile_has_no_voice_key()
    test_empty_mapping_never_pushed(ctl)
    test_sync_is_simple()

    print()
    print("=" * 66)
    if FAILURES:
        print(f"✘ {len(FAILURES)} 项未通过：{', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
