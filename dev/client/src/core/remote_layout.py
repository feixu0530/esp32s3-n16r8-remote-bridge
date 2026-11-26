"""
遥控器键位档案 —— 谷歌 Chromecast 遥控器的固定键位清单。

照抄参考项目 windows-remote-mic-app 的
    apps/windows/rc003/src/ovb_rc003/remote_layout.py
（CHROMECAST_HOTSPOTS / CHROMECAST_DISPLAY_NAMES /
  CHROMECAST_CONSUMER_USAGES / CHROMECAST_REPORT_CODES）。

★ 为什么用「固定档案」而不是「让用户自己标注」★
    参考项目的按键页，行来自**设备档案里的固定键位清单**，
    不是来自用户标的热点标记 —— 打开就有完整的一列键可配。
    板子上报的 usage 已经是标准 HID consumer usage
    （固件 br_hid.c 的 k_chromecast_keys[] 做的翻译），
    和这里的 CHROMECAST_CONSUMER_USAGES 逐项一致，
    所以 PC 端可以直接用这张表算出 key_id。

★ key_id 的格式 ★
    固件的 key_id = (source << 12) | usage，
    source: 1=键盘 2=消费者 3=数据 4=厂商 5=ATVV。
    遥控器的普通按键都在**消费者页**，所以 source 恒为 2。
    PC 端内部用 "<source>:<usage十进制>" 的字符串形式
    （与旧的「标记学习」路径保持一致，controller._push_bindings_to_firmware
    会把它解析成整数再发给板子）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ★ 消费者页 source ★
BR_SRC_CONSUMER = 2

# ★ 键盘页 source（小米那台用这个）★
# 与固件 br_types.h 的 BR_SRC_KEYBOARD 一一对应 —— 两边必须一致。
BR_SRC_KEYBOARD = 1

# ★ 手机（移动端）source ★
# 与固件 br_types.h 的 BR_SRC_PHONE 一一对应 —— 两边必须一致。
BR_SRC_PHONE = 6

# 手机在映射表里的伪地址（手机没有 BLE 地址）
BR_PHONE_ADDR = "50:50:50:50:50:50"


@dataclass(frozen=True)
class ButtonHotspot:
    """
    实物图上的一个按键热点。

    坐标是**图片自身宽高的 0..1 归一化值**，
    这样换个分辨率、换个显示尺寸都对得上。
    """
    button_id: str
    x: float
    y: float
    width: float
    height: float
    is_voice: bool = False


# ------------------------------------------------------------------ 热点
# 照抄参考项目 remote_layout.py:106-123
# （配套图片：assets/remotes/Chromecast-remote-photo.png，
#   与参考项目 Resources/Chromecast-remote-photo.png 字节完全相同）
CHROMECAST_HOTSPOTS: Tuple[ButtonHotspot, ...] = (
    ButtonHotspot("up", .50, .060, .22, .045),
    ButtonHotspot("left", .20, .152, .17, .065),
    ButtonHotspot("ok", .50, .152, .28, .095),
    ButtonHotspot("right", .81, .152, .17, .065),
    ButtonHotspot("down", .50, .250, .22, .045),
    ButtonHotspot("back", .26, .355, .28, .090),
    ButtonHotspot("mic", .73, .355, .28, .090, is_voice=True),
    ButtonHotspot("home", .26, .483, .28, .090),
    ButtonHotspot("volume_mute", .73, .483, .28, .090),
    ButtonHotspot("youtube", .26, .612, .28, .090),
    ButtonHotspot("netflix", .73, .612, .28, .090),
    ButtonHotspot("power", .26, .733, .16, .052),
    ButtonHotspot("input_source", .73, .733, .16, .052),
    # 侧面音量摇杆：正面图只露出右边缘
    ButtonHotspot("volume_up", .98, .208, .04, .035),
    ButtonHotspot("volume_down", .98, .252, .04, .035),
)

CHROMECAST_HOTSPOTS_BY_ID: Dict[str, ButtonHotspot] = {
    hotspot.button_id: hotspot for hotspot in CHROMECAST_HOTSPOTS
}

# ------------------------------------------------------------------ 显示名
# 照抄参考项目 remote_layout.py:124-128
CHROMECAST_DISPLAY_NAMES: Dict[str, str] = {
    "up": "上键",
    "left": "左键",
    "ok": "确定键",
    "right": "右键",
    "down": "下键",
    "back": "返回键",
    "mic": "语音 / Assistant",
    "home": "主页键",
    "volume_mute": "静音键",
    "youtube": "YouTube",
    "netflix": "Netflix",
    "power": "电源键",
    "input_source": "输入源",
    "volume_up": "音量 + 键",
    "volume_down": "音量 − 键",
}

# ------------------------------------------------------------------ HID usage
# 照抄参考项目 remote_layout.py:138-144
# 与固件 br_hid.c 的 k_chromecast_keys[] 逐项一致。
CHROMECAST_CONSUMER_USAGES: Dict[str, int] = {
    "power": 0x019E, "input_source": 0x0189,
    "up": 0x0042, "down": 0x0043, "left": 0x0044, "right": 0x0045,
    "ok": 0x0041, "back": 0x0224, "home": 0x0223,
    "volume_up": 0x00E9, "volume_down": 0x00EA, "volume_mute": 0x00E2,
    "youtube": 0x0077, "netflix": 0x0078,
}

# 遥控器自己的按键码（诊断用；板子内部也有一张同样的表）
CHROMECAST_REPORT_CODES: Dict[str, int] = {
    "power": 0x01, "up": 0x03, "down": 0x04, "left": 0x05, "right": 0x06,
    "ok": 0x07, "volume_mute": 0x08, "home": 0x0A, "back": 0x0B,
    "volume_up": 0x0C, "volume_down": 0x0D, "youtube": 0x0E,
    "netflix": 0x0F, "input_source": 0x11,
}

# ------------------------------------------------------------------ 页面布局
# 照抄参考项目 ButtonsPage.qml:15（leftButtonIds）与 76-92（visualRow）
# 左卡片列：遥控器左边一列的键
LEFT_BUTTON_IDS: Tuple[str, ...] = (
    "up", "left", "down", "back", "home", "youtube", "power",
)
# 右卡片列：遥控器右边一列的键
RIGHT_BUTTON_IDS: Tuple[str, ...] = (
    "ok", "right", "volume_up", "volume_down",
    "mic", "volume_mute", "netflix", "input_source",
)

# 每个键在卡片列里的行号（0 起）
VISUAL_ROWS: Dict[str, int] = {
    "up": 0, "left": 1, "down": 2,
    "ok": 0, "right": 1, "volume_up": 2, "volume_down": 3,
    "back": 3, "mic": 4, "home": 4, "volume_mute": 5,
    "youtube": 5, "netflix": 6, "power": 6, "input_source": 7,
}

# 全部键的顺序（实物图热点顺序）
BUTTON_ORDER: Tuple[str, ...] = tuple(h.button_id for h in CHROMECAST_HOTSPOTS)

# 语音键：它不走 HID 报告，是 ATVV 控制通道上的
VOICE_BUTTON_ID = "mic"


def button_key_id(button_id: str) -> str:
    """
    键 → 内部 key_id 字符串（"<source>:<usage十进制>"）。

    没有 usage 的键（语音键）返回空串 —— 它不能配 HID 映射。
    """
    usage = CHROMECAST_CONSUMER_USAGES.get(button_id)
    if usage is None:
        return ""
    return f"{BR_SRC_CONSUMER}:{usage}"


def button_usage(button_id: str) -> Optional[int]:
    return CHROMECAST_CONSUMER_USAGES.get(button_id)


def display_name(button_id: str) -> str:
    return CHROMECAST_DISPLAY_NAMES.get(button_id, button_id)


# 键名短写（参考项目 ButtonsPage.qml:182-192 的 shortButtonName）
# 卡片左列窄，用短名才排得下。
CHROMECAST_SHORT_NAMES: Dict[str, str] = {
    "up": "上",
    "down": "下",
    "left": "左",
    "right": "右",
    "ok": "确定",
    "back": "返回",
    "home": "主页",
    "power": "电源",
    "volume_up": "音+",
    "volume_down": "音-",
    "volume_mute": "静音",
    "input_source": "输入",
    "youtube": "YouTube",
    "netflix": "Netflix",
    "mic": "语音",
}


def short_name(button_id: str) -> str:
    """键名短写。"""
    return CHROMECAST_SHORT_NAMES.get(button_id, display_name(button_id))


def usage_to_button_id() -> Dict[int, str]:
    """consumer usage → 键 id。

    ★ 给「人话日志」用 ★
      协议里报的是 usage 数值，得反查成键名（主页键…）用户才看得懂。
    """
    return {usage: bid for bid, usage in CHROMECAST_CONSUMER_USAGES.items()
            if usage is not None}


def hotspot_for(button_id: str) -> Optional[ButtonHotspot]:
    return CHROMECAST_HOTSPOTS_BY_ID.get(button_id)


def is_voice_button(button_id: str) -> bool:
    return button_id == VOICE_BUTTON_ID


def visual_row(button_id: str) -> int:
    """这个键在卡片列里的行号。"""
    return VISUAL_ROWS.get(button_id, 0)


# ------------------------------------------------------------------ 小米布局
#
# ★ 小米蓝牙语音遥控器一代（广播名 "MI RC" / "小米蓝牙语音遥控器"）★
#
#   实测（2026-11-24，证据见 dev/docs/xiaomi-gen1-capture.md）：
#   它的按键走**键盘页**（Usage Page 0x07），8 字节标准键盘报告：
#       byte0=修饰键  byte1=保留  byte2..7=键码
#   所以小米这台的 key_id 是 "1:<usage>"（source=键盘页），
#   谷歌那台是 "2:<usage>"（source=消费者页）—— 两个空间天然隔离。
#
#   实物图：src/assets/remotes/Xiaomi-remote.png
#   （由 reference/小米语音遥控器.jpg 裁剪而来，热点坐标按这张图标定）
#
#   遥控器上有：电源、语音、圆环（上/下/左/右 + 圆心确定）、
#   主页、返回、菜单、音量+、音量− —— 共 11 个可配键 + 1 个语音键。

XIAOMI_HOTSPOTS: Tuple[ButtonHotspot, ...] = (
    ButtonHotspot("power", .42, .045, .16, .045),
    ButtonHotspot("mic", .40, .115, .20, .05, is_voice=True),
    ButtonHotspot("up", .43, .165, .14, .05),
    ButtonHotspot("left", .10, .235, .14, .07),
    ButtonHotspot("ok", .36, .240, .28, .08),
    ButtonHotspot("right", .76, .235, .14, .07),
    ButtonHotspot("down", .43, .315, .14, .05),
    ButtonHotspot("home", .16, .425, .10, .04),
    ButtonHotspot("back", .445, .430, .10, .045),
    ButtonHotspot("menu", .755, .430, .08, .04),
    ButtonHotspot("volume_up", .45, .500, .10, .04),
    ButtonHotspot("volume_down", .45, .590, .10, .04),
)

XIAOMI_HOTSPOTS_BY_ID: Dict[str, ButtonHotspot] = {
    hotspot.button_id: hotspot for hotspot in XIAOMI_HOTSPOTS
}

XIAOMI_DISPLAY_NAMES: Dict[str, str] = {
    "power": "电源键",
    "mic": "语音键",
    "up": "上键",
    "down": "下键",
    "left": "左键",
    "right": "右键",
    "ok": "确定键",
    "home": "主页键",
    "back": "返回键",
    "menu": "菜单键",
    "volume_up": "音量 + 键",
    "volume_down": "音量 − 键",
}

XIAOMI_SHORT_NAMES: Dict[str, str] = {
    "power": "电源",
    "mic": "语音",
    "up": "上",
    "down": "下",
    "left": "左",
    "right": "右",
    "ok": "确定",
    "home": "主页",
    "back": "返回",
    "menu": "菜单",
    "volume_up": "音+",
    "volume_down": "音-",
}

# ★ HID usage（键盘页 0x07）★ 与固件 br_mapping.c 的 k_binds_xiaomi[] 一致
XIAOMI_USAGES: Dict[str, int] = {
    "power": 0x66,
    "up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4F,
    "ok": 0x28,
    "home": 0x4A,
    "back": 0xF1,
    "menu": 0x65,
    "volume_up": 0x80, "volume_down": 0x81,
}

XIAOMI_BUTTON_ORDER: Tuple[str, ...] = tuple(h.button_id for h in XIAOMI_HOTSPOTS)

# 两列都按实物从上到下排：电源在顶、音量在底，右侧语音在顶。
XIAOMI_LEFT_BUTTON_IDS: Tuple[str, ...] = (
    "power", "up", "left", "down", "home", "back", "menu",
)
XIAOMI_RIGHT_BUTTON_IDS: Tuple[str, ...] = (
    "mic", "ok", "right", "volume_up", "volume_down",
)

XIAOMI_VISUAL_ROWS: Dict[str, int] = {
    "power": 0, "up": 1, "left": 2, "down": 3,
    "home": 4, "back": 5, "menu": 6,
    "mic": 0, "ok": 1, "right": 2, "volume_up": 3, "volume_down": 4,
}

# 出厂默认动作（与固件 br_mapping.c 的 k_binds_xiaomi[] 一一对应）
XIAOMI_DEFAULT_ACTIONS: Dict[str, str] = {
    "power": "Escape",
    "up": "方向上", "down": "方向下",
    "left": "方向左", "right": "方向右",
    "ok": "回车",
    "home": "显示桌面",
    "back": "退格",
    "menu": "右键菜单",
    "volume_up": "系统音量 +", "volume_down": "系统音量 −",
}

XIAOMI_DEFAULT_LONG_ACTIONS: Dict[str, str] = {
    "back": "退格",
}


# ------------------------------------------------------------------ 内置默认
# 照抄参考项目 key_mapping.py:459 default_button_actions()，
# 再按 chromecast profile 补上 volume_mute（参考项目 remote_layout.py:157）。
# 值用的是**我们自己的中文动作名**（core/action_options.py 里的那套）。
DEFAULT_ACTIONS: Dict[str, str] = {
    # ★ 用户 2026-11-17 定下的一套默认 ★
    #   固件里 br_mapping.c 的 k_default_binds[] 是同一套（烧完就自带，
    #   插上板子不用 PC 也能用）。两边必须保持一致。
    "power": "Escape",
    "up": "方向上", "down": "方向下",
    "left": "方向左", "right": "方向右",
    "ok": "回车",
    "back": "退格",
    "volume_up": "系统音量 +", "volume_down": "系统音量 −",
    "volume_mute": "系统静音",
    "home": "显示桌面",
    "youtube": "应用切换",
    "netflix": "打开终端",       # 只在 PC 上做，不下发板子
    "input_source": "回车",
}

# 默认档案里的**长按**动作（键 id → 动作标签）。
#
# ★ 返回键长按 = 退格 ★
#   删除字符分两种习惯：按一下删一个，长按一直删到松手。
#   两种都要有，所以出厂默认里返回键既映射单击退格，也映射长按退格。
#   （固件 br_mapping.c 的 k_binds_chromecast[] 里是同一条：8740u trig=0 和 trig=2）
DEFAULT_LONG_ACTIONS: Dict[str, str] = {
    "back": "退格",
}


# ------------------------------------------------------------------ 默认档案
# ★ 按遥控器型号分档 ★
#
#   不同型号的遥控器键位不一样，默认映射也不该一样。
#   现在只有一档：谷歌遥控器。以后要支持别的遥控器，
#   就加一张 actions 表 + 在 DEFAULT_PROFILES 里加一行。
#
#   ★ 两边必须一致 ★
#     固件里 br_mapping.c 的 k_default_profiles[] 是同一套档案，
#     烧完就自带 —— 插上板子、遥控器配上对，按键立刻能用，不用开 PC。
#     tests/test_v020.py 会逐条比对两边，对不上就红。
DEFAULT_PROFILES: List[Dict[str, object]] = [
    {
        "id": "chromecast",
        "label": "谷歌遥控器",
        "name_match": ("chromecast", "media remote"),
        "actions": DEFAULT_ACTIONS,
        "long_actions": DEFAULT_LONG_ACTIONS,
    },
    {
        "id": "xiaomi",
        "label": "小米遥控器",
        "name_match": ("mi rc", "小米蓝牙语音遥控器", "xiaomi remote",
                       "xiaomi bluetooth remote"),
        "actions": XIAOMI_DEFAULT_ACTIONS,
        "long_actions": XIAOMI_DEFAULT_LONG_ACTIONS,
    },
]


def profile_for_name(name: str) -> Optional[Dict[str, object]]:
    """按遥控器名（广播名或自定义名）找默认档案。找不到返回 None。

    ★ 找不到就不给默认 ★
      给一台不认识的遥控器铺一整套谷歌键位，它按下去全是错的动作，
      比「什么都不配」更让人困惑。固件那边也是同一个态度。
    """
    if not name:
        return None
    low = name.lower()
    for prof in DEFAULT_PROFILES:
        for token in prof["name_match"]:      # type: ignore[index]
            if str(token).lower() in low:
                return prof
    return None


# ------------------------------------------------------------------ 手机布局
#
# 手机（movo 触控板页的快捷键区）没有实物遥控器上的「上/下/左/右」，
# 只有几个语义键。固件 br_types.h 的 BR_PHU_* 就是它们的 usage。
#
# ★ 键位空间必须与谷歌遥控器隔离 ★
#   手机  key_id = "6:<usage>"（source=BR_SRC_PHONE）
#   遥控器 key_id = "2:<usage>"（source=BR_SRC_CONSUMER）
#   前缀不同 ⇒ 同一台 PC 上两套映射物理上不可能互相覆盖。

PHONE_BUTTONS: Tuple[Tuple[str, int, str], ...] = (
    ("esc",         0x01, "退出"),
    ("play_pause",  0x05, "播放 / 暂停"),
    ("volume_up",   0x02, "音量+"),
    ("volume_down", 0x03, "音量-"),
    ("mute",        0x04, "静音"),
)

PHONE_BUTTON_ORDER: Tuple[str, ...] = tuple(b[0] for b in PHONE_BUTTONS)
PHONE_USAGES: Dict[str, int] = {b[0]: b[1] for b in PHONE_BUTTONS}
PHONE_DISPLAY_NAMES: Dict[str, str] = {b[0]: b[2] for b in PHONE_BUTTONS}

PHONE_SHORT_NAMES: Dict[str, str] = {
    "esc": "退出",
    "play_pause": "播放",
    "volume_up": "音+",
    "volume_down": "音-",
    "mute": "静音",
}

# 手机没有实物图 ⇒ 没有热点；按键卡片按两列排
PHONE_VISUAL_ROWS: Dict[str, int] = {
    "esc": 0, "play_pause": 1,
    "volume_up": 0, "volume_down": 1, "mute": 2,
}

PHONE_RIGHT_BUTTON_IDS: Tuple[str, ...] = ("volume_up", "volume_down", "mute")

# 出厂默认动作：与固件 br_mapping.c 的 k_binds_phone[] 对齐
# 旧版「底部快捷键」的 usage：固件里仍保留（6:6），但手机端已改为打开快捷键页。
PHONE_LEGACY_SHORTCUT_USAGE = 0x06

# ---- 手机「自定义快捷键页」----
# usage = CUSTOM_KEY_USAGE_BASE + id（id 1..12），与固件 br_types.h 一一对应。
CUSTOM_KEY_MAX = 64          # 映射表 64 条，扣掉 5 条固定键后仍够用
CUSTOM_KEY_USAGE_BASE = 0x100
CUSTOM_COLS = 3
CUSTOM_ROWS = 64             # 行上限（滚屏展示）
# 手机截图里触点区的归一化范围（配合 phone-shot.jpg）
CUSTOM_AREA = {"x0": 0.06, "x1": 0.94, "y0": 0.30, "y1": 0.86}


def custom_button_id(kid: int) -> str:
    return "ck%d" % kid


def custom_key_id(kid: int) -> str:
    return "%d:%d" % (BR_SRC_PHONE, CUSTOM_KEY_USAGE_BASE + kid)


def custom_id_of_usage(usage: int) -> int:
    """usage 落在自定义键区间就返回 id，否则 0。"""
    kid = usage - CUSTOM_KEY_USAGE_BASE
    return kid if 1 <= kid <= CUSTOM_KEY_MAX else 0


PHONE_DEFAULT_ACTIONS: Dict[str, str] = {
    "esc": "Escape",
    "play_pause": "播放 / 暂停",
    "volume_up": "系统音量 +",
    "volume_down": "系统音量 −",
    "mute": "系统静音",
}

# 所有布局的种类
LAYOUT_CHROMECAST = "chromecast"
LAYOUT_PHONE = "phone"
LAYOUT_XIAOMI = "xiaomi"
LAYOUT_KINDS: Tuple[str, ...] = (LAYOUT_CHROMECAST, LAYOUT_PHONE, LAYOUT_XIAOMI)


def layout_kind_for_remote(remote: object) -> str:
    """一个遥控器条目属于哪套布局。

    配置里没写 kind 的一律当谷歌遥控器 —— 老的配置不能因为加了新类型就失效。
    """
    if isinstance(remote, dict):
        kind = str(remote.get("kind", ""))
        if kind == LAYOUT_PHONE:
            return LAYOUT_PHONE
        if kind == LAYOUT_XIAOMI:
            return LAYOUT_XIAOMI
        if kind == LAYOUT_CHROMECAST:
            return LAYOUT_CHROMECAST
        name = str(remote.get("name", ""))
        if name:
            prof = profile_for_name(name)
            if prof and prof.get("id") == LAYOUT_XIAOMI:
                return LAYOUT_XIAOMI
    return LAYOUT_CHROMECAST


def button_order_for(kind: str) -> Tuple[str, ...]:
    if kind == LAYOUT_PHONE:
        return PHONE_BUTTON_ORDER
    if kind == LAYOUT_XIAOMI:
        return XIAOMI_BUTTON_ORDER
    return BUTTON_ORDER


def display_name_for(kind: str, button_id: str) -> str:
    if kind == LAYOUT_PHONE:
        return PHONE_DISPLAY_NAMES.get(button_id, button_id)
    if kind == LAYOUT_XIAOMI:
        return XIAOMI_DISPLAY_NAMES.get(button_id, button_id)
    return display_name(button_id)


def short_name_for(kind: str, button_id: str) -> str:
    if kind == LAYOUT_PHONE:
        return PHONE_SHORT_NAMES.get(button_id, display_name_for(kind, button_id))
    if kind == LAYOUT_XIAOMI:
        return XIAOMI_SHORT_NAMES.get(button_id, display_name_for(kind, button_id))
    return short_name(button_id)


def button_key_id_for(kind: str, button_id: str) -> str:
    """键 → 内部 key_id 字符串（"<source>:<usage十进制>"）。"""
    if kind == LAYOUT_PHONE:
        if button_id.startswith("ck"):
            try:
                kid = int(button_id[2:])
            except ValueError:
                return ""
            return custom_key_id(kid) if 1 <= kid <= CUSTOM_KEY_MAX else ""
        usage = PHONE_USAGES.get(button_id)
        if usage is None:
            return ""
        return f"{BR_SRC_PHONE}:{usage}"
    if kind == LAYOUT_XIAOMI:
        usage = XIAOMI_USAGES.get(button_id)
        if usage is None:
            return ""
        return f"{BR_SRC_KEYBOARD}:{usage}"
    return button_key_id(button_id)


def is_voice_button_for(kind: str, button_id: str) -> bool:
    # 手机没有语音键（语音走按住说话的通道，不是快捷键）
    return False if kind == LAYOUT_PHONE else is_voice_button(button_id)


def visual_row_for(kind: str, button_id: str) -> int:
    if kind == LAYOUT_PHONE:
        return PHONE_VISUAL_ROWS.get(button_id, 0)
    if kind == LAYOUT_XIAOMI:
        return XIAOMI_VISUAL_ROWS.get(button_id, 0)
    return visual_row(button_id)


def right_button_ids_for(kind: str) -> Tuple[str, ...]:
    if kind == LAYOUT_PHONE:
        return PHONE_RIGHT_BUTTON_IDS
    if kind == LAYOUT_XIAOMI:
        return XIAOMI_RIGHT_BUTTON_IDS
    return RIGHT_BUTTON_IDS


def usage_to_button_id_for(kind: str) -> Dict[int, str]:
    """usage → 键 id（给人话日志用）。"""
    if kind == LAYOUT_PHONE:
        return {usage: bid for bid, usage in PHONE_USAGES.items()}
    if kind == LAYOUT_XIAOMI:
        return {usage: bid for bid, usage in XIAOMI_USAGES.items()
                if usage is not None}
    return usage_to_button_id()


def default_actions_for(kind: str) -> Dict[str, str]:
    if kind == LAYOUT_PHONE:
        return dict(PHONE_DEFAULT_ACTIONS)
    if kind == LAYOUT_XIAOMI:
        return dict(XIAOMI_DEFAULT_ACTIONS)
    return DEFAULT_ACTIONS


def default_long_actions_for(kind: str) -> Dict[str, str]:
    if kind == LAYOUT_PHONE:
        return {}
    if kind == LAYOUT_XIAOMI:
        return dict(XIAOMI_DEFAULT_LONG_ACTIONS)
    return DEFAULT_LONG_ACTIONS


def hotspots_for(kind: str) -> Tuple[ButtonHotspot, ...]:
    """实物图上的热点表。"""
    if kind == LAYOUT_PHONE:
        return ()
    if kind == LAYOUT_XIAOMI:
        return XIAOMI_HOTSPOTS
    return CHROMECAST_HOTSPOTS

