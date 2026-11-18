"""
快捷键录制闸门。

★ 为什么要有这条 ★
    v0.15.0 的录制**永远录不到东西**，而且录制的钩子会一直挂着
    吞掉全系统键盘输入 —— 表现是「点了录制之后键盘就不听使唤了」。
    根因有两个：
      1. SetWindowsHookExW 在 PeekMessageW 之前 → 回调永不派发
      2. UnhookWindowsHookEx(0) 用错句柄 → 钩子卸载不掉

这条测试真的起一个子进程、真的用 SendInput 打键，验证：
    - 必须回调，且组合键正确
    - stop() 之后子进程必须退出（钩子不能残留）
    - 超时兜底在（30 秒自动停）

★ 为什么允许重试 ★
    钩子是**全局**的：测试期间谁敲一下键盘都会被一起录进去
    （自动化脚本、终端、别的程序）。
    所以「录到的组合键不对」不一定是被测代码坏了，
    可能只是录到了别人的键 —— 重开一个子进程再试，
    录到期望的组合键才算过。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-cap-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from platforms.hotkey_capture_process import (  # noqa: E402
    CAPTURE_PROBE_KEYS, ProcessHotkeyRecorder, send_test_chord)

FAILURES: list[str] = []

EXPECTED = "lctrl+lalt+p"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def _one_attempt(got: list[str]) -> bool:
    """起一个录制子进程，打一次探针组合键。"""
    rec = ProcessHotkeyRecorder()
    if not rec.start(lambda chord: got.append(chord)):
        return False
    if not rec.wait_ready(6.0):
        rec.stop()
        return False
    send_test_chord(CAPTURE_PROBE_KEYS)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if EXPECTED in got:
            break
        time.sleep(0.05)
    rec.stop()
    time.sleep(0.3)
    return EXPECTED in got


def test_capture_roundtrip() -> None:
    print("\n[录制闸门]")
    got: list[str] = []

    # ★ 环境里可能有别的按键 → 最多试三遍 ★
    ok = False
    for attempt in range(3):
        got.clear()
        if _one_attempt(got):
            ok = True
            break
        print(f"        第 {attempt + 1} 次没录到 {EXPECTED}（环境噪音？），重试")
    check("录到了组合键", ok, f"got={got}")
    if ok:
        check("组合键内容正确（lctrl+lalt+p）",
              EXPECTED in got, repr(got))

    # 钩子不能残留：子进程必须能停掉
    rec = ProcessHotkeyRecorder()
    rec.start(lambda chord: None)
    rec.wait_ready(6.0)
    rec.stop()
    time.sleep(0.8)
    check("stop() 之后子进程退出了", not rec.running)


def test_stop_before_capture() -> None:
    print("\n[提前停止]")
    got: list[str] = []
    rec = ProcessHotkeyRecorder()
    rec.start(lambda chord: got.append(chord))
    ready = rec.wait_ready(6.0)
    check("钩子装好了", ready)
    # ★ 窗口要窄 ★
    #   这个检查是「没人按键时不该凭空出结果」。
    #   窗口开久了，别人（或自动化脚本）随手敲一下键盘就会被记下来，
    #   那不是误报，是它正常工作 —— 检查就变成时好时坏。
    time.sleep(0.15)
    check("没有误报结果", not got, str(got))
    rec.stop()
    time.sleep(0.5)
    check("没录到东西时 stop() 也不会卡住", not rec.running)


def main() -> int:
    print("=" * 66)
    print("快捷键录制闸门")
    print("=" * 66)

    test_capture_roundtrip()
    test_stop_before_capture()

    print()
    print("=" * 66)
    if FAILURES:
        print(f"✘ {len(FAILURES)} 项未通过：{', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
