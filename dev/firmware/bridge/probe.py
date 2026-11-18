"""
串口协议探针 —— 开发期用来直接跟开发板对话。

用法：
    python probe.py <命令> [参数...]
    python probe.py INFO
    python probe.py VOICEKEY
    python probe.py LOGREAD 20
    python probe.py LOGLIVE 1
    python probe.py TESTGESTURE
    python probe.py MAPGET AA:BB:CC:DD:EE:FF

默认连 ESP32-S3 原生 USB 的 CDC 口（VID 0x303A）——
协议应答只走 CDC，CH343（UART）上只有 ESP_LOG 和发出去的命令。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

import serial
import serial.tools.list_ports as lp

CDC_VID = 0x303A
BAUD = 115200


def find_cdc():
    for p in lp.comports():
        if p.vid == CDC_VID:
            return p.device
    return None


def find_uart():
    for p in lp.comports():
        if p.vid == 0x1A86:
            return p.device
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    port_name = os.environ.get("PROBE_PORT") or find_cdc()
    if not port_name:
        print("找不到 CDC 口（VID 0x303A）—— 开发板插上了吗？")
        uart = find_uart()
        if uart:
            print(f"只有 CH343 UART: {uart}（协议应答不在这里）")
        return 2

    try:
        ser = serial.Serial(port_name, BAUD, timeout=0.3)
    except Exception as exc:
        print(f"打开 {port_name} 失败：{exc}")
        return 3

    # 先把积压的行读掉
    end = time.time() + 0.5
    while time.time() < end:
        ser.read(4096)

    line = " ".join(sys.argv[1:])
    ser.write((line + "\n").encode("utf-8"))
    ser.flush()

    print(f"> {line}")
    buf = bytearray()
    deadline = time.time() + 8.0
    while time.time() < deadline:
        chunk = ser.read(16384)
        if chunk:
            buf += chunk
            text = buf.decode("utf-8", errors="replace")
            # 已经收到 OK/ER 就停
            if "\nOK " in text or "\nER " in text:
                break
    print(bytes(buf).decode("utf-8", errors="replace").strip())
    ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
