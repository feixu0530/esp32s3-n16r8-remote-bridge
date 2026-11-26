"""
SP-v2 协议客户端自测（用假串口，不碰真设备）。

重点验证：
    - DT 行必须先于 OK 到达（顺序保证）
    - k=v 解析与 % 转义
    - 事件/数据分发
    - 超时处理

运行：
    python dev/client/tests/test_protocol.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import os
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.protocol import (ProtocolClient, escape, parse_kv,  # noqa: E402
                           unescape)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


class FakePort:
    """假串口：写入会被记录，读取从队列里取。"""

    def __init__(self) -> None:
        self.written: list[str] = []
        self._rx: list[bytes] = []
        self._lock = threading.Lock()
        self._event = threading.Event()

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(data.decode("utf-8", errors="replace"))
        return len(data)

    def read(self, size: int = 4096) -> bytes:
        with self._lock:
            if not self._rx:
                self._event.wait(0.02)
                self._event.clear()
            if not self._rx:
                return b""
            chunk = self._rx.pop(0)
        return chunk

    def feed(self, text: str) -> None:
        with self._lock:
            self._rx.append((text + "\n").encode("utf-8"))
            self._event.set()


def test_escaping() -> None:
    print("\n[转义]")
    for raw in ("hello world", "a=b", "100%", "line1\nline2", "中文名字"):
        enc = escape(raw)
        check(f"往返 {raw!r}", unescape(enc) == raw, f"-> {enc!r}")
        check(f"转义后无空格 {raw!r}", " " not in enc or raw == "")


def test_kv() -> None:
    print("\n[键值解析]")
    kv = parse_kv(["idx=0", "name=Chromecast%20Remote", "flag", "x=1"])
    check("解析出数值", kv.get("idx") == "0")
    check("值已反转义", kv.get("name") == "Chromecast Remote")
    check("无等号的 token 被忽略", "flag" not in kv)


def test_ordering() -> None:
    print("\n[顺序保证：DT 先于 OK]")
    port = FakePort()
    cli = ProtocolClient(port)
    cli.start()

    data_seen: list[tuple[str, dict]] = []
    ok_seen: list[str] = []
    cli.on_data(lambda kind, kv: data_seen.append((kind, kv)))

    def responder():
        time.sleep(0.05)
        port.feed("DT RM idx=0 addr=AA:BB name=Chromecast%20Remote")
        port.feed("DT RM idx=1 addr=CC:DD name=MI%20RC")
        port.feed("OK REMOTES count=2")

    threading.Thread(target=responder, daemon=True).start()
    res = cli.request("REMOTES", timeout=3.0)

    check("请求成功", res.ok, res.reason)
    check("拿到 kv", res.get_int("count") == 2)
    check("DT 行在 OK 之前就收到了", len(data_seen) == 2, str(data_seen))
    check("DT 内容正确",
          data_seen and data_seen[0][1].get("name") == "Chromecast Remote")

    cli.stop()


def test_error() -> None:
    print("\n[错误应答]")
    port = FakePort()
    cli = ProtocolClient(port)
    cli.start()

    def responder():
        time.sleep(0.05)
        port.feed("ER CONNECT args")

    threading.Thread(target=responder, daemon=True).start()
    res = cli.request("CONNECT", "x", timeout=3.0)
    check("失败状态", not res.ok)
    check("带原因", res.reason == "args", res.reason)
    cli.stop()


def test_timeout() -> None:
    print("\n[超时]")
    port = FakePort()
    cli = ProtocolClient(port)
    cli.start()
    t0 = time.time()
    res = cli.request("INFO", timeout=0.4)
    check("超时返回失败", not res.ok)
    check("原因正确", res.reason == "timeout", res.reason)
    check("确实等了超时时间", time.time() - t0 >= 0.35)
    cli.stop()


def test_events() -> None:
    print("\n[事件分发]")
    port = FakePort()
    cli = ProtocolClient(port)
    cli.start()

    events: list[tuple[str, dict]] = []
    cli.on_event(lambda name, kv: events.append((name, kv)))

    port.feed("EV CONN idx=0 addr=7E:A2:AA:BB:CC:02 type=0")
    time.sleep(0.2)
    check("收到事件", len(events) == 1, str(events))
    if events:
        check("事件名正确", events[0][0] == "CONN")
        check("事件 kv 正确", events[0][1].get("addr") == "7E:A2:AA:BB:CC:02")
        check("type 正确", events[0][1].get("type") == "0")
    cli.stop()


def test_send_format() -> None:
    print("\n[发送格式]")
    port = FakePort()
    cli = ProtocolClient(port)
    cli.send("SET", 0, 1041, 1, 2, 0x5B, 0x48)
    check("命令拼装正确",
          port.written and port.written[0] == "SET 0 1041 1 2 91 72\n",
          str(port.written))


def main() -> int:
    print("=" * 60)
    print("SP-v2 协议自测")
    print("=" * 60)
    test_escaping()
    test_kv()
    test_ordering()
    test_error()
    test_timeout()
    test_events()
    test_send_format()

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
