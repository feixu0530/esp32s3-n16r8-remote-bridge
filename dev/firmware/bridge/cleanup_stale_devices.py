"""
刷完固件后清理 Windows 里的「未连接的旧设备实例」。

★ 为什么需要 ★
    板子每次复位，Windows 都会给它的新枚举分配一个新的实例号
    （实例 ID 里 &4&0002 的那个计数就是重枚举的次数）。
    旧实例不会消失，只是变成「未连接」，还留在设备管理器里，
    状态显示 Unknown。

    刷一次固件就多一条这样的残留 —— 攒多了设备管理器里全是
    「USB JTAG/serial debug unit / 未知设备」，看着像坏了。

★ 弹窗本身说明不了固件坏了 ★
    「无法识别的 USB 设备」是在**枚举切换的那一瞬间**弹的：
    下载模式（ROM 的 JTAG/serial）→ 我们的复合设备，
    中间 Windows 读描述符时设备还没准备好。
    弹完之后设备会被重新识别、正常工作 ——
    刷固件频繁触发重新枚举，所以每次都看得到这个提示。

用法：
    python dev/firmware/bridge/cleanup_stale_devices.py
"""
from __future__ import annotations

import subprocess
import sys

# 我们板子的 VID（Espressif）
OUR_VID = "VID_303A"


def stale_instances() -> list[tuple[str, str]]:
    """列出「未连接 + 状态异常 + 是我们板子」的设备实例。"""
    ps = (
        "Get-PnpDevice | Where-Object { "
        "$_.InstanceId -like '*VID_303A*' -and "
        "$_.Present -eq $false -and "
        "$_.Status -ne 'OK' "
        "} | ForEach-Object { $_.InstanceId + '|' + $_.FriendlyName }"
    )
    # PowerShell 的输出是 GBK/UTF-8 混杂的，用 errors="replace" 兜住
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=90)
    items = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        inst, name = line.split("|", 1)
        inst = inst.strip()
        if OUR_VID in inst.upper():
            items.append((inst, name.strip()))
    return items


def main() -> int:
    items = stale_instances()
    if not items:
        print("没有需要清理的残留设备")
        return 0
    print(f"发现 {len(items)} 个未连接的旧设备实例（刷一次固件就多一条）：")
    for inst, name in items[:10]:
        print(f"  {name}  ({inst})")
    if len(items) > 10:
        print(f"  …… 还有 {len(items) - 10} 条")
    ok = 0
    for inst, _name in items:
        r = subprocess.run(
            ["pnputil", "/remove-device", inst, "/subtree"],
            capture_output=True, timeout=90)
        if r.returncode == 0:
            ok += 1
    if ok:
        print(f"已移除 {ok} 个")
        return 0
    print()
    print("─" * 66)
    print("★ 自动清理需要**管理员权限**（移除设备是系统级操作）")
    print("  请用管理员身份打开 PowerShell，执行：")
    print("    python dev/firmware/bridge/cleanup_stale_devices.py")
    print("  这些是未连接的旧实例，留着不影响板子使用，")
    print("  只是设备管理器里看着脏。")
    print("─" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
