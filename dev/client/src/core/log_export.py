"""
日志导出 ZIP。

抄 windows-remote-mic-app 的 log_export.py，保留三个关键设计：

    1. **快照稳定性**：日志在轮转过程中可能正在被改写。
       采集前后比对 (dev, ino, size, mtime)，不一致就重试（最多 2 轮）。
    2. **原子替换**：先写临时文件，再 os.replace 覆盖目标 ZIP。
    3. **manifest**：ZIP 里带一份 export-info.json，说明这次快照是否完整、
       有没有包含配置/录音（明确写 false，避免误导）。

另外：
    - 单文件最多取尾部 512KB，且截断对齐到首个完整行
    - **不包含**配置内容与录音
"""
from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .logging_setup import manager
from .resources import logs_dir

MAX_FILE_BYTES = 512 * 1024
MAX_ATTEMPTS = 2


def _log_files() -> list[Path]:
    d = logs_dir()
    if not d.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(d.glob("*.log*")):
        if p.is_file():
            out.append(p)
    return out


def _stat_key(path: Path) -> tuple:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime)


def _tail_bytes(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    """取文件尾部最多 limit 字节。"""
    try:
        size = path.stat().st_size
        if size <= limit:
            return path.read_bytes()
        with path.open("rb") as f:
            f.seek(-limit, os.SEEK_END)
            data = f.read()
    except OSError:
        return b""
    # 截断对齐到首个完整行
    idx = data.find(b"\n")
    if idx >= 0:
        data = data[idx + 1:]
    return data


def _collect() -> tuple[dict[str, bytes], bool]:
    """
    采集所有日志文件。

    返回 (内容表, 快照是否稳定)。
    """
    for attempt in range(MAX_ATTEMPTS):
        files = _log_files()
        before = {p.name: _stat_key(p) for p in files}
        content = {p.name: _tail_bytes(p) for p in files}
        after = {p.name: _stat_key(p) for p in files if p.exists()}

        stable = before == after
        if stable or attempt == MAX_ATTEMPTS - 1:
            return content, stable
        time.sleep(0.3)
    return {}, False


def downloads_dir() -> Path:
    """当前用户的「下载」文件夹（取不到就退回 ~/Downloads）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.c_wchar_p()
            GUID = ctypes.create_string_buffer(
                bytes.fromhex("374DE29012345639C4E51A3B8F57DAEF"))
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    GUID, 0, None, ctypes.byref(buf)) == 0 and buf.value:
                path = Path(buf.value)
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return path
        except Exception:
            pass
    return Path.home() / "Downloads"


def export_logs(target: Optional[Path] = None) -> Path:
    """
    导出日志 ZIP，返回最终路径。
    """
    # 先把队列里的日志冲干净，保证导出的是完整内容
    manager().flush_all(2.0)

    content, stable = _collect()

    if target is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = downloads_dir() / f"遥控桥接日志-{stamp}.zip"

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")

    manifest = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "incomplete": not stable,
        "snapshot_stable": stable,
        "includes_configuration": False,
        "includes_recordings": False,
        "files": sorted(content.keys()),
    }

    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in content.items():
            zf.writestr(name, data)
        zf.writestr("export-info.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))

    os.replace(tmp, target)
    return target


def open_logs_folder() -> bool:
    """打开日志目录。目录不存在就不开（避免建个空目录出来）。"""
    d = logs_dir()
    if not d.is_dir():
        return False
    try:
        if os.name == "nt":
            os.startfile(str(d))
        elif os.uname().sysname == "Darwin":
            os.system(f'open "{d}"')
        else:
            os.system(f'xdg-open "{d}"')
        return True
    except Exception:
        return False
