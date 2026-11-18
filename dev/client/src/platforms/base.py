"""
平台抽象层 —— 把 Windows / macOS 的差异关在接口后面。

设计原则（来自 macOS 移植的教训）：
    业务代码只依赖这里的 ABC，永远不直接 import win32api / Quartz。
    新增平台 = 新增一个 module 实现这些接口 + 在 build.py 里注册。

能力清单：
    SerialPort      串口枚举 / 打开 / 读写
    HotkeyRecorder  全局键盘钩子录制（不干扰前台）
    TextInjector   往当前输入框打字（剪贴板 + 粘贴）
    AudioPlayer     把 PCM 播到指定输出端点（虚拟麦克风）
    StartupManager  开机自启
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


# --------------------------------------------------------------------- 串口


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    description: str = ""
    vid: int = 0
    pid: int = 0

    @property
    def label(self) -> str:
        if self.vid:
            return f"{self.device} — {self.description} (VID {self.vid:04X} PID {self.pid:04X})"
        return f"{self.device} — {self.description}"


class SerialPort(abc.ABC):
    """串口通道（USB CDC）。"""

    @abc.abstractmethod
    def list_ports(self) -> list[SerialPortInfo]:
        ...

    @abc.abstractmethod
    def open(self, device: str, baud: int = 115200) -> None:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...

    @abc.abstractmethod
    def read(self, size: int = 4096) -> bytes:
        ...

    @abc.abstractmethod
    def write(self, data: bytes) -> int:
        ...

    @abc.abstractmethod
    def set_dtr(self, on: bool) -> None:
        ...

    @property
    def is_open(self) -> bool:
        return False


# ----------------------------------------------------------------- 键盘录制


@dataclass(frozen=True)
class HotkeyCapture:
    """一次录制的结果。"""

    vks: tuple[int, ...]          # 按下顺序的虚拟键码
    tokens: tuple[str, ...]       # 归一化的键名（lctrl / win / f5 ...）
    out_type: int                 # 0=键盘 1=消费者
    hid_keys: tuple[int, ...]     # 归一化的 HID usage


class HotkeyRecorder(abc.ABC):
    """
    全局键盘录制。

    实现要求：
        - 保留首次按下顺序
        - 区分左右修饰键（lctrl / rctrl）
        - **吞掉被录事件**，不影响前台程序
    """

    @abc.abstractmethod
    def start(self, on_capture: Callable[[HotkeyCapture], None]) -> None:
        ...

    @abc.abstractmethod
    def stop(self) -> None:
        ...

    @property
    @abc.abstractmethod
    def running(self) -> bool:
        ...


# ----------------------------------------------------------------- 文字注入


class TextInjector(abc.ABC):
    """往当前焦点输入框注入文字。"""

    @abc.abstractmethod
    def inject_text(self, text: str) -> bool:
        ...

    @abc.abstractmethod
    def send_keys(self, tokens: Iterable[str]) -> bool:
        """按住一组键（down），用于「按住说话」这类持续动作。"""

    @abc.abstractmethod
    def release_keys(self, tokens: Iterable[str]) -> bool:
        ...


# ----------------------------------------------------------------- 音频播放


@dataclass(frozen=True)
class AudioEndpoint:
    index: int
    name: str
    channels: int = 2
    is_cable: bool = False    # 是否是 VB-Cable 之类的虚拟端点


class AudioPlayer(abc.ABC):
    """
    把 PCM 播到指定输出端点。

    实现要求：
        - 精确打开用户选定的端点，不用系统默认设备
        - 打开失败即 fail-closed（语音失败，但按键照常工作）
    """

    @abc.abstractmethod
    def list_outputs(self) -> list[AudioEndpoint]:
        ...

    @abc.abstractmethod
    def open(self, endpoint: AudioEndpoint, sample_rate: int = 16000) -> bool:
        ...

    @abc.abstractmethod
    def write_pcm(self, pcm: bytes) -> int:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...


# ----------------------------------------------------------------- 开机自启


@dataclass(frozen=True)
class StartupState:
    enabled: bool
    detail: str = ""


class StartupManager(abc.ABC):
    """开机自启。"""

    @abc.abstractmethod
    def read_state(self) -> StartupState:
        ...

    @abc.abstractmethod
    def set_enabled(self, enabled: bool) -> StartupState:
        ...

    @abc.abstractmethod
    def rebind_if_needed(self) -> StartupState:
        """换版本/换路径后自动重绑启动项（只改自己写的那条）。"""


# ------------------------------------------------------------------- 注册表


_REGISTRY: dict[str, type] = {}


def register(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        _REGISTRY[name] = cls
        return cls

    return deco


def create(name: str) -> Optional[object]:
    """按名字创建平台实现；未注册返回 None。"""
    cls = _REGISTRY.get(name)
    return cls() if cls else None


def available() -> list[str]:
    return sorted(_REGISTRY)


def current_platform_name() -> str:
    import sys
    return "windows" if sys.platform.startswith("win") else "macos"
