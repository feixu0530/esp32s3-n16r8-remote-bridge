"""
PC 侧动作执行器。

板子负责把动作翻译成 HID 按键（拔掉 PC 也能用）；
「打开终端」「打开 Edge」「鼠标左键单击」这类必须由 PC 参与的，
只能在客户端里执行。

板子对每个物理键都发 `EV KEY`（src/usage/state/addr），
controller 在 PC 侧自己做单击/双击/长按判定（阈值与固件一致），
命中 PC 专属动作时调这里的 run_pc_action()。
"""
from __future__ import annotations

from .action_options import application_command
from .key_mapping import ActionKind, ButtonAction
from platforms.windows import launch_command, mouse_event

APP_KINDS = frozenset(k for k in ActionKind if k.value.startswith("open_"))

MOUSE_CLICK = {
    ActionKind.MOUSE_LEFT_CLICK: "left",
    ActionKind.MOUSE_RIGHT_CLICK: "right",
    ActionKind.MOUSE_MIDDLE_CLICK: "middle",
    ActionKind.MOUSE_X1_CLICK: "x1",
    ActionKind.MOUSE_X2_CLICK: "x2",
}

MOUSE_HOLD = {
    ActionKind.MOUSE_LEFT_HOLD: "left",
    ActionKind.MOUSE_RIGHT_HOLD: "right",
    ActionKind.MOUSE_X1_HOLD: "x1",
    ActionKind.MOUSE_X2_HOLD: "x2",
}


def is_pc_only(action: ButtonAction) -> bool:
    """这个动作板子做不了，只能由 PC 执行。"""
    return (action.kind in APP_KINDS or action.kind in MOUSE_CLICK
            or action.kind in MOUSE_HOLD)


def run_pc_action(action: ButtonAction, phase: str = "click") -> bool:
    """
    执行一个 PC 专属动作。

    phase：
        "click"   —— 单击/双击这种「按下即完成」
        "press"   —— 长按类动作的按下（鼠标长按）
        "release" —— 长按类动作的松开
    """
    if action.kind in MOUSE_CLICK:
        button = MOUSE_CLICK[action.kind]
        return mouse_event(button, True) and mouse_event(button, False)

    if action.kind in MOUSE_HOLD:
        button = MOUSE_HOLD[action.kind]
        if phase == "press":
            return mouse_event(button, True)
        if phase == "release":
            return mouse_event(button, False)
        return True

    if action.kind in APP_KINDS:
        if phase == "release":
            return True          # 启动只做一次，别在松手时再来一遍
        if action.kind == ActionKind.OPEN_APP:
            # 用户自定义启动项：路径就是 uri
            if not action.uri:
                return False
            launch_command([action.uri])
            return True
        command = application_command(action.kind)
        if command is None:
            return False
        launch_command(command)
        return True

    return False
