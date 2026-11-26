"""
串口监视器 —— ESP32-S3 USB CDC 协议通道的观测/控制工具。

用法：
    python dev/tools/serial_mon.py --port COM12                 # 只监听
    python dev/tools/serial_mon.py --port COM12 --send "SCAN 2" # 发命令后继续监听
    python dev/tools/serial_mon.py --port COM12 --send "SCAN 2" --send "AUTO 1" --seconds 30

协议是 SP-v2 行文本，工具会把 OK/ER/EV/DT/LG 分色显示。
"""
from __future__ import annotations

import argparse
import sys
import time

import serial

COLORS = {
    "OK": "\033[32m",    # 绿
    "ER": "\033[31m",    # 红
    "EV": "\033[36m",    # 青
    "DT": "\033[33m",    # 黄
    "LG": "\033[90m",    # 灰
    "#":  "\033[90m",
}
RESET = "\033[0m"


def colorize(line: str) -> str:
    token = line.split(" ", 1)[0] if line else ""
    c = COLORS.get(token)
    return f"{c}{line}{RESET}" if c else line


def open_port(port: str, baud: int = 115200, retries: int = 20) -> serial.Serial:
    last = None
    for _ in range(retries):
        try:
            s = serial.Serial(port, baud, timeout=0.2)
            return s
        except Exception as exc:      # 设备刚复位时可能短暂占用
            last = exc
            time.sleep(0.5)
    raise SystemExit(f"打不开串口 {port}: {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM12")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--send", action="append", default=[],
                    help="连接后发送的命令，可重复")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="发送命令前的等待秒数")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="监听多少秒后退出（0=一直监听）")
    ap.add_argument("--raw", action="store_true", help="不做分色")
    ap.add_argument("--out", default="", help="同时写入的文件")
    args = ap.parse_args()

    s = open_port(args.port, args.baud)
    print(f"[串口] {args.port} @ {args.baud}", file=sys.stderr)

    sink = open(args.out, "w", encoding="utf-8", errors="replace") if args.out else None

    for cmd in args.send:
        time.sleep(args.delay)
        s.write((cmd + "\n").encode("ascii"))
        s.flush()
        print(f"[发送] {cmd}", file=sys.stderr)

    deadline = time.time() + args.seconds if args.seconds > 0 else None
    buf = bytearray()
    try:
        while True:
            if deadline and time.time() > deadline:
                break
            chunk = s.read(4096)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                text = line.decode("utf-8", errors="replace").rstrip("\r")
                if not text:
                    continue
                if sink:
                    sink.write(text + "\n")
                    sink.flush()
                print(text if args.raw else colorize(text), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if sink:
            sink.close()
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
