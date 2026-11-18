"""抓板子 UART 控制台日志（诊断按键的按下/抬起）。

用法：
    python dev/client/tests/capture_board.py [COM6] [输出文件] [秒数]
"""
from __future__ import annotations

import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM6"
out = sys.argv[2] if len(sys.argv) > 2 else \
    r"C:\Users\Figo\AppData\Local\Temp\board_capture.log"
seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 900

print(f"抓 {port} 的板子日志 → {out}（{seconds} 秒）", flush=True)
s = serial.Serial(port, 115200, timeout=0.3)
t0 = time.time()
count = 0
with open(out, "a", encoding="utf-8", buffering=1) as f:
    while time.time() - t0 < seconds:
        line = s.readline()
        if line:
            text = line.decode("utf-8", "replace").rstrip()
            f.write(text + "\n")
            count += 1
            if any(k in text for k in ("HIDTX", "kbd press", "kbd release",
                                       "MAPOUT", "EV KEY", "EV HIDRAW")):
                print(text, flush=True)
s.close()
print(f"共 {count} 行", flush=True)
