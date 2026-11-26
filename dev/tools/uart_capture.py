"""Capture one UART without toggling DTR/RTS (opening COM6 must not reset the board)."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import serial


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM6")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--out", default="temp/uart-capture.log")
    args = ap.parse_args()

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.seconds
    with out.open("w", encoding="utf-8", errors="replace", newline="\n") as f:
        while time.time() < deadline:
            chunk = ser.read(4096)
            if not chunk:
                continue
            text = chunk.decode("utf-8", "replace")
            print(text, end="", flush=True)
            f.write(text)
            f.flush()
    ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
