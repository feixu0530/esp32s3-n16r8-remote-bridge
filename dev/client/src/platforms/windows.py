"""
Windows 平台实现。

包含：
    SerialPortWin      串口（pyserial）
    HotkeyRecorderWin  WH_KEYBOARD_LL 全局钩子录制
    TextInjectorWin    剪贴板 + Ctrl+V / SendInput
    AudioPlayerWin     sounddevice 输出端点
    StartupManagerWin  HKCU\\...\\Run 开机自启
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Iterable, Optional

from .base import (AudioEndpoint, AudioPlayer, HotkeyCapture,
                   HotkeyRecorder, SerialPort, SerialPortInfo, StartupManager,
                   StartupState, TextInjector, register)

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ================================================================== 串口


@register("windows.serial")
class SerialPortWin(SerialPort):
    """
    串口（pyserial）。

    ★ 所有方法都加锁 ★
      协议读线程在 read()，而 UI 线程可能在 open()/close()。
      不加锁就会出现「一个线程正在读、另一个线程把句柄 Close 掉」——
      Windows 驱动层行为未定义，直接 access violation。
      v0.15.0 实测：在串口下拉框里换一个口，程序当场消失。
    """

    def __init__(self) -> None:
        self._ser = None
        self._lock = threading.RLock()

    def list_ports(self) -> list[SerialPortInfo]:
        import serial.tools.list_ports as lp
        out: list[SerialPortInfo] = []
        for p in lp.comports():
            out.append(SerialPortInfo(p.device, p.description or "",
                                       p.vid or 0, p.pid or 0))
        return out

    def open(self, device: str, baud: int = 115200) -> None:
        import serial
        with self._lock:
            self._close_locked()
            self._ser = serial.Serial(device, baud, timeout=0.05)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """关掉当前句柄。调用方必须已持有锁。"""
        ser = self._ser
        self._ser = None
        if ser is None:
            return
        try:
            # ★ 先取消阻塞的读，再关句柄 ★
            #   不 cancel 就 close，Windows 上正在 ReadFile 的那个线程
            #   会落到已失效的句柄上。
            cancel = getattr(ser, "cancel_read", None)
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass

    def read(self, size: int = 4096) -> bytes:
        with self._lock:
            ser = self._ser
            if ser is None:
                return b""
            try:
                return ser.read(size)
            except Exception:
                return b""

    def write(self, data: bytes) -> int:
        with self._lock:
            ser = self._ser
            if ser is None:
                return 0
            try:
                ser.write(data)
                ser.flush()
                return len(data)
            except Exception:
                return 0

    def set_dtr(self, on: bool) -> None:
        with self._lock:
            ser = self._ser
            if ser is not None:
                try:
                    ser.dtr = on
                except Exception:
                    pass

    @property
    def is_open(self) -> bool:
        with self._lock:
            ser = self._ser
            return ser is not None and ser.is_open


# ================================================================== 键盘录制

# 修饰键的 VK -> token（左右要分开）
_MODIFIER_TOKENS = {
    0xA0: "lshift", 0xA1: "rshift",
    0xA2: "lctrl", 0xA3: "rctrl",
    0xA4: "lalt", 0xA5: "ralt",
    0x5B: "lwin", 0x5C: "rwin",
}

# VK -> 名称（只覆盖常用键，够用即可）
_VK_NAMES = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "escape",
    0x20: "space", 0x21: "pageup", 0x22: "pagedown", 0x23: "end",
    0x24: "home", 0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2C: "printscreen", 0x2D: "insert", 0x2E: "delete",
    0x30: "0", 0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
    0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8", 0x39: "9",
    0x41: "a", 0x42: "b", 0x43: "c", 0x44: "d", 0x45: "e",
    0x46: "f", 0x47: "g", 0x48: "h", 0x49: "i", 0x4A: "j",
    0x4B: "k", 0x4C: "l", 0x4D: "m", 0x4E: "n", 0x4F: "o",
    0x50: "p", 0x51: "q", 0x52: "r", 0x53: "s", 0x54: "t",
    0x55: "u", 0x56: "v", 0x57: "w", 0x58: "x", 0x59: "y",
    0x5A: "z", 0x5D: "apps",
    # 标点区（OEM 键）—— 少了这些，逗号/句号会被录成 vk_XX，QML 判为无法识别
    0xBA: "semicolon", 0xBB: "equal", 0xBC: "comma", 0xBD: "minus",
    0xBE: "period", 0xBF: "slash", 0xC0: "grave",
    0xDB: "bracketleft", 0xDC: "backslash", 0xDD: "bracketright",
    0xDE: "quote",
    0x70: "f1", 0x71: "f2", 0x72: "f3", 0x73: "f4", 0x74: "f5",
    0x75: "f6", 0x76: "f7", 0x77: "f8", 0x78: "f9", 0x79: "f10",
    0x7A: "f11", 0x7B: "f12",
    0xAD: "volume_mute", 0xAE: "volume_down", 0xAF: "volume_up",
    0xB3: "media_play_pause",
}

# token -> HID usage（USB HID Keyboard/Keypad page）
_TOKEN_HID = {
    "backspace": 0x2A, "tab": 0x2B, "enter": 0x28, "escape": 0x29,
    "space": 0x2C, "pageup": 0x33, "pagedown": 0x34, "end": 0x35,
    "home": 0x36, "left": 0x50, "up": 0x52, "right": 0x4F, "down": 0x51,
    "insert": 0x49, "delete": 0x4A,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E,
    "f6": 0x3F, "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43,
    "f11": 0x44, "f12": 0x45,
    "lctrl": 0xE0, "rctrl": 0xE4, "lshift": 0xE1, "rshift": 0xE5,
    "lalt": 0xE2, "ralt": 0xE6, "lwin": 0xE3, "rwin": 0xE7,
    "apps": 0x65,
    # 标点（USB HID 键盘页，与 PC 键盘主键区一一对应）
    "minus": 0x2D, "equal": 0x2E, "bracketleft": 0x2F,
    "bracketright": 0x30, "backslash": 0x31, "semicolon": 0x33,
    "quote": 0x34, "grave": 0x35, "comma": 0x36, "period": 0x37,
    "slash": 0x38, "printscreen": 0x46,
    "volume_mute": 0xE2, "volume_down": 0x51, "volume_up": 0x52,
    "media_play_pause": 0xCD,
}
for _i in range(10):
    _TOKEN_HID[str(_i)] = 0x1E + _i
for _c in range(26):
    _TOKEN_HID[chr(ord('a') + _c)] = 0x04 + _c


def token_for_keyboard_usage(usage: int) -> str | None:
    """键盘页 usage -> 键名（日志里显示 a / q，而不是 0x04）。"""
    return _KEYBOARD_USAGE_TOKENS.get(usage)


def hid_usage_for_token(token: str) -> int | None:
    """Return the USB HID keyboard usage for a saved mapping token."""
    token = (token or "").strip().lower()
    token = {"ctrl": "lctrl", "control": "lctrl", "shift": "lshift",
             "alt": "lalt", "win": "lwin", "cmd": "lwin"}.get(token, token)
    return _TOKEN_HID.get(token)


# 修饰键位掩码的每一位对应哪个 token（与 USB HID 键盘页一致）
_MODIFIER_TOKEN_BY_BIT = ("lctrl", "lshift", "lalt", "lwin",
                         "rctrl", "rshift", "ralt", "rwin")

# 键盘页 usage -> token 的反向表（用于把板子上的映射读回 PC）。
# 消费者页的 token（音量之类）不在这里 —— 它们走 out=2，
# usage 数值跟键盘页撞号，只能按 token 分开认。
_KEYBOARD_USAGE_TOKENS: dict[int, str] = {}
for _tok, _u in _TOKEN_HID.items():
    if _tok in ("volume_mute", "volume_down", "volume_up", "media_play_pause"):
        continue
    _KEYBOARD_USAGE_TOKENS.setdefault(_u, _tok)


def tokens_from_firmware(mods: int, usages: list[int]) -> list[str]:
    """
    把开发板上存的一条映射还原成 token 列表。

    mods  是修饰键位掩码（每位一个修饰键）
    usages 是普通键的 HID usage
    """
    tokens: list[str] = []
    for bit in range(8):
        if mods & (1 << bit):
            tokens.append(_MODIFIER_TOKEN_BY_BIT[bit])
    for usage in usages:
        token = _KEYBOARD_USAGE_TOKENS.get(usage)
        if token:
            tokens.append(token)
    return tokens


@register("windows.hotkey.local")
class HotkeyRecorderWin(HotkeyRecorder):
    """
    WH_KEYBOARD_LL 全局低级键盘钩子。

    ★ 关键行为（抄 winmic 的 hotkey_capture_windows.py）★
      - 保留首次按下顺序
      - 区分左右修饰键
      - **吞掉被录事件**（不影响前台程序）
    """

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105

    def __init__(self) -> None:
        self._hook = None
        self._hook_handle = None
        self._hook_proc_cb = None
        self._thread = None
        self._running = False
        self._on_capture: Optional[Callable[[HotkeyCapture], None]] = None
        self._down: list[int] = []
        self._done = False

    # -- 钩子回调（必须在安装它的线程里）--
    def _hook_proc(self, ncode: int, wparam: int, lparam: int) -> int:
        if ncode >= 0:
            if wparam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                vk = ctypes.wintypes.DWORD.from_address(lparam).value
                # ★ Escape 永远放行给 Qt ★
                #   不放行的话，用户在录制弹窗里按 Escape 取消不了 ——
                #   而且「关掉弹窗后 hook 还挂着」会把全系统键盘吞掉。
                if vk == 0x1B:
                    return 0
                if vk not in self._down:
                    self._down.append(vk)
                if len(self._down) == 1:
                    self._done = False
            elif wparam in (self.WM_KEYUP, self.WM_SYSKEYUP):
                vk = ctypes.wintypes.DWORD.from_address(lparam).value
                if vk == 0x1B:
                    return 0
                if vk in self._down:
                    self._down.remove(vk)
                # 最后一个键抬起 → 录制完成
                if not self._down and not self._done:
                    self._done = True
                    cap = self._build()
                    if cap is not None and self._on_capture:
                        try:
                            self._on_capture(cap)
                        except Exception:
                            pass
        # 返回 1 = 吞掉事件，不传给前台
        return 1

    def _build(self) -> Optional[HotkeyCapture]:
        if not self._down:
            return None
        tokens = []
        for vk in self._down:
            if vk in _MODIFIER_TOKENS:
                tokens.append(_MODIFIER_TOKENS[vk])
            elif vk in _VK_NAMES:
                tokens.append(_VK_NAMES[vk])
            else:
                tokens.append(f"vk_{vk:02X}")
        hid = tuple(_TOKEN_HID[t] for t in tokens if t in _TOKEN_HID)
        return HotkeyCapture(tuple(self._down), tuple(tokens), 0, hid)

    def start(self, on_capture: Callable[[HotkeyCapture], None]) -> None:
        import threading

        if self._running:
            return
        self._on_capture = on_capture
        self._down = []
        self._done = False

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)

        def run():
            msg = wintypes.MSG()
            # ★ 必须先 PeekMessageW 建出线程的消息队列 ★
            #   低级键盘钩子靠消息队列派发回调。队列还没建就装钩子的话，
            #   SetWindowsHookExW 看起来成功了，回调却永远不来 ——
            #   表现就是「点了录制，按什么都没反应」（v0.15.0 实测）。
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

            # ★ 钩子回调对象必须活到卸载为止 ★
            #   它是局部变量的话，run() 退出后就被 GC 回收，
            #   而内核里的钩子还指着那块内存 → 用户下次按键直接 access violation。
            self._hook_proc_cb = HOOKPROC(self._hook_proc)
            handle = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL,
                                             self._hook_proc_cb,
                                             kernel32.GetModuleHandleW(None), 0)
            if not handle:
                self._hook_proc_cb = None
                return
            self._hook_handle = handle
            try:
                while self._running:
                    if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0):
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                    else:
                        time.sleep(0.01)
            finally:
                # ★ 用真句柄卸载 ★
                #   传 0 卸载失败，钩子会留在系统里。
                user32.UnhookWindowsHookEx(handle)
                self._hook_handle = None
                self._hook_proc_cb = None

        self._running = True
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._running


# ================================================================== 文字注入

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_KEYBOARD = 1
_INPUT_MOUSE = 0

# token -> (VK, 扩展位)
_TOKEN_VK = {
    "lctrl": (0x11, False), "rctrl": (0x11, True),
    "lshift": (0x10, False), "rshift": (0x10, True),
    "lalt": (0x12, False), "ralt": (0x12, True),
    "lwin": (0x5B, False), "rwin": (0x5C, False),
    "enter": (0x0D, False), "escape": (0x1B, False), "space": (0x20, False),
    "tab": (0x09, False), "backspace": (0x08, False),
    "left": (0x25, False), "up": (0x26, False), "right": (0x27, False),
    "down": (0x28, False),
    "f1": (0x70, False), "f2": (0x71, False), "f3": (0x72, False),
    "f4": (0x73, False), "f5": (0x74, False),
}


@register("windows.text")
class TextInjectorWin(TextInjector):
    """
    文字注入。

    中文没法用键盘事件直接打，所以走**剪贴板 + Ctrl+V**：
        1. 存原剪贴板
        2. 写新文本到剪贴板
        3. SendInput 发 Ctrl+V
        4. 恢复原剪贴板
    """

    def __init__(self) -> None:
        self._orig_clip: Optional[str] = None

    # -- 剪贴板 --
    def _clip_get(self) -> Optional[str]:
        if not IS_WINDOWS:
            return None
        CF_UNICODETEXT = 13
        if not user32.OpenClipboard(None):
            return None
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(ctypes.c_void_p(h))
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(ctypes.c_void_p(h))
        finally:
            user32.CloseClipboard()

    def _clip_set(self, text: str) -> None:
        if not IS_WINDOWS:
            return
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            data = text + "\0"
            size = len(data) * ctypes.sizeof(ctypes.c_wchar)
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                return
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(ctypes.c_void_p(h))
            if ptr:
                ctypes.memmove(ptr, data, size)
                kernel32.GlobalUnlock(ctypes.c_void_p(h))
                user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h))
        finally:
            user32.CloseClipboard()

    # -- SendInput --
    def _send_vk(self, vk: int, extended: bool, up: bool = False) -> bool:
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        flags = 0
        if extended:
            flags |= 0x0001
        if up:
            flags |= _KEYEVENTF_KEYUP
        inp = INPUT(type=_INPUT_KEYBOARD)
        inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                            dwExtraInfo=None)
        user32.SendInput.restype = wintypes.UINT
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1

    def inject_text(self, text: str) -> bool:
        if not text:
            return False
        self._orig_clip = self._clip_get()
        try:
            self._clip_set(text)
            # Ctrl+V
            self._send_vk(0x11, False, False)   # lctrl down
            self._send_vk(0x56, False, False)   # v down
            self._send_vk(0x56, False, True)    # v up
            self._send_vk(0x11, False, True)    # lctrl up
            time.sleep(0.05)
            return True
        finally:
            if self._orig_clip is not None:
                try:
                    self._clip_set(self._orig_clip)
                except Exception:
                    pass
                self._orig_clip = None

    def send_keys(self, tokens: Iterable[str]) -> bool:
        ok = True
        for t in tokens:
            vk, ext = _TOKEN_VK.get(t, (None, False))
            if vk is None:
                continue
            ok = self._send_vk(vk, ext, False) and ok
        return ok

    def release_keys(self, tokens: Iterable[str]) -> bool:
        ok = True
        for t in reversed(list(tokens)):
            vk, ext = _TOKEN_VK.get(t, (None, False))
            if vk is None:
                continue
            ok = self._send_vk(vk, ext, True) and ok
        return ok


# ================================================================== 音频播放


@register("windows.audio")
class AudioPlayerWin(AudioPlayer):
    """sounddevice 输出端点。"""

    def __init__(self) -> None:
        self._stream = None
        self._sd = None

    def _module(self):
        try:
            import sounddevice as sd
            return sd
        except Exception:
            return None

    def list_outputs(self) -> list[AudioEndpoint]:
        sd = self._module()
        if sd is None:
            return []
        out: list[AudioEndpoint] = []
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_output_channels", 0) <= 0:
                    continue
                name = dev.get("name", "")
                out.append(AudioEndpoint(
                    index=idx, name=name,
                    channels=int(dev.get("max_output_channels", 2)),
                    is_cable=("cable" in name.lower()
                              or "vb-audio" in name.lower()),
                ))
        except Exception:
            return []
        return out

    def open(self, endpoint: AudioEndpoint, sample_rate: int = 16000) -> bool:
        sd = self._module()
        if sd is None:
            return False
        self.close()
        try:
            self._stream = sd.OutputStream(
                device=endpoint.index, channels=1, dtype="int16",
                samplerate=sample_rate, blocksize=0)
            self._stream.start()
            return True
        except Exception:
            self._stream = None
            return False

    def write_pcm(self, pcm: bytes) -> int:
        if self._stream is None:
            return 0
        try:
            self._stream.write(pcm)
            return len(pcm)
        except Exception:
            return 0

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# ================================================================== 开机自启

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
BACKGROUND_FLAG = "--background"


@register("windows.startup")
class StartupManagerWin(StartupManager):
    """
    HKCU Run 键开机自启。

    ★ 抄 winmic 的 startup_windows.py 三件套 ★
      1. 写/删注册表值
      2. **读注册表做字符串全等**来判状态（不靠 config.json 缓存）
      3. 换版本自动重绑（只改自己写的那条，不碰用户其它启动项）
    """

    def __init__(self, value_name: str = "ESP32S3RemoteBridge") -> None:
        self.value_name = value_name

    def _command(self) -> str:
        exe = sys.executable
        if getattr(sys, "frozen", False):
            return f'"{exe}" {BACKGROUND_FLAG}'
        return f'"{exe}" "{os.path.abspath(sys.argv[0])}" {BACKGROUND_FLAG}'

    def _read(self) -> Optional[str]:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
                value, _ = winreg.QueryValueEx(key, self.value_name)
                return value
        except (FileNotFoundError, OSError):
            return None

    def read_state(self) -> StartupState:
        value = self._read()
        if value is None:
            return StartupState(False, "未设置")
        if value == self._command():
            return StartupState(True, "已启用")
        # 指向别的 exe（换版本/换路径了）
        return StartupState(False, "启动项指向其它路径，需要重绑")

    def set_enabled(self, enabled: bool) -> StartupState:
        import winreg
        if enabled:
            cmd = self._command()
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH,
                                    0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH,
                                    0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, self.value_name)
            except FileNotFoundError:
                pass
        return self.read_state()

    def rebind_if_needed(self) -> StartupState:
        """
        启动项指向旧 exe 时自动重绑。

        只在确认旧值是「本产品自己的」启动项时才改写 ——
        判定方式：旧命令行里可执行文件名与当前产品名一致。
        """
        value = self._read()
        if value is None or value == self._command():
            return self.read_state()

        if not self._is_ours(value):
            # 用户的其它启动项，不碰
            return self.read_state()

        return self.set_enabled(True)

    @staticmethod
    def _is_ours(command: str) -> bool:
        import re
        name = os.path.basename(sys.executable if getattr(sys, "frozen", False)
                                else sys.argv[0])
        stem = os.path.splitext(name)[0].lower()
        return bool(stem) and stem in command.lower()


# ============================================================== PC 侧动作

def open_uri(uri: str) -> bool:
    """用系统默认处理程序打开 URI（例如 ms-settings:regionlanguage）。"""
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def launch_command(command: "list[str]") -> None:
    """启动一个应用（.lnk 交给 Shell，其它直接起进程，不阻塞客户端）。"""
    if len(command) == 1 and command[0].lower().endswith(".lnk"):
        os.startfile(command[0])  # type: ignore[attr-defined]
        return
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=False,
        creationflags=flags,
    )


# 鼠标按键 -> (按下事件, 抬起事件, XBUTTON 号)
_MOUSE_BUTTONS = {
    "left": (0x0002, 0x0004, 0),
    "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0),
    "x1": (0x0080, 0x0100, 1),
    "x2": (0x0080, 0x0100, 2),
}


def mouse_event(button: str, down: bool) -> bool:
    """发一次鼠标按下/抬起。失败返回 False。"""
    spec = _MOUSE_BUTTONS.get(button)
    if spec is None:
        return False
    down_flag, up_flag, xbutton = spec
    data = xbutton << 16 if xbutton else 0
    try:
        ctypes.windll.user32.mouse_event(down_flag if down else up_flag,
                                         0, 0, data, 0)
        return True
    except Exception:
        return False
