"""
判读板子 COM6 日志：按下/抬起是否成对、有没有卡住的键。

用法：
    python dev/client/tests/analyze_board_log.py <board.log> [--max-hold 500]

为什么要有它：
  · v0.22.x 的核心事故是「抬起丢了 → 键永远按在 PC 上 → 系统重复删光文字」。
    判据就一条：**每次 kbd press 后面必须有 kbd release**。
    人眼看几十行日志很容易漏，尤其短按只有 60~210 ms。
  · 验收要求「每一条都要抓 COM6 日志留证」，那就得有机器判据，不能靠印象。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LINE = re.compile(r"^[IWED]\s*\((\d+)\)\s*(\S+):\s*(.*)$")
HIDTX = re.compile(r"HIDTX .*?mod=([0-9A-F]{2}) key0=([0-9A-F]{2}) ok=(\d)")
PRESS = re.compile(r"kbd press mod=([0-9A-F]{2}) count=(\d+)")
RELEASE = re.compile(r"kbd release mod=([0-9A-F]{2}) count=(\d+)")


def analyze(path: Path, max_hold_ms: int) -> int:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[tuple[int, str, str]] = []
    n_ok = n_fail = n_defer = n_resend = 0
    audio_start = audio_stop = 0

    for raw in lines:
        m = LINE.match(raw.strip())
        if not m:
            continue
        ms = int(m.group(1))
        text = m.group(3)

        t = HIDTX.search(text)
        if t:
            if t.group(3) == "1":
                n_ok += 1
            else:
                n_fail += 1
            continue
        t = PRESS.search(text)
        if t:
            events.append((ms, "press", t.group(1)))
            continue
        t = RELEASE.search(text)
        if t:
            events.append((ms, "release", t.group(1)))
            continue
        # USB 不 ready 时，按下/抬起只记状态、报告走补发 —— 逻辑上已经发生了
        if "HID 暂不可用" in text:
            n_defer += 1
            events.append((ms, "release" if "抬起" in text else "press", "??"))
            continue
        if "补发之前发不出去的 HID 报告" in text:
            n_resend += 1
        elif "AUDIO_START" in text:
            audio_start += 1
        elif "AUDIO_STOP" in text:
            audio_stop += 1

    holds: list[int] = []
    stuck: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for ms, kind, mod in events:
        if kind == "press":
            if pending is not None:
                stuck.append(pending)      # 上一次还没抬起就又按下
            pending = (ms, mod)
        else:
            if pending is None:
                continue                   # 无按下记录的抬起：忽略
            holds.append(ms - pending[0])
            pending = None
    if pending is not None:
        stuck.append(pending)

    print(f"日志：{path}")
    print(f"  行数            {len(lines)}")
    print(f"  HIDTX ok=1 / 0  {n_ok} / {n_fail}")
    print(f"  press / release {sum(1 for e in events if e[1] == 'press')} / "
          f"{sum(1 for e in events if e[1] == 'release')}")
    print(f"  暂不可用 / 补发  {n_defer} / {n_resend}")
    print(f"  语音开始 / 停止  {audio_start} / {audio_stop}")
    if holds:
        print(f"  按住时长(ms)    最短 {min(holds)} / 最长 {max(holds)} / "
              f"平均 {sum(holds) // len(holds)}")
        over = [h for h in holds if h > max_hold_ms]
        if over:
            print(f"  超过 {max_hold_ms} ms 的按住：{over}")

    print()
    if not events:
        print("⚠ 没有任何按键事件 —— 遥控器没按，或日志抓的不是 COM6。")
        return 2
    if stuck:
        for ms, mod in stuck:
            print(f"✘ 卡住的键：t={ms} ms mod={mod} 只有按下没有抬起")
        print(f"✘ {len(stuck)} 个键没有抬起 —— 这就是「键按死在 PC 上」的信号")
        return 1
    print(f"✔ {len(holds)} 次按下全部成对抬起")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    max_hold = 500
    if "--max-hold" in sys.argv:
        max_hold = int(sys.argv[sys.argv.index("--max-hold") + 1])
    if not path.is_file():
        print(f"找不到日志：{path}")
        return 2
    return analyze(path, max_hold)


if __name__ == "__main__":
    sys.exit(main())
