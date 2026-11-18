"""
平台层入口 —— 业务代码只跟这里打交道。

    from platforms import serial_port, hotkey_recorder, text_injector, ...

平台实现按需注册，缺依赖的实现会退化成 None，调用方要能容错。
"""
from __future__ import annotations

from .base import (AudioEndpoint, AudioPlayer, HotkeyCapture, HotkeyRecorder,
                   SerialPort, SerialPortInfo, StartupManager, StartupState,
                   available, create, current_platform_name)

from . import windows  # noqa: F401  （Windows 实现，导入即注册）

# 快捷键录制默认走**子进程**（钩子装在没有 Raw Input 的进程里）。
# windows.hotkey.local 是进程内旧实现，留着当备用。
from . import hotkey_capture_process  # noqa: E402
hotkey_capture_process.register_process_recorder()

try:
    from . import macos  # noqa: F401
except Exception:      # pragma: no cover - macOS 上没有 Windows 实现是正常的
    macos = None


def _make(name: str):
    return create(name)


serial_port: SerialPort | None = _make("windows.serial")
hotkey_recorder: HotkeyRecorder | None = _make("windows.hotkey")
text_injector = _make("windows.text")
audio_player: AudioPlayer | None = _make("windows.audio")
startup_manager: StartupManager | None = _make("windows.startup")


def platform_summary() -> dict:
    return {
        "platform": current_platform_name(),
        "serial": serial_port is not None,
        "hotkey": hotkey_recorder is not None,
        "text": text_injector is not None,
        "audio": audio_player is not None,
        "startup": startup_manager is not None,
        "registered": available(),
    }
