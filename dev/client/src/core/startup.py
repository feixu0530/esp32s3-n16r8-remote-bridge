"""
开机自启 —— 对平台层的薄封装。

真正的实现在 platform/{windows,macos}.py：
    Windows: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
    macOS  : LaunchAgent plist
"""
from __future__ import annotations

from .resources import app_root
from platforms import startup_manager
from platforms.base import StartupState

VALUE_NAME = "ESP32S3RemoteBridge"


def read_state() -> StartupState:
    if startup_manager is None:
        return StartupState(False, "当前平台不支持开机自启")
    return startup_manager.read_state()


def set_enabled(enabled: bool) -> StartupState:
    if startup_manager is None:
        return StartupState(False, "当前平台不支持开机自启")
    return startup_manager.set_enabled(enabled)


def rebind_if_needed() -> StartupState:
    """启动时调用：换版本后自动把启动项指回当前 exe。"""
    if startup_manager is None:
        return StartupState(False, "当前平台不支持开机自启")
    return startup_manager.rebind_if_needed()


def executable_path() -> str:
    return str(app_root())
