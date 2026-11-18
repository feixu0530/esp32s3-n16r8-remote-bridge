"""
按键映射的数据模型。

抄 windows-remote-mic-app 的 key_mapping.py，保留它的核心设计：

    - ActionKind（动作类型）+ ButtonAction（动作实例）
    - from_dict **fail-closed**：任何不合法的数据一律降级成 DISABLED，
      绝不"猜"出一个动作
    - 三段手势（单击/双击/长按）分开存
    - REPEATABLE_ACTIONS 白名单：只有方向键/退格/音量± 允许按住连发

按本项目的需要加了一条（方案 D2）：

    - **KeyCombo** 类动作可以下发到固件，PC 不在场时也能用
    - **Semantic** 类动作（显示桌面/鼠标/启动应用/按住说话）只能 PC 执行

数据落点：
    bindings          {button_id: action_dict}                     单击
    secondary_bindings{button_id: {trigger: action_dict}}          双击/长按
    display_notes     {button_id: {trigger: note_text}}             备注
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


# --------------------------------------------------------------- 动作类型


class ActionKind(str, Enum):
    DISABLED = "disabled"
    KEY_COMBO = "key_combo"

    # 键盘语义
    ESCAPE = "escape"
    RETURN = "return"
    ARROW_UP = "arrow_up"
    ARROW_DOWN = "arrow_down"
    ARROW_LEFT = "arrow_left"
    ARROW_RIGHT = "arrow_right"
    DELETE_BACKWARD = "delete_backward"

    # 系统语义
    SHOW_DESKTOP = "show_desktop"
    CONTEXT_MENU = "context_menu"
    APP_SWITCHER = "app_switcher"
    SYSTEM_VOLUME_UP = "system_volume_up"
    SYSTEM_VOLUME_DOWN = "system_volume_down"
    SYSTEM_VOLUME_MUTE = "system_volume_mute"
    PLAY_PAUSE = "play_pause"

    # 鼠标
    MOUSE_LEFT_CLICK = "mouse_left_click"
    MOUSE_RIGHT_CLICK = "mouse_right_click"
    MOUSE_MIDDLE_CLICK = "mouse_middle_click"
    MOUSE_X1_CLICK = "mouse_x1_click"
    MOUSE_X2_CLICK = "mouse_x2_click"
    MOUSE_LEFT_HOLD = "mouse_left_hold"
    MOUSE_RIGHT_HOLD = "mouse_right_hold"
    MOUSE_X1_HOLD = "mouse_x1_hold"
    MOUSE_X2_HOLD = "mouse_x2_hold"

    ELEMENT_NAVIGATION_TOGGLE = "element_navigation_toggle"

    # 语音：只保留「按住说话」
    VOICE_HOLD = "voice_hold"

    # 启动应用
    OPEN_APP = "open_app"
    OPEN_CODEX = "open_codex"
    OPEN_CLAUDE = "open_claude"
    OPEN_CURSOR = "open_cursor"
    OPEN_TERMINAL = "open_terminal"
    OPEN_WECHAT = "open_wechat"
    OPEN_WEWORK = "open_wework"
    OPEN_NETEASE = "open_netease"
    OPEN_SLACK = "open_slack"
    OPEN_CHROME = "open_chrome"
    OPEN_EDGE = "open_edge"
    OPEN_ZED = "open_zed"


MOUSE_HOLD_BUTTONS = {
    ActionKind.MOUSE_LEFT_HOLD: "left",
    ActionKind.MOUSE_RIGHT_HOLD: "right",
    ActionKind.MOUSE_X1_HOLD: "x1",
    ActionKind.MOUSE_X2_HOLD: "x2",
}

# 语义动作 → 实际键序列（PC 侧执行）
SEMANTIC_KEY_TOKENS: Dict[ActionKind, Tuple[str, ...]] = {
    ActionKind.ESCAPE: ("escape",),
    ActionKind.RETURN: ("enter",),
    ActionKind.ARROW_UP: ("up",),
    ActionKind.ARROW_DOWN: ("down",),
    ActionKind.ARROW_LEFT: ("left",),
    ActionKind.ARROW_RIGHT: ("right",),
    ActionKind.DELETE_BACKWARD: ("backspace",),
    ActionKind.SHOW_DESKTOP: ("win", "d"),
    ActionKind.CONTEXT_MENU: ("shift", "f10"),
    ActionKind.APP_SWITCHER: ("alt", "tab"),
    ActionKind.SYSTEM_VOLUME_UP: ("volume_up",),
    ActionKind.SYSTEM_VOLUME_DOWN: ("volume_down",),
    ActionKind.SYSTEM_VOLUME_MUTE: ("volume_mute",),
    ActionKind.PLAY_PAUSE: ("media_play_pause",),
}

# 消费者页的 usage。
# ★ 和键盘页的修饰键撞号 ★
#   键盘页 0xE0-0xE7 是 ctrl/shift/alt/gui，
#   消费者页 0x00E2 却是「静音」、0x00E9 是「音量 +」。
#   同一个数值在不同页含义完全不同，所以**必须按 token 区分**，
#   不能只看 usage 数值。
CONSUMER_TOKENS: Dict[str, int] = {
    "volume_up": 0x00E9,
    "volume_down": 0x00EA,
    "volume_mute": 0x00E2,
    "media_play_pause": 0x00CD,
}

APPLICATION_ACTIONS = frozenset({
    ActionKind.OPEN_APP, ActionKind.OPEN_CODEX, ActionKind.OPEN_CLAUDE,
    ActionKind.OPEN_CURSOR, ActionKind.OPEN_TERMINAL, ActionKind.OPEN_WECHAT,
    ActionKind.OPEN_WEWORK, ActionKind.OPEN_NETEASE,
    ActionKind.OPEN_SLACK, ActionKind.OPEN_CHROME, ActionKind.OPEN_EDGE,
    ActionKind.OPEN_ZED,
})

REPEATABLE_ACTIONS = frozenset({
    ActionKind.ARROW_UP, ActionKind.ARROW_DOWN,
    ActionKind.ARROW_LEFT, ActionKind.ARROW_RIGHT,
    ActionKind.DELETE_BACKWARD,
    ActionKind.SYSTEM_VOLUME_UP, ActionKind.SYSTEM_VOLUME_DOWN,
})

def firmware_tokens(action: "ButtonAction") -> Tuple[str, ...]:
    """
    这个动作在**开发板**上怎么执行 —— 返回 HID 键 token。

    语义动作（显示桌面 / 方向上 / 音量 +…）在 PC 上是「特殊动作」，
    但在板子上它就是**一串按键** —— 板子只认识键序列。
    所以这里把语义动作还原成键序列，它才能下发到板子、脱离 PC 也生效。
    """
    if action.kind == ActionKind.KEY_COMBO:
        return action.keys
    return SEMANTIC_KEY_TOKENS.get(action.kind, ())


# 旧版本的键元组 → 语义动作的迁移表（读旧配置时用）
LEGACY_SEMANTIC_ACTIONS = {
    ("escape",): ActionKind.ESCAPE,
    ("enter",): ActionKind.RETURN,
    ("up",): ActionKind.ARROW_UP,
    ("down",): ActionKind.ARROW_DOWN,
    ("left",): ActionKind.ARROW_LEFT,
    ("right",): ActionKind.ARROW_RIGHT,
    ("backspace",): ActionKind.DELETE_BACKWARD,
    ("win", "d"): ActionKind.SHOW_DESKTOP,
    ("shift", "f10"): ActionKind.CONTEXT_MENU,
    ("alt", "tab"): ActionKind.APP_SWITCHER,
}


class ButtonTrigger(str, Enum):
    SINGLE_CLICK = "single_click"
    DOUBLE_CLICK = "double_click"
    LONG_PRESS = "long_press"


# 触发类型的数值编码（与固件 BR_TRIG_* 对齐）
TRIGGER_INDEX = {
    ButtonTrigger.SINGLE_CLICK: 0,
    ButtonTrigger.DOUBLE_CLICK: 1,
    ButtonTrigger.LONG_PRESS: 2,
}
INDEX_TRIGGER = {v: k for k, v in TRIGGER_INDEX.items()}


# --------------------------------------------------------------- 动作实例


@dataclass(frozen=True)
class ButtonAction:
    kind: ActionKind
    keys: Tuple[str, ...] = field(default_factory=tuple)
    uri: str = ""          # OPEN_APP 用的路径/URI

    def to_dict(self) -> dict:
        data: dict = {"kind": self.kind.value, "keys": list(self.keys)}
        if self.kind == ActionKind.OPEN_APP:
            data["uri"] = self.uri
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ButtonAction":
        """
        严格解析。**任何不合法都抛异常**，由调用方降级成 DISABLED。
        """
        if not isinstance(data, dict):
            raise TypeError("button action must be a mapping")
        kind = ActionKind(data["kind"])          # 未知 kind -> KeyError
        raw_keys = data.get("keys", ())
        if not isinstance(raw_keys, (list, tuple)) or not all(
            isinstance(k, str) and k.strip() for k in raw_keys
        ):
            raise ValueError("button action keys must be non-empty strings")
        keys = tuple(k.strip().lower() for k in raw_keys)

        raw_uri = data.get("uri", "")
        if not isinstance(raw_uri, str):
            raise ValueError("button action URI must be text")
        uri = raw_uri.strip()

        if kind == ActionKind.KEY_COMBO and not keys:
            raise ValueError("key_combo action must contain at least one key")
        if kind != ActionKind.KEY_COMBO and keys:
            raise ValueError("non-key action must not contain keys")
        if kind == ActionKind.OPEN_APP and not uri:
            raise ValueError("open_app action must contain a uri")
        if kind != ActionKind.OPEN_APP and uri:
            raise ValueError("non-uri action must not contain a uri")
        return cls(kind=kind, keys=keys, uri=uri)

    # -- 能否下发到固件（PC 不在场时也能用）--
    @property
    def firmware_capable(self) -> bool:
        """
        能不能下发到固件（PC 不在场时也能用）。

        ★ 不只是 KeyCombo ★
          语义动作只要能还原成**纯键序列**，板子就能做 ——
          「显示桌面」就是 win+d，「方向上」就是一个方向键。
          真正做不了的是鼠标动作和「启动应用」（那些要 PC 参与）。
        """
        return bool(firmware_tokens(self))

    @property
    def is_voice(self) -> bool:
        return self.kind == ActionKind.VOICE_HOLD

    @property
    def repeatable(self) -> bool:
        return self.kind in REPEATABLE_ACTIONS


DISABLED_ACTION = ButtonAction(ActionKind.DISABLED)


# --------------------------------------------------------------- 读映射


def button_action_for(bindings: dict, button_id: str,
                      trigger: ButtonTrigger = ButtonTrigger.SINGLE_CLICK
                      ) -> ButtonAction:
    """
    从版本化的绑定文档里读某一段手势的动作。

    读不出来一律返回 DISABLED（fail-closed），绝不猜。
    """
    if not isinstance(bindings, dict):
        return DISABLED_ACTION

    if trigger == ButtonTrigger.SINGLE_CLICK:
        raw = bindings.get("bindings", {})
        raw = raw.get(button_id) if isinstance(raw, dict) else None
    else:
        secondary = bindings.get("secondary_bindings", {})
        entry = secondary.get(button_id) if isinstance(secondary, dict) else None
        raw = entry.get(trigger.value) if isinstance(entry, dict) else None

    if not isinstance(raw, dict):
        return DISABLED_ACTION

    try:
        action = ButtonAction.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return DISABLED_ACTION

    # 鼠标长按类动作只允许出现在长按
    if action.kind in MOUSE_HOLD_BUTTONS and trigger != ButtonTrigger.LONG_PRESS:
        return DISABLED_ACTION

    # 语音键：双击/长按一律禁用
    # 判据是**主键动作**是不是语音 —— 不是这一段动作本身。
    # （语音遥控器只有一个麦克风键，双击/长按没有语义）
    if trigger != ButtonTrigger.SINGLE_CLICK:
        primary = button_action_for(bindings, button_id,
                                    ButtonTrigger.SINGLE_CLICK)
        if primary.is_voice:
            return DISABLED_ACTION

    return action


def has_secondary_action(bindings: dict, button_id: str) -> bool:
    return any(
        button_action_for(bindings, button_id, t).kind != ActionKind.DISABLED
        for t in (ButtonTrigger.DOUBLE_CLICK, ButtonTrigger.LONG_PRESS)
    )


def display_note(bindings: dict, button_id: str,
                 trigger: ButtonTrigger = ButtonTrigger.SINGLE_CLICK) -> str:
    notes = bindings.get("display_notes", {})
    if not isinstance(notes, dict):
        return ""
    entry = notes.get(button_id)
    if not isinstance(entry, dict):
        return ""
    value = entry.get(trigger.value, "")
    return value if isinstance(value, str) else ""


def set_button_action(bindings: dict, button_id: str,
                      trigger: ButtonTrigger, action: ButtonAction) -> dict:
    """写一条映射（原地更新并返回）。"""
    out = dict(bindings)
    if trigger == ButtonTrigger.SINGLE_CLICK:
        table = dict(out.get("bindings", {}))
        table[button_id] = action.to_dict()
        out["bindings"] = table
    else:
        table = dict(out.get("secondary_bindings", {}))
        entry = dict(table.get(button_id, {}))
        entry[trigger.value] = action.to_dict()
        table[button_id] = entry
        out["secondary_bindings"] = table
    return out


def clear_button_action(bindings: dict, button_id: str,
                        trigger: ButtonTrigger) -> dict:
    out = dict(bindings)
    if trigger == ButtonTrigger.SINGLE_CLICK:
        table = dict(out.get("bindings", {}))
        table.pop(button_id, None)
        out["bindings"] = table
    else:
        table = dict(out.get("secondary_bindings", {}))
        entry = dict(table.get(button_id, {}))
        entry.pop(trigger.value, None)
        if entry:
            table[button_id] = entry
        else:
            table.pop(button_id, None)
        out["secondary_bindings"] = table
    return out


def set_display_note(bindings: dict, button_id: str,
                     trigger: ButtonTrigger, note: str) -> dict:
    out = dict(bindings)
    table = dict(out.get("display_notes", {}))
    entry = dict(table.get(button_id, {}))
    if note.strip():
        entry[trigger.value] = note.strip()
    else:
        entry.pop(trigger.value, None)
    if entry:
        table[button_id] = entry
    else:
        table.pop(button_id, None)
    out["display_notes"] = table
    return out


def firmware_bindings(bindings: dict) -> list[tuple[str, int, tuple[str, ...]]]:
    """
    抽出能下发到固件的那部分映射。

    返回 [(button_id, trigger_index, keys), ...]
    """
    out: list[tuple[str, int, tuple[str, ...]]] = []
    for trigger in ButtonTrigger:
        table = bindings.get("bindings" if trigger == ButtonTrigger.SINGLE_CLICK
                             else "secondary_bindings", {})
        if not isinstance(table, dict):
            continue
        for button_id in table:
            action = button_action_for(bindings, button_id, trigger)
            tokens = firmware_tokens(action)
            if action.firmware_capable and tokens:
                out.append((button_id, TRIGGER_INDEX[trigger], tokens))
    return out
