"""
全通道监视器 —— 同时看 UART 日志和 USB CDC 协议，合并到一个文件。

用法：
    python dev/tools/watch_all.py --uart COM6 --cdc COM12 --send "AUTO 1" --out /tmp/watch.log

好处：只有��个后台任务、���个监视器，不用同时开好几个。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import serial

STOP = threading.Event()
LOCK = threading.Lock()
SINK = None
T0 = time.time()


def emit(tag: str, line: str) -> None:
    text = f"{time.time() - T0:8.3f} [{tag}] {line}"
    with LOCK:
        print(text, flush=True)
        if SINK:
            SINK.write(text + "\n")
            SINK.flush()


def pump(port: str, baud: int, tag: str, repl: int = 30,
         send_cmds: list[str] | None = None) -> None:
    ser = None
    last = None
    for _ in range(repl):
        try:
            ser = serial.Serial(port, baud, timeout=0.2)
            break
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    if ser is None:
        emit(tag, f"打不开 {port}: {last}")
        return

    # ★ 命令必须用**同一个**串口句柄发 ——
    #   Windows 串口不能被两个句柄同时打开。
    if send_cmds:
        time.sleep(1.0)
        for cmd in send_cmds:
            try:
                ser.write((cmd + "\n").encode("ascii"))
                ser.flush()
                emit(tag, f">>> {cmd}")
            except Exception as exc:
                emit(tag, f"发命令失败: {exc}")
            time.sleep(0.4)

    buf = bytearray()
    while not STOP.is_set():
        try:
            chunk = ser.read(4096)
        except Exception as exc:
            emit(tag, f"读取中断: {exc}")
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                emit(tag, text)
    try:
        ser.close()
    except Exception:
        pass


def main() -> int:
    global SINK
    ap = argparse.ArgumentParser()
    ap.add_argument("--uart", default="COM6")
    ap.add_argument("--cdc", default="COM12")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--send", action="append", default=[])
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--seconds", type=float, default=900.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.out:
        SINK = open(args.out, "w", encoding="utf-8", errors="replace")

    threads = []
    if args.uart:
        threads.append(threading.Thread(target=pump,
                                       args=(args.uart, args.baud, "UART"),
                                       daemon=True))
    if args.cdc:
        # ★ 命令跟着 CDC 线程发，用同一个串口句柄 ★
        threads.append(threading.Thread(
            target=pump,
            args=(args.cdc, args.baud, "CDC"),
            kwargs={"send_cmds": args.send},
            daemon=True))
    for t in threads:
        t.start()

    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        if SINK:
            SINK.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
