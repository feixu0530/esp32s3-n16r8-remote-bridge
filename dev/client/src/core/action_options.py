"""
动作下拉的分组清单。

抄 windows-remote-mic-app 的 settings_ui.py（ACTION_OPTION_GROUPS），
按本项目的 ActionKind 集合调整。

要点：
    - 下拉里是**语义动作名**（"显示桌面"、"鼠标左键单击"…）
    - 自定义组合键不在这份清单里，用户直接在输入框写 `ctrl+shift+p`
    - 「启动应用」组会**按本机是否真的装了对应的 exe** 过滤
      —— 列一个装不上的应用没有意义
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, Tuple

from .key_mapping import ActionKind

# 语义动作 -> 显示名
ACTION_LABELS: Dict[ActionKind, str] = {
    ActionKind.DISABLED: "禁用",
    ActionKind.ESCAPE: "Escape",
    ActionKind.RETURN: "回车",
    ActionKind.ARROW_UP: "方向上",
    ActionKind.ARROW_DOWN: "方向下",
    ActionKind.ARROW_LEFT: "方向左",
    ActionKind.ARROW_RIGHT: "方向右",
    ActionKind.DELETE_BACKWARD: "退格",
    ActionKind.SHOW_DESKTOP: "显示桌面",
    ActionKind.CONTEXT_MENU: "右键菜单",
    ActionKind.APP_SWITCHER: "应用切换",
    ActionKind.SYSTEM_VOLUME_UP: "系统音量 +",
    ActionKind.SYSTEM_VOLUME_DOWN: "系统音量 −",
    ActionKind.SYSTEM_VOLUME_MUTE: "系统静音",
    ActionKind.PLAY_PAUSE: "播放 / 暂停",
    ActionKind.MOUSE_LEFT_CLICK: "鼠标左键单击",
    ActionKind.MOUSE_RIGHT_CLICK: "鼠标右键单击",
    ActionKind.MOUSE_MIDDLE_CLICK: "鼠标中键单击",
    ActionKind.MOUSE_X1_CLICK: "鼠标 X1 单击",
    ActionKind.MOUSE_X2_CLICK: "鼠标 X2 单击",
    ActionKind.MOUSE_LEFT_HOLD: "鼠标左键长按",
    ActionKind.MOUSE_RIGHT_HOLD: "鼠标右键长按",
    ActionKind.MOUSE_X1_HOLD: "鼠标 X1 长按",
    ActionKind.MOUSE_X2_HOLD: "鼠标 X2 长按",
    ActionKind.ELEMENT_NAVIGATION_TOGGLE: "元素导航开关",
    ActionKind.VOICE_HOLD: "按住说话",
    ActionKind.OPEN_APP: "打开应用…",
    ActionKind.OPEN_CODEX: "打开 Codex",
    ActionKind.OPEN_CLAUDE: "打开 Claude",
    ActionKind.OPEN_CURSOR: "打开 Cursor",
    ActionKind.OPEN_TERMINAL: "打开终端",
    ActionKind.OPEN_WECHAT: "打开微信",
    ActionKind.OPEN_WEWORK: "打开企业微信",
    ActionKind.OPEN_NETEASE: "打开网易云音乐",
    ActionKind.OPEN_SLACK: "打开 Slack",
    ActionKind.OPEN_CHROME: "打开 Chrome",
    ActionKind.OPEN_EDGE: "打开 Edge",
    ActionKind.OPEN_ZED: "打开 Zed",
}

LABEL_TO_KIND: Dict[str, ActionKind] = {
    label: kind for kind, label in ACTION_LABELS.items()
}

# 旧版本的别名，仍然接受
LEGACY_LABEL_TO_KIND: Dict[str, ActionKind] = {
    "Return": ActionKind.RETURN,
    "Delete（退格）": ActionKind.DELETE_BACKWARD,
    "上下文菜单": ActionKind.CONTEXT_MENU,
    "打开 cmux": ActionKind.OPEN_TERMINAL,
    "打开 cmux 终端": ActionKind.OPEN_TERMINAL,
}


# --------------------------------------------------------------- 启动应用组

# 动作 -> (可执行文件名, 常见安装目录相对路径)
_APPLICATIONS = {
    ActionKind.OPEN_CODEX: ("codex.exe", ["Codex", ".codex", "Programs/Codex"]),
    ActionKind.OPEN_CLAUDE: ("claude.exe", ["Claude", ".claude", "Programs/Claude"]),
    ActionKind.OPEN_CURSOR: ("Cursor.exe", ["Programs/Cursor", "Cursor"]),
    ActionKind.OPEN_TERMINAL: ("wt.exe", ["Microsoft/WindowsApps"]),
    ActionKind.OPEN_WECHAT: ("WeChat.exe", ["Tencent/WeChat", "WeChat"]),
    ActionKind.OPEN_WEWORK: ("WXWork.exe", ["Tencent/WXWork", "Programs/WXWork",
                                            "Tencent/WeCom"]),
    ActionKind.OPEN_NETEASE: ("cloudmusic.exe", ["NetEase/CloudMusic",
                                                 "Programs/NeteaseCloudMusic",
                                                 "Netease"]),
    ActionKind.OPEN_SLACK: ("slack.exe", ["Programs/Slack", "Slack"]),
    ActionKind.OPEN_CHROME: ("chrome.exe", ["Google/Chrome/Application"]),
    ActionKind.OPEN_EDGE: ("msedge.exe", ["Microsoft/Edge/Application"]),
    ActionKind.OPEN_ZED: ("Zed.exe", ["Programs/Zed", ".local/bin"]),
}


def _app_dirs() -> list[Path]:
    home = Path(os.path.expanduser("~"))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        program = os.environ.get("PROGRAMFILES", "")
        program_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        dirs = [home]
        for base in (local, roaming, program, program_x86):
            if base:
                dirs.append(Path(base))
        return dirs
    return [home / ".local" / "bin", home / "Applications", home / ".local" / "share"]


def application_installed(kind: ActionKind) -> bool:
    """这个应用在本机装了吗？"""
    spec = _APPLICATIONS.get(kind)
    if spec is None:
        return False
    exe, relatives = spec
    if shutil.which(exe.replace(".exe", "")):
        return True
    for base in _app_dirs():
        for rel in relatives:
            candidate = base / rel / exe
            if candidate.exists():
                return True
            # macOS / Linux 上没有 .exe 后缀
            candidate = candidate.with_suffix("")
            if candidate.exists():
                return True
    return False


def application_command(kind: ActionKind) -> list[str] | None:
    """
    启动这个应用要执行的命令；本机找不到返回 None。

    ★ 「下拉里有、按了没反应」的根因就是没这一步 ★
      以前只判断了装没装，从来没真正启动过。
    """
    spec = _APPLICATIONS.get(kind)
    if spec is None:
        return None
    exe, relatives = spec
    stem = exe[:-4] if exe.lower().endswith(".exe") else exe
    found = shutil.which(exe) or shutil.which(stem)
    if found:
        return [found]
    for base in _app_dirs():
        for rel in relatives:
            for name in (exe, stem):
                candidate = base / rel / name
                if candidate.is_file():
                    return [str(candidate)]
    return None


def _app_group() -> tuple[str, ...]:
    labels = []
    for kind in _APPLICATIONS:
        if application_installed(kind):
            labels.append(ACTION_LABELS[kind])
    return tuple(labels)


# ------------------------------------------------------------------- 分组

def action_option_groups(custom_labels: tuple[str, ...] = ()
                         ) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """
    返回 [(组名, (选项...)), ...]，供 QML 的分组下拉使用。

    custom_labels：用户自己加的启动项（「启动应用」组末尾），数量不限。
    """
    key_ops = ("Escape", "回车", "退格", "方向上", "方向下", "方向左", "方向右",
               "tab", "space", "f5")
    mouse_ops = ("元素导航开关", "鼠标左键单击", "鼠标右键单击", "鼠标中键单击",
                 "鼠标 X1 单击", "鼠标 X2 单击", "右键菜单")
    system_ops = ("显示桌面", "应用切换", "系统音量 +", "系统音量 −",
                  "系统静音", "播放 / 暂停")
    apps = _app_group() + tuple(custom_labels)
    others = ("禁用",)

    groups: list[tuple[str, tuple[str, ...]]] = [
        ("按键操作", key_ops),
        ("鼠标操作", mouse_ops),
        ("系统与媒体", system_ops),
    ]
    # ★ 「语音 / 按住说话」已从下拉移除 ★
    #   语音键改到「语音」页单独配置，这里再放一份只会造成两处配置打架。
    if apps:
        groups.append(("启动应用", apps))
    groups.append(("其他", others))
    return tuple(groups)


def all_option_labels() -> tuple[str, ...]:
    return tuple(
        opt for _title, opts in action_option_groups() for opt in opts)


def label_to_kind(label: str) -> ActionKind | None:
    """显示名 -> ActionKind；认不出来返回 None（当作自定义组合键）。"""
    label = (label or "").strip()
    if label in LABEL_TO_KIND:
        return LABEL_TO_KIND[label]
    if label in LEGACY_LABEL_TO_KIND:
        return LEGACY_LABEL_TO_KIND[label]
    return None


def kind_to_label(kind: ActionKind) -> str:
    return ACTION_LABELS.get(kind, kind.value)
