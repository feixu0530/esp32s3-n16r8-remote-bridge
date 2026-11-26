"""
常驻自动配对助手。

一直扫描，只要目标遥控器一出现就自动发起配对。
用户什么时候按遥控器都行 —— 醒了就会被抓住。

用法：
    python dev/tools/auto_pair.py --port COM12 --want Chromecast
    Ctrl+C 退出
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
    ap.add_argument("--scan-seconds", type=float, default=8.0)
    args = ap.parse_args()

    port = SerialPortWin()
    try:
        port.open(args.port, 115200)
    except Exception as exc:
        print(f"打开串口失败：{exc}")
        return 1

    cli = ProtocolClient(port)
    events: list[tuple[str, dict]] = []
    cli.on_event(lambda n, kv: events.append((n, kv)))
    cli.start()
    time.sleep(0.3)

    print(f"自动配对助手已启动 —— 目标名字含「{args.want}」")
    print("请按遥控器上的任意键把它唤醒，唤醒后会自动完成配对。\n")

    t_start = time.time()
    last_scan = 0.0
    done = False

    try:
        while not done:
            now = time.time()

            # 每 scan-seconds 秒重启一轮扫描（固件每次扫描会清空候选）
            if now - last_scan >= args.scan_seconds:
                last_scan = now
                cli.send("SCAN", 2)

            # 找目标
            target = None
            for n, kv in events:
                if n == "SCAN" and kv.get("name") and \
                        args.want.lower() in kv["name"].lower():
                    target = kv
                    break

            if target is not None:
                print(f"[{time.time()-t_start:6.1f}s] 发现目标："
                      f"{target.get('name')}  {target.get('addr')}  "
                      f"type={target.get('type')}  {target.get('rssi')} dBm")
                cli.send("PAIR", target["addr"], target.get("type", "0"),
                         target.get("name", ""))
                print(f"           >>> 已发起 PAIR，等待结果…")
                events.clear()
                time.sleep(0.5)
                # 等 EV PAIR
                t0 = time.time()
                while time.time() - t0 < 25:
                    time.sleep(0.3)
                    for n, kv in events:
                        if n == "PAIR":
                            ok = kv.get("ok") == "1"
                            print(f"           EV PAIR ok={kv.get('ok')} "
                                  f"addr={kv.get('addr')} "
                                  f"name={kv.get('name')} "
                                  f"status={kv.get('status')}")
                            if ok:
                                print("\n✔ 配对成功！")
                                cli.send("REMOTES")
                                time.sleep(1.0)
                                for n2, kv2 in events:
                                    pass
                                done = True
                            break
                    if done:
                        break

            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        cli.stop()
        port.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
