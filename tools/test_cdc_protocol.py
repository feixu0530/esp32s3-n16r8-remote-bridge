#!/usr/bin/env python3
"""
CDC 协议链路测试（对应 firmware/ble_hid_bridge_main.c: br_proto_handle_line() 的输出）。

发送一系列协议命令到 COM6，验证固件是否实时应答。
这些命令是真实客户端会发出的：
  INFO     -> 设备信息
  REMOTES  -> 已配对设备列表
  PAIR <N>-> 配对指定遥控器（如果未配对）
  SET      -> 设置 HID map
  BIND     -> 绑定 BLE（模拟按键组合，用于测试语音通道）
  SAVEALL  -> 保存到 NVS

测试目标：验证 bridge_usb_write() -> tinyusb_cdcacm_write_queue + flush 是否实时有效。
"""
import argparse
import sys
import time
import serial
import threading
from collections import deque

# === CDC protocol commands (与 firmware/ble_hid_bridge_main.c:br_proto_handle_line 对应) ===
COMMANDS = [
    ("INFO", "设备信息，验证 CDC 连接"),
    ("REMOTES", "已配对设备列表"),
    ("SET 0 1066 1 1 4B", "设置 HID 映射"),
    ("BIND 0", "列出某台遥控器的映射"),
    ("PAIR 0", "配对遥控器 0（如果未配对）"),
    ("SAVEALL", "保存所有配置到 NVS"),
    ("CDCSTAT", "CDC 内部状态诊断"),
    ("BOGUS", "未知命令（应返回 ERR unknown）"),
]


def send_command(port, cmd: str) -> str:
    """发送一条 CDC 协议命令，等待"OK ..."开头的响应行。"""
    if not port.is_open:
        try:
            timeout = 10.0
            while not port.is_open and timeout > 0:
                time.sleep(0.5)
                timeout -= 0.5
            return f"OPEN FAIL"

        except serial.SerialException as e:
            return f"OPEN FAIL [{e}]"

    # 刷新输入流（确保没有残留数据）
    port.flushInput()
    port.flushOutput()

    # 去掉尾部\r（如果有用户加上的回车）
    if isinstance(cmd, str) and cmd.endswith("\r"):
        cmd = cmd[:-1]

    port.write((cmd + "\n").encode("ascii", errors="replace"))
    port.flush()

    start = time.monotonic()
    timeout = 8.0
    resp = b""
    try:
        # 收集数据直到出现 "OK ..." 或 "ERR ..." 结尾行。
        # 固件有些命令（REMOTES / BIND）会先发若干行 R/B 数据，最后才发 OK 汇总行，
        # 所以不能只读第一行就返回。
        while time.monotonic() - start < timeout:
            if port.in_waiting:
                resp += port.read(port.in_waiting)
                text = resp.decode("utf-8", errors="replace")
                lines = [l.strip() for l in text.replace("\r\n", "\n").split("\n") if l.strip()]
                if lines and (lines[-1].startswith("OK ") or lines[-1].startswith("ERR ")):
                    break
            else:
                time.sleep(0.05)
    except serial.SerialException:
        return "ERR"

    if not resp:
        return "TIMEOUT"

    try:
        resp_str = resp.decode("utf-8", errors="replace")
        # ✅ 匹配固件的应答前缀：
        #   "OK {CMD}" 成功
        #   "ERR ..." 参数错误/未知命令（也是有效应答，说明链路通）
        prefix = f"OK {cmd.upper().split()[0]}" if cmd.strip() else ""
        first = resp_str.strip().split("\n")[0] if resp_str.strip() else ""
        if first.startswith(prefix):
            return f"OK [{resp_str.strip()[:120]}]"
        if first.startswith("ERR"):
            return f"ERR [{first[:80]}]"
    except Exception:
        pass

    return "TIMEOUT"


def protocol_test(port):
    """按顺序发送所有命令，测试 CDC 协议链路。"""
    print("=== CDC 协议链路 ===")

    results = []
    for cmd, note in COMMANDS:
        result = send_command(port, cmd + "\n")
        # OK 和 ERR 都算链路通（ERR 说明固件处理了命令并回了错误）
        status = "PASS" if ("OK [" in result or "ERR [" in result) else \
                 ("TIMEOUT" if "TIMEOUT" in result else "FAIL")
        results.append(result)
        print(f"{cmd:>18} | {status:<8} | {note}")

    # 简单统计
    ok_count = sum(1 for r in results if "OK [" in r or "ERR [" in r)
    total = len(results)
    print("=== CDC Result: {} / {} 命令得到应答 ===".format(ok_count, total))


def main():
    parser = argparse.ArgumentParser(description="Test CDC protocol")
    parser.add_argument("-p", "--port", default=None, help="Serial port (e.g. COM6)")
    args = parser.parse_args()

    if args.port:
        try:
            port = serial.Serial(args.port, 460800, timeout=1)
            protocol_test(port)
            port.close()
        except serial.SerialException as e:
            print(f"Failed to open {args.port}: {e}")

    else:
        # Windows：先等待枚举完成再测试
        time.sleep(3)

        available = serial.tools.list_ports.comports()
        if not available:
            print("No serial ports found")
            return 1

        # ❌ 上一版逻辑错误：next()在 args.port=None 时会抛出 StopIteration
        if args.port:
            port_match = next((p for p in available if p.device == args.port), None)
        else:
            # 没有指定端口，取第一个 ESP32 设备（VID=303A）
            esp_ports = [p for p in available if p.vid == 0x303A]
            port_match = esp_ports[0] if esp_ports else None

        if not port_match:
            print("No suitable port found")
            return 1

        try:
            port_serial = serial.Serial(port_match.device, 460800, timeout=1)
            protocol_test(port_serial)
            port_serial.close()
        except Exception as e:
            print(f"CDC test failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
