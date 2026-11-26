"""
双通道监视器 —— 同时读 UART0 日志 (COM6) 和 USB CDC 协议 (COM12)。

崩溃现场往往只在其中一个通道上可见，两个一起看才完整。

用法：
    python dev/tools/dual_mon.py --uart COM6 --cdc COM12 --seconds 40
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import serial

STOP = threading.Event()


def pump(name, port, baud, out):
    try:
        s = serial.Serial(port, baud, timeout=0.2)
    except Exception as exc:
        out(f"[{name}] 打开失败: {exc}")
        return
    buf = bytearray()
    t0 = time.time()
    while not STOP.is_set():
        try:
            chunk = s.read(4096)
        except Exception as exc:
            out(f"[{name}] 读取中断: {exc}")
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                out(f"{time.time()-t0:8.3f} [{name}] {text}")
    try:
        s.close()
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uart", default="COM6")
    ap.add_argument("--cdc", default="COM12")
    ap.add_argument("--seconds", type=float, default=40.0)
    args = ap.parse_args()

    def out(line):
        print(line, flush=True)

    threads = []
    if args.uart:
        threads.append(threading.Thread(target=pump, args=("UART", args.uart, 115200, out), daemon=True))
    if args.cdc:
        threads.append(threading.Thread(target=pump, args=("CDC", args.cdc, 115200, out), daemon=True))
    for t in threads:
        t.start()
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    STOP.set()
    time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
