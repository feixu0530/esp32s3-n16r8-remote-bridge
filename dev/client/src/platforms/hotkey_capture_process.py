"""
快捷键录制 —— 钩子装在**独立子进程**里。

★ 为什么要开子进程 ★
    照抄参考项目 windows-remote-mic-app 的 hotkey_capture_process.py：
    Windows 的键盘 Raw Input 会**绕过整条低级键盘钩子链** ——
    前台进程自己注册了 Raw Input，WH_KEYBOARD_LL 的回调就再也收不到键。
    把钩子放进一个没有 Raw Input 的一次性子进程里，问题就没了。

    顺带解决另外两个坑：
    - 父进程退出时子进程跟着退出 → 钩子不会残留
    - 子进程崩了也不会拖垮主程序

★ 父进程 ↔ 子进程用 JSON 行协议 ★
    子进程往 stdout 写一行一个 JSON：
        {"kind": "ready"}                    钩子装好了
        {"kind": "result", "chord": "ctrl+c"}  录到了
        {"kind": "error", "message": "..."}    出错了

运行：
    python -m platforms.hotkey_capture_process          # 前台调试用
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

# ------------------------------------------------------------------ 键表

# ★ 用主进程那一份，别各存一份 ★
#   以前这里有一份独立的 _VK_NAMES，标点键没跟上 ——
#   录制逗号出来是 vk_BC，主进程的 HID 表认识 comma 却没机会见到它。
from platforms.windows import _MODIFIER_TOKENS, _VK_NAMES

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x00000010


def _token_for(vk: int) -> str:
    if vk in _MODIFIER_TOKENS:
        return _MODIFIER_TOKENS[vk]
    return _VK_NAMES.get(vk, f"vk_{vk:02X}")


# ------------------------------------------------------------------ 自测探针
# 测试用它真的打一串键，验证录制链路真的能收到。

CAPTURE_PROBE_KEYS = (0x11, 0x12, 0x50)   # Ctrl + Alt + P

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002


def send_test_chord(vks) -> int:
    """用 SendInput 打一串组合键，返回实际送出的事件数。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            # ★ 必须带上 MOUSEINPUT ★
            #   SendInput 校验 cbSize == sizeof(INPUT)，
            #   只放 KEYBDINPUT 的话结构体只有 32 字节（真值是 40），
            #   SendInput 直接返回 0 —— 一个键都发不出去。
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

    events = []
    for vk in vks:
        events.append(INPUT(type=_INPUT_KEYBOARD,
                            u=INPUT._U(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0,
                                                     time=0, dwExtraInfo=None))))
    for vk in reversed(list(vks)):
        events.append(INPUT(type=_INPUT_KEYBOARD,
                            u=INPUT._U(ki=KEYBDINPUT(wVk=vk, wScan=0,
                                                     dwFlags=_KEYEVENTF_KEYUP,
                                                     time=0, dwExtraInfo=None))))
    arr = (INPUT * len(events))(*events)
    # ★ 同样要声明 argtypes ★
    #   不声明的话数组指针会被截断，SendInput 直接返回 0（一个键都没发出去）。
    user32.SendInput.argtypes = [ctypes.c_ulong, ctypes.POINTER(INPUT),
                                 ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    return int(user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)))


# ------------------------------------------------------------------ 子进程


def child_main() -> int:
    """
    子进程入口：装钩子，录到组合键就报上去。

    ★ 三条命门（照抄参考项目的教训）★
      1. 装钩子**之前**必须先 PeekMessageW 建出消息队列，
         否则 SetWindowsHookExW 看起来成功了，回调却永远不来。
      2. Escape 永远放行 —— 不然用户在弹窗里按 Escape 取消不了。
      3. 卸载要用 SetWindowsHookExW 返回的**真句柄**，传 0 卸载失败，
         钩子会留在系统里指向已回收的回调内存 → 用户下次按键直接崩。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # ★ 必须声明 argtypes ★
    #   不声明的话，HMODULE / HHOOK 这种指针参数会被当成 32 位整数传，
    #   64 位下句柄被截断 → SetWindowsHookExW 直接失败。
    #   v0.15.0 的录制「永远录不到」就是这个原因。
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_ulong]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = ctypes.c_bool
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                    ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_uint, ctypes.c_uint]
    user32.PeekMessageW.restype = ctypes.c_bool
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_void_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    down: list[int] = []
    tokens: list[str] = []
    done = False
    finished = threading.Event()
    state = {"hook": None, "cb": None}

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)

    def on_event(ncode: int, wparam: int, lparam: int) -> int:
        nonlocal done
        if ncode >= 0:
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                vk = ctypes.wintypes.DWORD.from_address(lparam).value
                if vk == 0x1B:      # Escape 放行
                    return 0
                flags = ctypes.wintypes.DWORD.from_address(
                    lparam + 8).value
                if flags & LLKHF_INJECTED:
                    # 注入事件（远程软件转发的键盘）也要录
                    pass
                if vk not in down:
                    down.append(vk)
                    # ★ token 在按下时记 ★
                    #   抬起时才拼的话，最后一次抬起手里只剩最后一个键，
                    #   组合键就变成单键了。
                    tokens.append(_token_for(vk))
                if len(down) == 1:
                    done = False
            elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                vk = ctypes.wintypes.DWORD.from_address(lparam).value
                if vk == 0x1B:
                    return 0
                if vk in down:
                    down.remove(vk)
                if not down and not done:
                    done = True
                    _emit({"kind": "result", "chord": "+".join(tokens)})
                    finished.set()
        return 1   # 吞掉，避免影响前台程序

    def _emit(obj: dict) -> None:
        try:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    msg = wintypes.MSG()
    # ★ 先建消息队列 ★
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

    cb = HOOKPROC(on_event)
    state["cb"] = cb            # ★ 持有引用，别让它被 GC ★
    handle = user32.SetWindowsHookExW(
        WH_KEYBOARD_LL, cb, kernel32.GetModuleHandleW(None), 0)
    if not handle:
        _emit({"kind": "error", "message": "SetWindowsHookExW 失败"})
        return 1
    state["hook"] = handle
    _emit({"kind": "ready"})

    try:
        while not finished.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnhookWindowsHookEx(handle)
        state["hook"] = None
        state["cb"] = None
    return 0


# ------------------------------------------------------------------ 父进程


class ProcessHotkeyRecorder:
    """
    父进程侧：起子进程、读 JSON 行、拿到结果。

    对外接口和 platforms.windows.HotkeyRecorderWin 一致：
        start(on_capture) / stop() / running
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._on_capture: Optional[Callable[[str], None]] = None
        self._error: Optional[Callable[[str], None]] = None
        self._ready = threading.Event()

    # -- 子进程命令行 ------------------------------------------------
    @staticmethod
    def _command() -> list[str]:
        if getattr(sys, "frozen", False):
            # 打包版：同一个 exe 换个参数再跑一遍
            return [sys.executable, "--capture-hotkey-worker"]
        # 源码运行：-c 起一段脚本
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from platforms.hotkey_capture_process import child_main; "
            "raise SystemExit(child_main())" % here
        )
        return [sys.executable, "-c", code]

    # -- 生命周期 ----------------------------------------------------
    def start(self, on_capture: Callable[[str], None],
              on_error: Optional[Callable[[str], None]] = None) -> bool:
        if self._running:
            return True
        self._on_capture = on_capture
        self._error = on_error
        cmd = self._command()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                bufsize=0,
            )
        except Exception as exc:
            if on_error:
                on_error(f"起录制进程失败：{exc}")
            return False
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._reader, name="hotkey-cap",
                                       daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        proc = self._proc
        self._running = False
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.5)
        return True

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等子进程报告「钩子装好了」。

        ★ 别用固定 sleep 等 ★
          机器忙的时候（打包、编译）钩子装好要好几秒，
          固定睡 1.2 秒就打早了 —— 打出来的键全丢了，
          测试就会时好时坏（真机上也可能是同一种毛病）。
        """
        return self._ready.wait(timeout)

    @property
    def running(self) -> bool:
        return self._running

    # -- 读子进程的 JSON 行 -------------------------------------------
    def _reader(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                if not self._running:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                kind = obj.get("kind")
                if kind == "ready":
                    # ★ 钩子装好了 ★
                    #   调用方等这个信号再打键 ——
                    #   固定 sleep 一两秒在机器忙的时候不够（钩子还没装好，
                    #   打出来的键全丢了），测试就会时好时坏。
                    self._ready.set()
                    continue
                if kind == "result":
                    chord = str(obj.get("chord", "") or "")
                    if chord and self._on_capture:
                        try:
                            self._on_capture(chord)
                        except Exception:
                            pass
                    self.stop()
                    return
                if kind == "error":
                    if self._error:
                        self._error(str(obj.get("message", "录制失败")))
                    self.stop()
                    return
        except Exception:
            pass
        finally:
            self._running = False


class HotkeyRecorderProcess:
    """
    把子进程录到的组合键包成 HotkeyCapture，接口跟 HotkeyRecorder 一致。

    这样 controller 那边不用改 —— 它只认 start(on_capture) / stop() / running。
    """

    def __init__(self) -> None:
        self._proc = ProcessHotkeyRecorder()
        self._tokens: tuple[str, ...] = ()

    def start(self, on_capture: Callable[[object], None]) -> bool:
        def got(chord: str) -> None:
            from .base import HotkeyCapture
            from .windows import _TOKEN_HID
            tokens = tuple(t.strip().lower()
                           for t in chord.split("+") if t.strip())
            hid = tuple(_TOKEN_HID[t] for t in tokens if t in _TOKEN_HID)
            on_capture(HotkeyCapture(tuple(), tokens, 0, hid))

        return self._proc.start(got)

    def stop(self) -> bool:
        return self._proc.stop()

    @property
    def running(self) -> bool:
        return self._proc.running


def register_process_recorder() -> None:
    """把子进程版注册成 windows.hotkey（默认用的那个）。"""
    from .base import register
    try:
        register("windows.hotkey")(HotkeyRecorderProcess)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(child_main())
