"""
USB 枚举检查工具（一键验证）。

重插 USB 后跑这个脚本，直接给出结论：
  - HID 键盘 / 消费者控制接口是否 OK
  - CDC 串口是否 OK
  - CDC 协议握手是否成功

用法：
    python tools/verify_usb.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dev" / "client" / "src"))

from platforms import serial_port as plat   # noqa: E402

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def pnp_status() -> str:
    """取 PnP 设备状态。"""
    try:
        r = subprocess.run(
            [PS, "-NoProfile", "-Command",
             "Get-PnpDevice | Where-Object { $_.InstanceId -match 'VID_303A' } | "
             "ForEach-Object { \"$($_.Status)|$($_.Class)|$($_.FriendlyName)\" }"],
            capture_output=True, text=True, timeout=20, encoding="utf-8",
            errors="replace")
        return r.stdout or ""
    except Exception as exc:
        return f"(查询失败: {exc})\n"


def main() -> int:
    print("=" * 62)
    print("USB 枚举检查")
    print("=" * 62)

    # 1) PnP 状态
    out = pnp_status()
    lines = [l for l in out.splitlines() if l.strip()]
    print("\n--- PnP 设备状态 (VID_303A) ---")
    if not lines:
        print("  没有找到任何 Espressif 设备 —— 设备可能没插上")
        return 1
    ok_count = err_count = 0
    for l in lines:
        parts = l.split("|")
        if len(parts) >= 3:
            status, cls, name = parts[0], parts[1], parts[2]
            flag = ""
            if status == "OK":
                ok_count += 1
                flag = "✓"
            elif status == "Error":
                err_count += 1
                flag = "✗"
            print(f"  {flag} [{status:8s}] {cls:16s} {name}")
    print(f"\n  正常 {ok_count} 个，错误 {err_count} 个")

    # 2) 串口
    print("\n--- 串口 ---")
    ports = plat.find_bridge_ports()
    if not ports:
        print("  没有找到 ESP32-S3 / CH343 串口")
        return 1
    for p in ports:
        print(f"  {p.device:10s} {p.display()}")

    # 3) 协议握手
    print("\n--- 协议握手 ---")
    all_ok = True
    for p in ports:
        try:
            from core import protocol as proto
            import serial
            ser = plat.open_serial(p.device, baud=115200, timeout=0.3)
            time.sleep(0.4)
            proto_obj = proto.BridgeProtocol(ser)
            try:
                # 设备可能刚复位，握手重试两次
                info = None
                last = None
                for _ in range(2):
                    try:
                        info = proto_obj.refresh_info(timeout=8.0)
                        break
                    except Exception as exc:
                        last = exc
                        time.sleep(1.0)
                if info is None:
                    raise last
                print(f"  {p.device:10s} OK   ver={info.version} "
                      f"remotes={info.remotes} voice={info.voice} "
                      f"output={info.output}")
            except Exception as exc:
                print(f"  {p.device:10s} 握手失败: {exc}")
                all_ok = False
            finally:
                proto_obj.close()
        except Exception as exc:
            print(f"  {p.device:10s} 打开失败: {exc}")
            all_ok = False

    print("\n" + "=" * 62)
    if all_ok and err_count == 0:
        print("结论：全部正常")
        return 0
    print("结论：存在问题（见上）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
