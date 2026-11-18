"""
连接生命周期闸门 + 语音键默认值闸门。

★ 为什么要有这两条 ★
    v0.15.0 的两个大坑：
      1. 换串口时程序**当场消失** —— connectDevice() 换新协议客户端时
         不停旧的读线程，两条线程抢同一个串口，新连接把旧线程正在读的
         句柄关掉 → access violation。
      2. 每次连上都把 PC 配置里的语音快捷键**无条件推给板子**，
         冲掉用户板子上生效的值（出厂默认就是左Ctrl+左Win）。

运行：
    python dev/client/tests/test_connect_lifecycle.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-life-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

from core.controller import BridgeController  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


class FakeProto:
    """记录发出去的命令。"""

    def __init__(self):
        self.sent = []

    def send(self, *args):
        self.sent.append(args)
        return True

    def start(self):
        pass

    def stop(self):
        pass

    def request(self, *args, **kwargs):
        from core.protocol import ProtocolResult
        return ProtocolResult(ok=True, verb=str(args[0]) if args else "")


def test_reconnect_cycle() -> None:
    print("\n[反复连接闸门]")
    ctl = BridgeController()
    boards = [p for p in ctl.portList if p.get("protocol")]
    if not boards:
        print("  SKIP  没插开发板，这条闸门本次无法验证")
        return
    port = boards[0]["device"]

    ok = 0
    for _ in range(5):
        if ctl.connectDevice(port):
            ok += 1
        ctl.disconnectDevice()
        time.sleep(0.2)
    check("连/断 5 轮全部成功", ok == 5, f"成功 {ok}/5")
    check("断开后状态干净", not ctl.connected and ctl._proto is None)

    # 连着的时候换一个口（v0.15.0 在这一步当场崩溃）
    ctl.connectDevice(port)
    others = [p["device"] for p in ctl.portList if not p.get("protocol")]
    if others:
        ctl.connectDevice(others[0])
        check("连着时换口没崩", True)
    else:
        check("连着时换口没崩（只有一个口，跳过）", True)
    ctl.disconnectDevice()
    ctl.shutdown()


def test_voice_key_default() -> None:
    print("\n[语音键默认值闸门]")
    ctl = BridgeController()

    # 没改过 → 不许下发 VOICEKEY
    ctl._cfg["ime_hotkey_custom"] = False
    proto = FakeProto()
    ctl._proto = proto
    ctl._sync_voice_hotkey()
    sent = [c[0] for c in proto.sent]
    check("未在软件改过时不下发 VOICEKEY", "VOICEKEY" not in sent, str(sent))

    # 改过 → 才可以下发
    ctl._cfg["ime_hotkey_custom"] = True
    proto.sent.clear()
    ctl._sync_voice_hotkey()
    sent = [c[0] for c in proto.sent]
    check("在软件改过之后才下发 VOICEKEY", "VOICEKEY" in sent, str(sent))
    if "VOICEKEY" in sent:
        args = [c for c in proto.sent if c[0] == "VOICEKEY"][0]
        check("下发的是左Ctrl+左Win（mods=0x09）",
              len(args) > 1 and args[1] == 0x09, str(args))
    ctl._proto = None


def main() -> int:
    print("=" * 66)
    print("连接生命周期 + 语音键默认值闸门")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    timer = QTimer()
    timer.start(200)
    timer.timeout.connect(lambda: None)

    test_reconnect_cycle()
    test_voice_key_default()

    print()
    print("=" * 66)
    if FAILURES:
        print(f"✘ {len(FAILURES)} 项未通过：{', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
