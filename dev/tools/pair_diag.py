"""
配对诊断：把固件在配对窗口里的每一步落到带时间戳的日志。

用法：
    python dev/tools/pair_diag.py                     # 自动挑名字像遥控器的设备
    python dev/tools/pair_diag.py --addr 7E:A2:AA:BB:CC:02 --type 1
    python dev/tools/pair_diag.py --scan-only         # 只扫描不配对

流程：
    1) INFO 看现状
    2) SCAN 2 持续扫描，收集候选（rssi / 地址 / 类型 / 广播类型）
    3) 选定目标（--addr 指定，否则挑信号最强、名字像遥控器的）
    4) PAIR 发起配对窗口，全程记录 EV SCAN / CONN / CONNFAIL / PAIR / LG
    5) 统计：配对窗口里还扫得到它吗、连接几次、状态码分布

★ 为什么单独写这个 ★
    UI 只给结论，诊断要的是过程：
    「到底有没有扫到它」「扫到时地址类型/广播类型是什么」
    「连接是在看到新鲜广播之后立刻发起的，还是拿过期地址瞎试」。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "dev" / "client" / "src"))

from platforms.windows import SerialPortWin          # noqa: E402
from core.protocol import ProtocolClient, escape, unescape  # noqa: E402

REMOTE_HINTS = ("chromecast", "google", "mi remote", "xiaomi", "语音遥控器", "remote")
KV_RE = re.compile(r"(\w+)=([^ ]*)")


class Diag:
    def __init__(self, log_path: Path):
        self.fh = open(log_path, "w", encoding="utf-8", buffering=1)
        self.t0 = time.time()
        self.cands: dict[str, dict] = {}
        self.scan_hits_during_pair = 0
        self.events: Counter = Counter()
        self.connfails: Counter = Counter()
        self.conns: list[dict] = []
        self.pair_result: dict | None = None
        self.pair_started = 0.0

    def stamp(self) -> float:
        return time.time() - self.t0

    def raw(self, line: str) -> None:
        self.fh.write(f"[{self.stamp():7.2f}] {line}\n")

    def note(self, msg: str) -> None:
        print(f"\n>>> {msg}", flush=True)
        self.raw(f"### {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM12")
    ap.add_argument("--seconds", type=float, default=150, help="配对窗口观察时长")
    ap.add_argument("--scan-seconds", type=float, default=20)
    ap.add_argument("--addr", default="")
    ap.add_argument("--type", type=int, default=0)
    ap.add_argument("--name", default="")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    log_path = Path(args.log) if args.log else (
        REPO / "dev" / "tools" / "logs" /
        f"pair_diag_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    d = Diag(log_path)
    print(f"日志 -> {log_path}", flush=True)

    port = SerialPortWin()
    try:
        port.open(args.port, 115200)
    except Exception as exc:
        d.note(f"打开 {args.port} 失败：{exc}（客户端在跑就先关掉它）")
        return 2

    def on_line(line: str) -> None:
        d.raw(line)
        if not line.startswith("EV "):
            if line.startswith("LG"):
                print("   · " + unescape(line[3:]), flush=True)
            return
        parts = line.split(" ")
        name = parts[1] if len(parts) > 1 else "?"
        kv = {k: unescape(v) for k, v in KV_RE.findall(line)}
        d.events[name] += 1
        if name == "SCAN":
            addr = kv.get("addr", "?")
            fresh = addr not in d.cands
            d.cands[addr] = kv
            if not fresh and d.pair_started:
                d.scan_hits_during_pair += 1
            if not fresh and addr == (args.addr or "") and d.pair_started:
                print(f"   ◎ {d.stamp():.1f}s 又扫到目标 {addr} "
                      f"(rssi={kv.get('rssi')} adv={kv.get('adv')})", flush=True)
        elif name == "CONNFAIL":
            d.connfails[kv.get("why", "?")] += 1
            print(f"   ✘ {d.stamp():.1f}s CONNFAIL {kv.get('why','')}", flush=True)
        elif name == "CONN":
            d.conns.append(kv)
            print(f"   ✔ {d.stamp():.1f}s 连上了：{kv}", flush=True)
        elif name == "PAIR":
            d.pair_result = kv
            print(f"   ■ {d.stamp():.1f}s PAIR 结果：{kv}", flush=True)

    cli = ProtocolClient(port)
    cli.on_raw(on_line)
    cli.start()

    d.note("== 1) INFO ==")
    cli.send("INFO")
    time.sleep(0.8)

    d.note("== 2) SCAN 2 —— 现在请把遥控器设为配对模式（长按 Home 3 秒，LED 闪烁）==")
    cli.send("SCAN", 2)
    # 不赌固定时长：一直扫到出现「像遥控器的」设备，或到达上限。
    # 用户什么时候让遥控器进配对模式都来得及。
    wait_until = time.time() + args.scan_seconds
    last_report = 0.0
    while time.time() < wait_until:
        time.sleep(0.5)
        if any(any(h in c.get("name", "").lower() for h in REMOTE_HINTS)
               for c in d.cands.values()):
            d.note(f"出现了！在 {d.stamp():.1f}s")
            break
        if time.time() - last_report > 5:
            last_report = time.time()
            print(f"   …已扫 {len(d.cands)} 个地址，还没看到遥控器", flush=True)

    named = {a: c for a, c in d.cands.items()
             if any(h in c.get("name", "").lower() for h in REMOTE_HINTS)}
    d.note(f"共 {len(d.cands)} 个地址，名字像遥控器的 {len(named)} 个：")
    for a, c in sorted(named.items(),
                       key=lambda x: -int(x[1].get("rssi", -999) or -999)):
        print(f"   {a}  type={c.get('type')}  adv={c.get('adv')}  "
              f"rssi={c.get('rssi')}  why={c.get('why')}  name={c.get('name')!r}",
              flush=True)
    if args.scan_only:
        cli.send("SCAN", 0)
        cli.stop()
        port.close()
        d.fh.close()
        return 0

    t_addr, t_type = args.addr, args.type
    t_name = args.name
    if not t_addr:
        if not named:
            d.note("没扫到任何像遥控器的设备 —— 不发起配对。"
                   "请确认遥控器进入配对模式后重跑 --scan-only 看看。")
            cli.send("SCAN", 0)
            cli.stop()
            port.close()
            d.fh.close()
            return 3
        best = max(named.items(),
                   key=lambda x: int(x[1].get("rssi", -999) or -999))
        t_addr = best[0]
        t_type = int(best[1].get("type", 0))
        t_name = best[1].get("name", "")
        args.addr = t_addr

    d.note(f"== 3) PAIR {t_addr} type={t_type} '{t_name}' ==")
    d.pair_started = time.time()
    cli.send("PAIR", t_addr, t_type, escape(t_name))

    t_pair = time.time()
    while time.time() - t_pair < args.seconds:
        if d.pair_result is not None or d.conns:
            break
        time.sleep(0.2)

    d.note("== 4) 统计 ==")
    print(f"   事件计数：{dict(d.events)}", flush=True)
    for why, n in d.connfails.items():
        print(f"   CONNFAIL x{n}  why={why}", flush=True)
    print(f"   配对窗口内目标地址又被扫到 {d.scan_hits_during_pair} 次", flush=True)
    print(f"   CONN：{d.conns}", flush=True)
    print(f"   PAIR：{d.pair_result}", flush=True)

    cli.send("SCAN", 0)
    time.sleep(0.3)
    cli.stop()
    port.close()
    d.fh.close()
    ok = bool(d.conns) or (d.pair_result or {}).get("ok") == "1"
    print(f"\n{'成功' if ok else '未连上'}。完整日志：{log_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
