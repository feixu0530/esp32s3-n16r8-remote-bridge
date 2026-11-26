"""
P0 探针驱动 —— 自动跑一遍「扫描 → 连接 → GATT 全量转储 → ATVV 订阅」，
并解析出结论。

用法：
    python dev/tools/probe_atvv.py --port COM12 --seconds 60

输出结论：
    · 扫到哪些设备
    · 连上了没有
    · GATT 里有没有 AB5E0001（ATVV）服务
    · ATVV 的 audio / control 特征句柄
    · 按住语音键时 control 通道吐出的原始字节（用于定死 opcode 语义）
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import serial

ATVV_SERVICE = "AB5E0001-5A21-4F05-BC7D-AF01F617B664"
ATVV_AUDIO = "AB5E0003-5A21-4F05-BC7D-AF01F617B664"
ATVV_CTL = "AB5E0004-5A21-4F05-BC7D-AF01F617B664"
ATVV_TX = "AB5E0002-5A21-4F05-BC7D-AF01F617B664"

KV_RE = re.compile(r"(\w+)=([^\s]*)")


def parse_kv(line: str) -> dict:
    return {m.group(1): m.group(2) for m in KV_RE.finditer(line)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM12")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--target", default="Chromecast",
                    help="目标设备名字子串，留空表示不限")
    ap.add_argument("--out", default="", help="把原始输出写到文件")
    ap.add_argument("--interactive", action="store_true",
                    help="保持监听并从 stdin 读命令")
    args = ap.parse_args()

    try:
        s = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as exc:
        print(f"打不开 {args.port}: {exc}", file=sys.stderr)
        return 1

    sink = open(args.out, "w", encoding="utf-8", errors="replace") if args.out else None

    def send(cmd: str) -> None:
        s.write((cmd + "\n").encode("ascii"))
        s.flush()
        print(f"[发送] {cmd}", flush=True)

    time.sleep(0.6)
    send("INFO")
    send("TARGET " + args.target if args.target else "TARGET _none_")
    send("SCAN 2")
    send("AUTO 1")

    scans: list[dict] = []
    conns: list[dict] = []
    svcs: list[dict] = []
    chrs: list[dict] = []
    atvv_lines: list[dict] = []
    ctl_bytes: list[tuple[int, str]] = []
    au_bytes: list[tuple[int, str]] = []
    other_notify = 0
    t0 = time.time()

    print("\n=== 开始监听（按住遥控器语音键可以测试）===\n", flush=True)
    buf = bytearray()
    while time.time() - t0 < args.seconds:
        chunk = s.read(4096)
        if chunk:
            buf += chunk
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if not text:
                continue
            if sink:
                sink.write(f"{time.time()-t0:8.3f} {text}\n")
                sink.flush()
            print(text, flush=True)

            if text.startswith("EV SCAN "):
                scans.append(parse_kv(text))
            elif text.startswith("EV CONN "):
                conns.append(parse_kv(text))
            elif text.startswith("DT SVC "):
                svcs.append(parse_kv(text))
            elif text.startswith("DT CHR "):
                chrs.append(parse_kv(text))
            elif text.startswith("DT ATVV "):
                atvv_lines.append(parse_kv(text))
            elif text.startswith("DT CTL "):
                ctl_bytes.append((int(time.time() - t0), parse_kv(text).get("hex", "")))
            elif text.startswith("DT AU "):
                au_bytes.append((int(time.time() - t0), parse_kv(text).get("hex", "")))
            elif text.startswith("DT NT "):
                other_notify += 1

    if sink:
        sink.close()
    s.close()

    # ---------------- 结论 ----------------
    print("\n" + "=" * 60)
    print("P0 探针结论")
    print("=" * 60)
    print(f"扫到候选 {len(scans)} 个：")
    for c in scans[:20]:
        print(f"   {c.get('addr')}  {c.get('name','')}  rssi={c.get('rssi')} score={c.get('score')} ({c.get('why')})")

    print(f"\n连接事件 {len(conns)} 个：")
    for c in conns:
        print(f"   idx={c.get('idx')} {c.get('addr')} {c.get('name','')}")

    print(f"\n服务 {len(svcs)} 个：")
    for v in svcs:
        print(f"   {v.get('uuid')}  (handle {v.get('start')}..{v.get('end')})")

    atvv_svcs = [v for v in svcs if v.get("uuid", "").upper() == ATVV_SERVICE]
    print(f"\n★ ATVV 服务（{ATVV_SERVICE}）: "
          f"{'✔ 可见' if atvv_svcs else '✘ 不可见'}")

    print(f"\nATVV 特征行 {len(atvv_lines)} 条：")
    for a in atvv_lines:
        print(f"   {a}")

    for name, want in (("audio", ATVV_AUDIO), ("ctl", ATVV_CTL), ("tx", ATVV_TX)):
        hit = [c for c in chrs if c.get("uuid", "").upper() == want]
        if hit:
            print(f"   {name}: handle={hit[0].get('handle')} props={hit[0].get('props')} cccd={hit[0].get('cccd')}")
        else:
            print(f"   {name}: 未发现")

    print(f"\ncontrol 通道收到 {len(ctl_bytes)} 帧：")
    for t, h in ctl_bytes[:40]:
        print(f"   t={t:7.3f}  {h}")

    print(f"\naudio 通道收到 {len(au_bytes)} 帧"
          f"（长度分布 {sorted(set(len(h)//2 for _, h in au_bytes))}）")
    for t, h in au_bytes[:10]:
        print(f"   t={t:7.3f}  {h[:80]}{'...' if len(h) > 80 else ''}")

    print(f"\n其它属性的通知 {other_notify} 条")

    if atvv_svcs:
        print("\n结论：ESP32 侧能看到 ATVV —— 方案成立，继续做固件 ATVV 客户端。")
    else:
        print("\n结论：ESP32 侧看不到 ATVV —— 需走降级方案（PC 侧 WinRT 直连）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
