"""
配置存储 —— 原子写 + 多线程安全。

配置文件：<data_root>/config.json

原则：
    - 写入用「临时文件 + os.replace」原子替换，避免写一半掉电留下坏文件
    - 用 RLock 串行化，UI 线程和串口线程都会写
    - 临时文件名带线程 id，避免两个线程互相覆盖临时文件
      （旧实现踩过：UI 线程和串口线程共用同一个 .tmp）
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .resources import config_path, ensure_data_dirs

_LOCK = threading.RLock()

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "output_enabled": True,
    "voice_enabled": True,
    "asr_enabled": True,
    "voice_model_path": "",
    "voice_sink": "ime",              # local_asr | ime
    "voice_transport": "auto",        # auto | atvv | hidfc
    "ime_provider": "wechat",
    "ime_hotkey": "lctrl+lwin",
    # ★ 用户有没有在软件里改过语音快捷键 ★
    #   没改过 → 连上板子时只读不写，板子上生效的值原样保留
    #   （板子的出厂默认就是左Ctrl+左Win，用户在板子上改过的也不该被冲掉）
    "ime_hotkey_custom": False,
    "audio_endpoint": -1,
    "log_level": "INFO",
    "launch_at_login": False,
    "close_behavior": "hide",           # hide = 关窗口缩到托盘；quit = 直接退出
    "log_live": False,                  # 是否接收板端实时日志
    "remotes": [],
    # ★ 映射归档（按 identity / 固定 MAC 索引）★
    #   全新刷写会清掉板子 NVS 里的按键映射；
    #   这份归档让「刷完固件的板子认出是同一台遥控器」时能自动把映射推回去。
    "mapping_archive": {},
    "active_remote": -1,
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """读配置；不存在或损坏时返回默认配置的副本。"""
    path = config_path()
    with _LOCK:
        if not path.is_file():
            return dict(DEFAULT_CONFIG)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return dict(DEFAULT_CONFIG)
        if not raw.strip():
            return dict(DEFAULT_CONFIG)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return dict(DEFAULT_CONFIG)
        if not isinstance(data, dict):
            return dict(DEFAULT_CONFIG)
        return _deep_merge(DEFAULT_CONFIG, data)


def save_config(cfg: dict) -> bool:
    """原子写配置。"""
    ensure_data_dirs()
    path = config_path()
    with _LOCK:
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.{threading.get_ident()}.", suffix=".tmp",
                dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
            tmp_name = None
            return True
        except OSError:
            return False
        finally:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass


def update_config(**kwargs) -> dict:
    """局部更新并落盘。"""
    with _LOCK:
        cfg = load_config()
        cfg.update(kwargs)
        save_config(cfg)
        return cfg
