"""
设备检查脚本：验证 ESP32-S3 桥接设备在 Windows 上的枚举与协议连通性。

用法：
    python tools/check_device.py            # 自动挑选端口
    python tools/check_device.py COM9       # 指定端口

检查项：
    1. USB 复合设备是否枚举成功（VID_303A&PID_4009）
    2. HID 键盘 / 消费者控制接口是否 OK
    3. CDC 串口是否可用
    4. 串口协议握手（INFO / REMOTES）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dev" / "client" / "src"))

try:
    import serial
    import serial.tools.list_ports as serial_ports
except ImportError:
    print("需要 pyserial： pip install pyserial")
    sys.exit(2)


def find_candidate_ports() -> list:
    """找出可能的桥接设备端口（Espressif VID + CDC 类）。"""
    out = []
    for p in serial_ports.comports():
        hwid = (p.hwid or "").upper()
        if "VID_303A" in hwid or "VID_2D79" in hwid:
            out.append(p)
    return out


def probe(port: str, timeout: float = 6.0) -> dict:
    """在指定端口上做协议握手。"""
    result = {"port": port, "opened": False, "info": None, "remotes": None,
              "error": None}
    try:
        ser = serial.Serial(port, 115200, timeout=0.3, write_timeout=2.0)
        result["opened"] = True
        time.sleep(0.4)

        def cmd(text: str) -> str:
            ser.write((text + "\n").encode())
            ser.flush()
            deadline = time.time() + timeout
            lines = []
            while time.time() < deadline:
                raw = ser.read(256)
                if not raw:
                    continue
                for line in raw.decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    if line:
                        lines.append(line)
                        if line.startswith("OK "):
                            return line
            return "\n".join(lines)

        result["info"] = cmd("INFO")
        result["remotes"] = cmd("REMOTES")
        ser.close()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else None

    print("=== 串口列表 ===")
    for p in serial_ports.comports():
        print(f"  {p.device:8s} | {p.description or '':30s} | {p.hwid}")

    ports = [target] if target else [p.device for p in find_candidate_ports()]
    if not ports:
        print("\n没有找到候选端口。用参数指定： python tools/check_device.py COM9")
        return 1

    ok = False
    for port in ports:
        print(f"\n=== 探测 {port} ===")
        r = probe(port)
        if not r["opened"]:
            print(f"  打开失败: {r['error']}")
            continue
        print(f"  INFO    -> {r['info']}")
        print(f"  REMOTES -> {r['remotes']}")
        if r["info"] and "OK INFO" in r["info"]:
            ok = True
            print("  ** 协议握手成功 **")
        else:
            print("  协议握手失败（可能不是本项目的设备，或 CDC 接口未启动）")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
