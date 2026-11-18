"""
人话日志：把协议事件翻译成「谁按了什么键，变成了什么键」。

★ 为什么单独一个模块 ★
   诊断面板里原来直接怼协议原文：

       EV KEY idx=0 src=2 usage=547 state=down
       EV MAPOUT key=8739 trig=0 out=1 hid_ready=1

   现场根本看不懂 —— 用户要的是这种：

       [21:04:31] 客厅遥控器 (7E:A2:99:61:45:FE)  按下「主页键」
       [21:04:31] 客厅遥控器 (7E:A2:99:61:45:FE)  「主页键」单击 命中映射 → 变成 lctrl + lshift + l

   协议原文仍然**完整**写进 device.log（工程视角留档），
   面板只放给人看的。

★ 时间戳哪来的 ★
   板端日志缓冲里没有时间戳（ESP_LOG 的 "(12345)" 以前被丢掉了），
   所以这里用 **PC 收到这条的时间** —— 对「刚才那一下发生了什么」够用。
"""
from __future__ import annotations

import time

# 固件的 trig 编号（br_mapping.c 里的 BR_TRIG_*）
TRIGGER_NAMES = {0: "单击", 1: "双击", 2: "长按"}


def stamp() -> str:
    """[HH:MM:SS] —— 什么时候发生的。"""
    return time.strftime("[%H:%M:%S]")


def who(name: str, addr: str = "") -> str:
    """遥控器身份：名字 + 地址。"""
    if name and addr:
        return f"{name} ({addr})"
    return name or addr or "未知遥控器"


def key_event(kv: dict, remote: str, key_name: str) -> str:
    """EV KEY —— 按下了哪个键。"""
    state = str(kv.get("state", "down") or "down")
    verb = "松开" if state == "up" else "按下"
    return f"{stamp()} {remote}  {verb}「{key_name}」"


def mapout(kv: dict, remote: str, key_name: str, out_text: str) -> str:
    """EV MAPOUT —— 命中映射，变成了什么键。"""
    try:
        trig = int(kv.get("trig", "0") or 0)
    except (TypeError, ValueError):
        trig = 0
    name = TRIGGER_NAMES.get(trig, "单击")
    tail = f" → 变成 {out_text}" if out_text else ""
    return f"{stamp()} {remote}  「{key_name}」{name} 命中映射{tail}"


def mapmiss(kv: dict, remote: str, key_name: str) -> str:
    """EV MAPMISS —— 这个键没配映射。"""
    return f"{stamp()} {remote}  「{key_name}」没配映射 → 不动作"


def voice_event(kv: dict, remote: str, hotkey_text: str) -> str:
    """EV VOICE —— 遥控器语音键按下了，板子正替电脑按语音快捷键。

    ★ 板子没按出去时要说实话 ★
      固件现在会报 sent（热键有没有真的按）和 on（语音通道开没开）。
      sent=0 时以前还写「板子替电脑按出 …」，用户看日志以为按了，
      其实输入法一个字都没收到 —— 日志不能骗人。
    """
    sent = str(kv.get("sent", "1") or "1")
    on = str(kv.get("on", "1") or "1")
    if sent == "0":
        why = "语音通道被关了" if on == "0" else "板子这次没按出热键"
        return f"{stamp()} {remote}  语音键按下 → **没按热键**（{why}）"
    return f"{stamp()} {remote}  语音键按下 → 板子替电脑按出 {hotkey_text}"


def board_log(level: str, tag: str, msg: str) -> str:
    """板端日志 —— 也带上时间戳，好按时间顺序看。"""
    who_ = tag or "板端"
    return f"{stamp()} {who_} {msg}"


def pair_event(text: str) -> str:
    """配对/解绑这类板端事件。"""
    return f"{stamp()} 板端 {text}"
