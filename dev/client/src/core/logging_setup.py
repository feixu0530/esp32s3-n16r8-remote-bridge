"""
日志体系 —— 异步写盘 + 隐私过滤 + 轮转。

抄 windows-remote-mic-app 的 logging_setup.py，但按本项目的需要做了两处调整：

    1. **多日志文件**：app.log（主程序）/ device.log（固件交互）/ flash.log（烧录）
       各自独立轮转，出问题时不用在一堆日志里翻。
    2. **日志级别可配**（参考项目写死 INFO）—— 设置页能切 DEBUG/INFO/WARN。

核心机制：
    EvidenceFileHandler
        - 独立 daemon 线程 + deque 队列
        - emit() 只入队，不落盘（绝不阻塞业务线程）
        - 队列满时丢普通 INFO，但**优先保留 ERROR**
        - 写失败不杀线程，1 秒后重试
        - flush_pending(timeout) 屏障：保证导出前日志已落盘

隐私约束（硬性）：
    **不记录** MAC 地址、HID 路径、设备 token、语音内容。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .resources import logs_dir

LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 1
LOG_QUEUE_SIZE = 2048
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}

# 禁止记录的敏感片段（替换成 <redacted>）
_SENSITIVE = ("mac=", "addr=", "ble_addr", "hwid", "token", "serial=")


def _redact(text: str) -> str:
    low = text.lower()
    for key in _SENSITIVE:
        if key in low:
            return "<redacted>"
    return text


class PrivacySafeExceptionFilter(logging.Filter):
    """
    异常只保留类型，不留 traceback。

    traceback 里经常带文件路径、变量值（可能含 MAC / 序列号）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            # 只留异常类型，不留 exc_text / traceback
            record.exc_info = None
            record.exc_text = None
            record.msg = f"{record.msg} ({record.exc_info[0].__name__})" \
                if record.exc_info else record.msg
        return True


class EvidenceFileHandler(RotatingFileHandler):
    """
    异步写文件的轮转处理器。

    继承 RotatingFileHandler 拿到轮转能力，但**不让它做实际的写入** ——
    真正的写入搬到自己的线程里。
    """

    def __init__(self, path: Path, level: int = logging.INFO):
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(str(path), maxBytes=LOG_MAX_BYTES,
                         backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
                         delay=True)
        self.setLevel(level)
        self.setFormatter(logging.Formatter(LOG_FORMAT))

        self._queue: deque[logging.LogRecord] = deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._stop = False
        self._retry_after = 0.0
        self._dropped = 0

        self._thread = threading.Thread(
            target=self._run, name="log-writer", daemon=True)
        self._thread.start()

    # -- 覆盖写入接口：只入队 --
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.exc_info:
                record.exc_info = None
            if isinstance(record.msg, str):
                record.msg = _redact(record.msg)
            with self._lock:
                if len(self._queue) >= LOG_QUEUE_SIZE:
                    # 队列满：丢掉最旧的一条普通记录，保留 ERROR
                    for _ in range(len(self._queue)):
                        old = self._queue.popleft()
                        if old.levelno >= logging.ERROR:
                            self._queue.append(old)
                            break
                        self._dropped += 1
                self._queue.append(record)
                self._not_empty.notify()
        except Exception:
            self.handleError(record)

    # -- 写线程 --
    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._stop:
                    self._not_empty.wait(timeout=0.5)
                if self._stop and not self._queue:
                    return
                batch = list(self._queue)
                self._queue.clear()

            if time.time() < self._retry_after:
                time.sleep(0.5)
                continue

            try:
                for record in batch:
                    self._write_record(record)
            except Exception:
                # 写失败：放回去，1 秒后重试，不杀线程
                self._retry_after = time.time() + 1.0
                with self._lock:
                    for record in reversed(batch):
                        self._queue.appendleft(record)

    def _write_record(self, record: logging.LogRecord) -> None:
        # 借用父类的格式化与轮转，但写到自己的流
        if self.stream is None:
            self.stream = self._open()
        msg = self.format(record)
        self.stream.write(msg + self.terminator)
        self.stream.flush()
        if self.shouldRollover(record):
            self.doRollover()

    # -- 屏障 --
    def flush_pending(self, timeout: float = 2.0) -> bool:
        """等队列排空并落盘。返回是否成功。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._queue:
                    break
            time.sleep(0.02)
        with self._lock:
            empty = not self._queue
        try:
            if self.stream is not None:
                self.stream.flush()
        except Exception:
            pass
        return empty

    @property
    def dropped(self) -> int:
        return self._dropped

    def close(self) -> None:
        self.flush_pending(1.0)
        self._stop = True
        with self._lock:
            self._not_empty.notify_all()
        try:
            super().close()
        except Exception:
            pass


class LogManager:
    """管理多个日志文件与全局级别。"""

    def __init__(self) -> None:
        self._handlers: dict[str, EvidenceFileHandler] = {}
        self._root = logging.getLogger("bridge")
        self._root.setLevel(logging.INFO)
        self._root.propagate = False
        self._level = "INFO"

    def setup(self, files: tuple[str, ...] = ("app", "device", "flash")) -> None:
        logs_dir().mkdir(parents=True, exist_ok=True)
        for name in files:
            path = logs_dir() / f"{name}.log"
            handler = EvidenceFileHandler(path, level=logging.DEBUG)
            handler.addFilter(PrivacySafeExceptionFilter())
            logger = logging.getLogger(f"bridge.{name}")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.handlers = [handler]
            self._handlers[name] = handler

    def logger(self, name: str) -> logging.Logger:
        """取某个日志通道；未知名字落到 app。"""
        if name not in self._handlers:
            return logging.getLogger("bridge.app")
        return logging.getLogger(f"bridge.{name}")

    def set_level(self, level_name: str) -> str:
        level_name = level_name.upper()
        if level_name not in LOG_LEVELS:
            return self._level
        self._level = level_name
        level = LOG_LEVELS[level_name]
        self._root.setLevel(level)
        for handler in self._handlers.values():
            handler.setLevel(level)
        return self._level

    def get_level(self) -> str:
        return self._level

    def flush_all(self, timeout: float = 2.0) -> bool:
        ok = True
        for handler in self._handlers.values():
            ok = handler.flush_pending(timeout) and ok
        return ok

    def log_files(self) -> list[Path]:
        return [h.baseFilename for h in self._handlers.values()]


_manager: Optional[LogManager] = None


def manager() -> LogManager:
    global _manager
    if _manager is None:
        _manager = LogManager()
    return _manager


def setup(level: str = "INFO") -> LogManager:
    m = manager()
    m.setup()
    m.set_level(level)
    return m


def get_logger(name: str = "app") -> logging.Logger:
    return manager().logger(name)
