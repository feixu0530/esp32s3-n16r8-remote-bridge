"""
资源与数据目录解析 —— 绿色版里「唯一真相」。

绿色版（PyInstaller onedir）目录结构：
    ESP32S3RemoteBridge/
      ESP32S3RemoteBridge.exe
      _internal/          ← Python 运行时 + 依赖
      ui/qml/             ← QML
      models/             ← ASR 模型
      firmware/           ← 固件 bin
      tools/esptool/      ← esptool

数据目录（用户可写）：
    %LOCALAPPDATA%\\ESP32S3RemoteBridge\\
      config.json
      logs/
      remotes/            ← 上传的遥控器平面图
      presets/
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ESP32S3RemoteBridge"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序所在目录（绿色版的 exe 同级）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_root() -> Path:
    """只读资源根（QML / 模型 / 固件）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return app_root()
    return Path(__file__).resolve().parents[2]


def data_root() -> Path:
    """用户可写的数据根。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return data_root() / "config.json"


def logs_dir() -> Path:
    """日志目录 —— 只在真正写日志时才创建。"""
    return data_root() / "logs"


def remotes_dir() -> Path:
    """上传的遥控器平面图。"""
    return data_root() / "remotes"


def presets_dir() -> Path:
    return data_root() / "presets"


def assets_dir() -> Path:
    """随包资源根（遥控器实物图等）。"""
    for candidate in (app_root() / "assets",
                      bundle_root() / "assets",
                      Path(__file__).resolve().parents[1] / "assets"):
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[1] / "assets"


def qml_dir() -> Path:
    # 源码布局：<项目>/src/ui/qml；绿色版布局：<exe同级>/ui/qml
    for candidate in (app_root() / "ui" / "qml",
                      bundle_root() / "ui" / "qml",
                      Path(__file__).resolve().parents[1] / "ui" / "qml",
                      bundle_root() / "qml"):
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[1] / "ui" / "qml"


def models_dir() -> Path:
    for candidate in (app_root() / "models",
                      bundle_root() / "models",
                      Path(__file__).resolve().parents[2] / "models",
                      bundle_root() / "models" / "paraformer-zh-small"):
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parents[2] / "models"


def firmware_dir() -> Path:
    for candidate in (app_root() / "firmware", bundle_root() / "firmware"):
        if candidate.is_dir():
            return candidate
    return app_root() / "firmware"


def esptool_dir() -> Path:
    for candidate in (app_root() / "tools" / "esptool",
                      bundle_root() / "tools" / "esptool"):
        if candidate.is_dir():
            return candidate
    return app_root() / "tools" / "esptool"


def ensure_data_dirs() -> None:
    for d in (data_root(), remotes_dir(), presets_dir()):
        d.mkdir(parents=True, exist_ok=True)
