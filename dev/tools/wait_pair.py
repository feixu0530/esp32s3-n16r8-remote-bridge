"""
守候配对 —— 一直等着，遥控器一出现就立刻连上。

用法：
    python dev/tools/wait_pair.py --port COM12 --want Chromecast

行为：
    · 持续扫描
    · 一看到名字匹配的设备，**立刻**用扫描到的当前地址发起连接
      （地址必须是最新的那一份，不能用之前记下的）
    · 连上后打印完整的 GATT 发现结果，然后停

Ctrl+C 退出。
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
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    port = SerialPortWin()
    try:
        port.open(args.port, 115200)
    except Exception as exc:
        print(f"打开串口失败：{exc}")
        return 1

    cli = ProtocolClient(port)
    state = {"target": None, "connected": False, "done": False}
    seen: dict[str, dict] = {}

    def on_event(name, kv):
        if name == "SCAN" and kv.get("name"):
            seen[kv["addr"]] = kv
            if args.want.lower() in kv["name"].lower():
                # 只保留最新的一次发现
                state["target"] = kv
        elif name == "CONN":
            state["connected"] = True
        elif name == "PAIR":
            print(f"\n★ EV PAIR {kv}")
        elif name == "GATTDONE":
            print(f"\n★ GATT 发现完成 {kv}")
        elif name == "ATVVREADY":
            print(f"\n★ ATVV 就绪 {kv}")
        elif name == "DISC":
            print(f"\n★ 断开 {kv}")

    def on_raw(line):
        if any(k in line for k in ("EV KEY", "EV LEARN", "DT CTL", "DT AU",
                                   "connected:", "已订阅", "HID 设置完成")):
            print("  ", line)

    cli.on_event(on_event)
    cli.on_raw(on_raw)
    cli.start()
    time.sleep(0.3)

    print(f"守候中…… 目标名字含「{args.want}」（Ctrl+C 退出）")
    cli.send("SCAN", 2)

    t0 = time.time()
    last_scan = 0.0
    tried = set()
    while time.time() - t0 < args.timeout and not state["done"]:
        time.sleep(0.25)
        now = time.time()

        # 每 8 秒重启一轮扫描（会话内固件自己也在轮转）
        if now - last_scan >= 8:
            last_scan = now
            cli.send("SCAN", 2)

        t = state["target"]
        if t and t["addr"] not in tried:
            tried.add(t["addr"])
            print(f"\n[{now - t0:6.1f}s] 发现 {t['name']}  {t['addr']} "
                  f"type={t.get('type')} rssi={t.get('rssi')} "
                  f"adv={t.get('adv')}  -> 立刻连接")
            cli.send("PAIR", t["addr"], t.get("type", "0"), t["name"])
            # 连接的 10 秒内不做别的尝试
            time.sleep(10)
            if not state["connected"]:
                print("     连接没成功，继续守候下一个广播窗口")

        if state["connected"]:
            time.sleep(6)
            cli.send("REMOTES")
            time.sleep(2)
            state["done"] = True

    cli.stop()
    port.close()
    if state["connected"]:
        print("\n✔ 连上了")
        return 0
    print("\n超时/退出，没连上")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
