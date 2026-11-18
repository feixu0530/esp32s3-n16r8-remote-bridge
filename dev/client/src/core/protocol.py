"""
SP-v2 行协议客户端（PC 侧）。

协议格式（见 dev/firmware/bridge/main/br_proto.h）：
    PC  → 设备：<VERB> [arg ...]
    设备 → PC：
        OK <VERB> [k=v ...]      命令成功
        ER <VERB> <reason> [k=v] 命令失败
        EV <EVENT> [k=v ...]     异步事件
        DT <KIND> [k=v ...]      数据行（列表项）
        LG level=... msg=...     结构化日志
        # <free text>            调试文本

★ 顺序保证 ★
    任何列表型命令的所有 DT 行都在该命令的 OK 行**之前**到达。
    所以收到 OK 时，DT 回调已经把模型填好了 —— 不会有「列表随机为空」的竞态。

值转义：空格 %20、% %25、= %3D、换行 %0A、回车 %0D。
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

from platforms.base import SerialPort

KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S*)")


def unescape(text: str) -> str:
    """反转义（只认大写十六进制，与编码端一致）。"""
    if "%" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%" and i + 2 < n:
            try:
                out.append(chr(int(text[i + 1:i + 3], 16)))
                i += 3
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def parse_kv(tokens: list[str]) -> dict[str, str]:
    """把 k=v 形式的 token 列表解析成字典（值已反转义）。"""
    out: dict[str, str] = {}
    for tok in tokens:
        m = KV_RE.fullmatch(tok)
        if m:
            out[m.group(1)] = unescape(m.group(2))
    return out


def escape(text: str) -> str:
    """转义（与固件 br_proto.c 的 append_escaped 对应）。"""
    out: list[str] = []
    for ch in text:
        if ch == " ":
            out.append("%20")
        elif ch == "%":
            out.append("%25")
        elif ch == "=":
            out.append("%3D")
        elif ch == "\n":
            out.append("%0A")
        elif ch == "\r":
            out.append("%0D")
        elif ord(ch) < 0x20:
            out.append("%%%02X" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


@dataclass
class ProtocolResult:
    ok: bool
    verb: str = ""
    reason: str = ""
    kv: dict[str, str] = field(default_factory=dict)
    raw: str = ""

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.kv.get(key, str(default)), 0)
        except (TypeError, ValueError):
            return default

    def get_str(self, key: str, default: str = "") -> str:
        return self.kv.get(key, default)


EventHandler = Callable[[str, dict[str, str]], None]
DataHandler = Callable[[str, dict[str, str]], None]
LogHandler = Callable[[str, str], None]


class ProtocolClient:
    """
    串口协议客户端。

    一个后台读线程 + 每动词一个 FIFO 等待队列。
    """

    def __init__(self, port: SerialPort) -> None:
        self._port = port
        self._sent_handlers: list[Callable[[str], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict[str, Deque[threading.Event]] = {}
        self._event_handlers: list[EventHandler] = []
        self._data_handlers: list[DataHandler] = []
        self._log_handlers: list[LogHandler] = []
        self._raw_handlers: list[Callable[[str], None]] = []
        self._lines: list[str] = []
        self._max_lines = 5000

    # ------------------------------------------------------------ 事件订阅
    def on_event(self, fn: EventHandler) -> None:
        self._event_handlers.append(fn)

    def on_data(self, fn: DataHandler) -> None:
        self._data_handlers.append(fn)

    def on_log(self, fn: LogHandler) -> None:
        self._log_handlers.append(fn)

    def on_raw(self, fn: Callable[[str], None]) -> None:
        self._raw_handlers.append(fn)

    def on_sent(self, fn: Callable[[str], None]) -> None:
        """订阅「发出的命令」—— 排障时两边都有才算完整。"""
        self._sent_handlers.append(fn)

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="proto-rx",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # ★ 必须等读线程真的退出 ★
            #   线程还在读的时候就把串口关掉，读线程会落到失效句柄上 → 崩。
            thread.join(timeout=2.0)
            if thread.is_alive():
                self.log_warning("协议读线程没有按时退出")
            self._thread = None
        # 清理没人认领的待办（断线时排队的请求）
        with self._lock:
            for q in self._pending.values():
                while q:
                    ev = q.popleft()
                    setattr(ev, "_result", ProtocolResult(
                        ok=False, verb="", reason="closed"))
                    ev.set()
            self._pending.clear()

    @staticmethod
    def log_warning(msg: str) -> None:
        import logging
        logging.getLogger("bridge.device").warning("%s", msg)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 发送
    def send(self, verb: str, *args) -> str:
        """发一条命令（不等应答）。"""
        parts = [verb]
        for a in args:
            if a is None:
                continue
            parts.append(str(a))
        line = " ".join(parts)
        self._port.write((line + "\n").encode("utf-8", errors="replace"))
        return line

    def request(self, verb: str, *args, timeout: float = 4.0) -> ProtocolResult:
        """
        发命令并等应答。

        注意：**DT 行会在 OK 之前被分发**，所以拿到 OK 时模型已经填好。
        """
        ev = threading.Event()
        with self._lock:
            q = self._pending.setdefault(verb, deque())
            q.append(ev)

        self.send(verb, *args)

        if not ev.wait(timeout):
            with self._lock:
                q = self._pending.get(verb)
                if q and ev in q:
                    q.remove(ev)
            return ProtocolResult(ok=False, verb=verb, reason="timeout")

        # ★ 结果直接挂在 Event 上 ★
        #   以前放在 self._results[(verb, id(ev))] 里有两个毛病：
        #   ① 超时的请求结果永远没人取 → 字典单调增长（内存泄漏）
        #   ② id(ev) 在对象被回收后会复用 → 可能把上一次的应答
        #      返回给本次请求（表现为「刷新没反应」）
        result = getattr(ev, "_result", None)
        if result is None:
            return ProtocolResult(ok=False, verb=verb, reason="no-result")
        return result

    # ------------------------------------------------------------ 读线程
    def _reader(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            chunk = self._port.read(4096)
            if not chunk:
                time.sleep(0.005)
                continue
            buf += chunk
            if len(buf) > 64 * 1024:
                del buf[:-32 * 1024]
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                text = line.decode("utf-8", errors="replace").rstrip("\r")
                if text:
                    self._handle_line(text)

    def _handle_line(self, text: str) -> None:
        self._lines.append(text)
        if len(self._lines) > self._max_lines:
            del self._lines[:len(self._lines) // 2]

        for fn in self._raw_handlers:
            try:
                fn(text)
            except Exception:
                pass

        if text.startswith("#"):
            return

        parts = text.split(" ")
        token = parts[0]
        rest = parts[1:]

        if token == "OK":
            verb = rest[0] if rest else ""
            kv = parse_kv(rest[1:])
            self._resolve(verb, ProtocolResult(True, verb, "", kv, text))

        elif token == "ER":
            verb = rest[0] if rest else ""
            reason = rest[1] if len(rest) > 1 else "unknown"
            kv = parse_kv(rest[2:])
            self._resolve(verb, ProtocolResult(False, verb, reason, kv, text))

        elif token == "EV":
            name = rest[0] if rest else ""
            kv = parse_kv(rest[1:])
            for fn in self._event_handlers:
                try:
                    fn(name, kv)
                except Exception:
                    pass

        elif token == "DT":
            kind = rest[0] if rest else ""
            kv = parse_kv(rest[1:])
            for fn in self._data_handlers:
                try:
                    fn(kind, kv)
                except Exception:
                    pass

        elif token == "LG":
            kv = parse_kv(rest)
            level = kv.get("level", "info")
            msg = kv.get("msg", "")
            for fn in self._log_handlers:
                try:
                    fn(level, msg)
                except Exception:
                    pass

    def _resolve(self, verb: str, result: ProtocolResult) -> None:
        with self._lock:
            q = self._pending.get(verb)
            if not q:
                return
            ev = q.popleft()
            setattr(ev, "_result", result)
        ev.set()

    # ------------------------------------------------------------ 调试
    def recent_lines(self, count: int = 100) -> list[str]:
        with self._lock:
            return list(self._lines[-count:])
