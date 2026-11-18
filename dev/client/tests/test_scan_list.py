"""
候选设备列表的行为验证（学 Windows「添加设备」）。

验证：
    · 默认只显示「有名字 + 信号强」的设备
    · 全部按信号从强到弱排序
    · 「显示所有设备」能展开出未命名/信号弱的
    · 同一地址反复上报不会产生重复行
    · 已配对列表能重命名，而且自定义名字不会被固件的广播名冲掉
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ★ 必须在 import core.* 之前 ★  数据根指到临时目录，别碰用户真机配好的遥控器
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-scanlist-")

os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import config as cfgmod  # noqa: E402
from core.controller import BridgeController  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def scan_event(kv: dict) -> None:
    ctl._on_event("SCAN", kv)


ctl = BridgeController()

print("=" * 60)
print("候选设备列表行为验证")
print("=" * 60)

# --- 造一批候选 ---
scan_event({"addr": "AA:BB:CC:DD:EE:01", "name": "Chromecast Remote",
            "rssi": "-55", "type": "0", "score": "100"})
scan_event({"addr": "AA:BB:CC:DD:EE:02", "name": "",
            "rssi": "-45", "type": "0", "score": "40"})
scan_event({"addr": "AA:BB:CC:DD:EE:03", "name": "K3_522b22",
            "rssi": "-72", "type": "1", "score": "20"})
scan_event({"addr": "AA:BB:CC:DD:EE:04", "name": "",
            "rssi": "-90", "type": "1", "score": "1"})
scan_event({"addr": "AA:BB:CC:DD:EE:05", "name": "BH9254413",
            "rssi": "-60", "type": "0", "score": "20"})

check("总数统计正确", ctl.totalCount == 5, str(ctl.totalCount))

rows = [ctl.scanModel.get(i) for i in range(ctl.scanModel.rowCount())]
names = [r["name"] for r in rows]
check("默认只显示有名字的", all(n for n in names), str(names))
check("默认剔除了信号弱的（-90）", "AA:BB:CC:DD:EE:04" not in
      [r["addr"] for r in rows], str(rows))
check("默认显示 3 个", ctl.nearbyCount == 3, str(ctl.nearbyCount))

rssi_list = [r["rssi"] for r in rows]
check("按信号从强到弱排序", rssi_list == sorted(rssi_list, reverse=True),
      str(rssi_list))

# --- 展开全部 ---
ctl.setShowAllDevices(True)
rows = [ctl.scanModel.get(i) for i in range(ctl.scanModel.rowCount())]
check("显示全部后有 5 个", len(rows) == 5, str(len(rows)))
check("未命名的也出来了",
      any(r["name"] == "" for r in rows), str([r["addr"] for r in rows]))
rssi_list = [r["rssi"] for r in rows]
check("展开后仍按信号排序", rssi_list == sorted(rssi_list, reverse=True),
      str(rssi_list))

# --- 收回去 ---
ctl.setShowAllDevices(False)
check("收回到附近的 3 个", ctl.scanModel.rowCount() == 3,
      str(ctl.scanModel.rowCount()))

# --- 同一地址重复上报不产生重复行 ---
before = ctl.scanModel.rowCount()
scan_event({"addr": "AA:BB:CC:DD:EE:01", "name": "Chromecast Remote",
            "rssi": "-50", "type": "0", "score": "100"})
check("重复上报不新增行", ctl.scanModel.rowCount() == before,
      f"{before} -> {ctl.scanModel.rowCount()}")
check("总数不涨", ctl.totalCount == 5, str(ctl.totalCount))

# --- 名字后来解析出来 ---
scan_event({"addr": "AA:BB:CC:DD:EE:02", "name": "BH9254413",
            "rssi": "-45", "type": "0", "score": "20"})
check("名字补上后进入默认视图", ctl.nearbyCount == 4, str(ctl.nearbyCount))

# --- 已配对列表重命名 ---
print("\n[已配对列表重命名]")


def remote_row(idx: int, addr: str, name: str, conn: int = 1) -> None:
    ctl._on_remote_row({"idx": str(idx), "addr": addr, "name": name,
                        "type": "0", "conn": str(conn)})


ctl._cfg["remotes"] = []
remote_row(0, "AA:BB:CC:DD:EE:11", "HID_Device")
remote_row(1, "AA:BB:CC:DD:EE:12", "HID_Device", conn=0)
check("两台同广播名的遥控器都在列表里",
      ctl.remoteModel.rowCount() == 2,
      str([ctl.remoteModel.get(i) for i in range(2)]))

check("重命名成功", ctl.setRemoteName(0, "客厅那台"))
check("列表立刻显示新名字",
      ctl.remoteModel.get(0).get("name") == "客厅那台",
      str(ctl.remoteModel.get(0)))
check("另一台不受影响",
      ctl.remoteModel.get(1).get("name") == "HID_Device",
      str(ctl.remoteModel.get(1)))

# ★ 固件每次上报遥控器都带广播名，自定义名字不能被冲掉 ★
remote_row(0, "AA:BB:CC:DD:EE:11", "HID_Device")
check("固件再上报不会冲掉自定义名字",
      ctl.remoteModel.get(0).get("name") == "客厅那台",
      str(ctl.remoteModel.get(0)))
saved = cfgmod.load_config().get("remotes", [])
check("自定义名字落盘了",
      len(saved) > 0 and saved[0].get("custom_name") == "客厅那台",
      str(saved))

check("越界索引被拒", not ctl.setRemoteName(9, "x"))
check("空索引以外的地址也不会误改",
      ctl.remoteModel.get(1).get("name") == "HID_Device")

# 清掉自定义名字 → 下一次上报恢复广播名
ctl.setRemoteName(0, "")
remote_row(0, "AA:BB:CC:DD:EE:11", "HID_Device")
check("清除自定义名字后恢复广播名",
      ctl.remoteModel.get(0).get("name") == "HID_Device",
      str(ctl.remoteModel.get(0)))

print("\n" + "=" * 60)
if FAILURES:
    print(f"失败 {len(FAILURES)} 项：")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("全部通过")
raise SystemExit(0)
