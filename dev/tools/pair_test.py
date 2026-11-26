"""
配对流程端到端验证。

模拟用户在 PC 端配对页的操作：
    1. 发 SCAN 2（开始扫描）
    2. 等候选列表里出现有名字的设备
    3. 选���目标，发 PAIR <addr> <type> <name>
    4. 等 EV PAIR（配对结果）
    5. 发 REMOTES，看已配对列表

用法：
    python dev/tools/pair_test.py --port COM12 --want Chromecast
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client" / "src"))

from platforms.windows import SerialPortWin  # noqa: E402
from core.protocol import ProtocolClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM12")
    ap.add_argument("--want", default="Chromecast")
    ap.add_argument("--scan-seconds", type=float, default=25.0)
    ap.add_argument("--pair-timeout", type=float, default=20.0)
    args = ap.parse_args()

    port = SerialPortWin()
    try:
        port.open(args.port, 115200)
    except Exception as exc:
        print(f"打开串口失败：{exc}")
        return 1

    cli = ProtocolClient(port)
    events: list[tuple[str, dict]] = []
    data: list[tuple[str, dict]] = []
    cli.on_event(lambda n, kv: events.append((n, kv)))
    cli.on_data(lambda k, kv: data.append((k, kv)))
    cli.start()
    time.sleep(0.3)

    print("=" * 62)
    print("[1] 开始扫描（模拟 PC 配对页点「扫描」）")
    print("=" * 62)
    cli.send("SCAN", 2)

    # ---- 等目标出现 ----
    t0 = time.time()
    target = None
    seen: dict[str, dict] = {}
    while time.time() - t0 < args.scan_seconds:
        time.sleep(0.3)
        for n, kv in events:
            if n == "SCAN" and kv.get("name"):
                seen[kv["addr"]] = kv
        for addr, kv in seen.items():
            if args.want.lower() in kv.get("name", "").lower():
                target = kv
                break
        if target:
            break

    print(f"  扫到 {len(seen)} 个有名设备：")
    for addr, kv in list(seen.items())[:12]:
        print(f"    {addr}  {kv.get('name'):<28} {kv.get('rssi')} dBm  "
              f"type={kv.get('type')}")

    if not target:
        print(f"\n  ✘ 没扫到含「{args.want}」的设备。")
        print("    → 请把遥控器长按主页键进配对模式，再跑一次。")
        cli.stop()
        port.close()
        return 1

    print(f"\n  ✔ 找到目标：{target.get('name')}  {target.get('addr')} "
          f"type={target.get('type')}")

    # ---- 发起配对 ----
    print()
    print("=" * 62)
    print("[2] 发起配对（模拟 PC 配对页点「配对」）")
    print("=" * 62)
    n0 = len(events)
    cli.send("PAIR", target["addr"], target.get("type", "0"),
             target.get("name", ""))
    print(f"  >>> PAIR {target['addr']} {target.get('type')} "
          f"{target.get('name')}")

    # ---- 等配对结果 ----
    t0 = time.time()
    paired = None
    while time.time() - t0 < args.pair_timeout:
        time.sleep(0.3)
        for n, kv in events[n0:]:
            if n == "PAIR":
                paired = kv
                break
        if paired:
            break

    if not paired:
        print("\n  ✘ 超时，没收到 EV PAIR")
        for n, kv in events[n0:][-10:]:
            print(f"    {n} {kv}")
        cli.stop()
        port.close()
        return 1

    ok = paired.get("ok") == "1"
    print(f"  EV PAIR ok={paired.get('ok')} addr={paired.get('addr')} "
          f"name={paired.get('name')} status={paired.get('status')}")

    # ---- 看已配对列表 ----
    print()
    print("=" * 62)
    print("[3] 已配对列表（REMOTES）")
    print("=" * 62)
    d0 = len(data)
    cli.send("REMOTES")
    time.sleep(1.2)
    rows = [kv for k, kv in data[d0:] if k == "RM"]
    for kv in rows:
        print(f"    idx={kv.get('idx')} {kv.get('addr')} "
              f"name={kv.get('name')} conn={kv.get('conn')} "
              f"voice={kv.get('voice')}")
    if not rows:
        print("    （空）")

    cli.stop()
    port.close()

    print()
    print("=" * 62)
    if ok and rows:
        print("✔ 配对流程端到端打通")
        return 0
    print("✘ 配对流程未完全打通")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
