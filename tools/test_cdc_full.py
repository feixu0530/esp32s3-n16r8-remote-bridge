#!/usr/bin/env python3
"""
CDC 协议链路 + 内部状态综合诊断（Windows）。

用法：
    python tools/test_cdc_full.py -p COM10

同时做三件事：
  1. 打开 CDC 口，通过 UART 抓固件日志
  2. 发协议命令，验证应答是否回到 CDC（而不是只走 UART）
  3. 发 CDCSTAT，拿固件侧的 TinyUSB 内部状态

输出里出现 "OK [...]" 说明应答真的从 CDC 回来了。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import serial


def open_with_retry(device: str, tries: int = 10, delay: float = 1.0):
    """端口可能被上一个测试进程占着，重试几次。"""
    last = None
    for _ in range(tries):
        try:
            return serial.Serial(device, 115200, timeout=0.2)
        except serial.SerialException as exc:
            last = exc
            time.sleep(delay)
    raise last


class UartTap:
    """后台抓 UART 日志。"""

    def __init__(self, device: str):
        self.device = device
        self.buf: list[str] = []
        self._stop = False
        self._t = None
        self.ser = None

    def start(self):
        try:
            self.ser = serial.Serial(self.device, 115200, timeout=0.2)
        except serial.SerialException as exc:
            print(f"[warn] UART {self.device} 打不开: {exc}")
            return
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop:
            try:
                if self.ser.in_waiting:
                    self.buf.append(self.ser.read(self.ser.in_waiting)
                                    .decode("utf-8", "replace"))
            except Exception:
                break
            time.sleep(0.02)

    def drain(self) -> str:
        text = "".join(self.buf)
        self.buf.clear()
        return text.replace("\r\n", "\n")

    def stop(self):
        self._stop = True
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


def read_response(port, timeout: float = 3.0) -> str:
    """在 timeout 内收集 CDC 收到的所有数据。"""
    end = time.time() + timeout
    got = b""
    while time.time() < end:
        if port.in_waiting:
            got += port.read(port.in_waiting)
            # 收到完整行就收工
            if b"\n" in got:
                break
        else:
            time.sleep(0.02)
    return got.decode("utf-8", "replace")


COMMANDS = [
    "INFO",
    "REMOTES",
    "CDCSTAT",
    "SAVEALL",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", default="COM10")
    ap.add_argument("-u", "--uart", default="COM6")
    args = ap.parse_args()

    tap = UartTap(args.uart)
    tap.start()
    time.sleep(0.3)

    try:
        cdc = open_with_retry(args.port)
    except serial.SerialException as exc:
        print(f"[FAIL] 打不开 {args.port}: {exc}")
        tap.stop()
        return 1

    cdc.dtr = True
    cdc.rts = True
    time.sleep(1.2)
    cdc.reset_input_buffer()
    tap.drain()

    print(f"=== CDC 协议链路（{args.port}）===")
    ok_count = 0
    for cmd in COMMANDS:
        cdc.write((cmd + "\n").encode())
        cdc.flush()
        resp = read_response(cdc, 3.0)
        clean = resp.strip()
        if clean:
            ok_count += 1
            print(f"  {cmd:<10} | CDC 有应答 | {clean[:110]}")
        else:
            print(f"  {cmd:<10} | CDC 无应答 | (超时)")
        time.sleep(0.2)

    print(f"\n=== 结果：{ok_count}/{len(COMMANDS)} 条命令的应答从 CDC 返回 ===")

    time.sleep(0.5)
    log = tap.drain()
    if log.strip():
        print("\n=== 固件 UART 日志 ===")
        for line in log.split("\n"):
            if line.strip():
                print("  " + line.strip())

    cdc.close()
    tap.stop()
    return 0 if ok_count == len(COMMANDS) else 1


if __name__ == "__main__":
    sys.exit(main())
