"""
读取 USB 设备的原始描述符（不需要 Windows 装驱动）。

原理：Windows 的 USB 栈已经读过描述符并缓存了，但当某个接口
（如 CDC）因为驱动问题被标记为失败时，libusb/SetupAPI 就看不到设备。
这里用「自己发控制传输」的方式绕过：直接对 USB 控制器发 URBs，
读回设备真实的配置描述符。

这条路太底层（要直接操作 USB 驱动），所以这里用另一个思路：
用 Windows 的 WinUSB API —— 但需要设备绑定 WinUSB 驱动。

更实用的替代方案：**用 Zadig 换成 WinUSB 驱动**，然后任何工具都能读。
本脚本只负责用 WinUSB 读描述符，需要先绑定。

    python tools/usb_inspect.py --winusb

前提：设备已用 Zadig 绑定 WinUSB 驱动（Interface 0 → WinUSB）。

如果 WinUSB 也读不到，说明设备本身没有响应 —— 那就是固件侧的问题。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import c_void_p, c_uint8, c_ulong

# ---- WinUSB 常量 ----
WINUSB_INTERFACE_TYPE = 0
USB_TRANSFER_TYPE_CONTROL = 0

# USB 描述符请求结构
class USB_DESCRIPTOR_REQUEST(ctypes.Structure):
    _fields_ = [
        ("wLength", ctypes.c_ushort),
        ("wIndex", ctypes.c_ushort),
        ("wValue", ctypes.c_ushort),
        ("wInterface", ctypes.c_ushort),
        ("RequestType", c_uint8),
        ("Request", c_uint8),
        ("Reserved", ctypes.c_ushort),
    ]


def open_winusb(interface_path: str):
    """打开 WinUSB 接口。"""
    setupapi = ctypes.WinDLL("setupapi")
    winusb = ctypes.WinDLL("winusb")

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong]

    GUID_DEVINTERFACE_USB_DEVICE = bytes([
        0x10, 0xBF, 0xDC, 0xA5, 0x30, 0x65, 0xD2, 0x11,
        0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED])

    g = ctypes.create_string_buffer(GUID_DEVINTERFACE_USB_DEVICE, 16)
    devs = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(g), None, None, 0x02 | 0x10)   # PRESENT | DEVICEINTERFACE
    if devs == ctypes.c_void_p(-1).value:
        raise RuntimeError("SetupDiGetClassDevs 失败")

    class SP_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong),
                    ("InterfaceClassGuid", ctypes.c_byte * 16),
                    ("Flags", ctypes.c_ulong),
                    ("Reserved", ctypes.POINTER(ctypes.c_ulong))]

    detail = SP_INTERFACE_DATA()
    detail.cbSize = ctypes.sizeof(SP_INTERFACE_DATA)
    if not setupapi.SetupDiEnumDeviceInterfaces(
            devs, None, 0, ctypes.byref(detail)):
        raise RuntimeError("SetupDiEnumDeviceInterfaces 失败")

    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_INTERFACE_DATA), ctypes.c_void_p,
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    need = ctypes.c_ulong(0)
    setupapi.SetupDiGetDeviceInterfaceDetailW(
        devs, ctypes.byref(detail), None, 0, ctypes.byref(need), None)
    buf = ctypes.create_string_buffer(need.value)
    if not setupapi.SetupDiGetDeviceInterfaceDetailW(
            devs, ctypes.byref(detail), buf, need.value, ctypes.byref(need), None):
        raise RuntimeError("SetupDiGetDeviceInterfaceDetailW 失败")

    path = ctypes.wstring_at(buf, (need.value - 8) // 2)
    print(f"接口路径: {path}")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]

    h = kernel32.CreateFileW(path, 0, 0, None, 3, 0, None)
    if h == ctypes.c_void_p(-1).value:
        raise RuntimeError("CreateFileW 失败（设备可能没绑 WinUSB 驱动）")

    winusb.WinUsb_Initialize.restype = ctypes.c_bool
    winusb.WinUsb_Initialize.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    usb_ctx = ctypes.c_void_p()
    if not winusb.WinUsb_Initialize(h, ctypes.byref(usb_ctx)):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(h)
        raise RuntimeError(f"WinUsb_Initialize 失败 (err={err})")
    return h, usb_ctx, winusb, kernel32


def get_config_descriptor(usb_ctx, winusb):
    """用 WinUSB 读配置描述符。"""
    winusb.WinUsb_GetDescriptor.restype = ctypes.c_bool
    winusb.WinUsb_GetDescriptor.argtypes = [
        ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ushort,
        ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    buf = ctypes.create_string_buffer(512)
    actual = ctypes.c_ulong(0)
    # USB_DESCRIPTOR_CONFIGURATION = 0x02
    ok = winusb.WinUsb_GetDescriptor(
        usb_ctx, 0, 0x02, 0, 0x0409, 0, ctypes.cast(buf, c_void_p),
        ctypes.byref(actual))
    if not ok:
        return None
    return buf.raw[:actual.value]


def main() -> int:
    print(__doc__)
    try:
        h, ctx, winusb, kernel32 = open_winusb("")
    except Exception as exc:
        print(f"失败: {exc}")
        return 1

    raw = get_config_descriptor(ctx, winusb)
    if not raw:
        print("读配置描述符失败")
        return 2

    names = {2: "CONFIG", 4: "INTERFACE", 5: "ENDPOINT", 7: "CS_INTERFACE",
             0x0B: "IAD", 0x21: "HID", 0x22: "REPORT"}
    i = 0
    while i < len(raw):
        bl = raw[i]
        if bl == 0:
            break
        dt = raw[i + 1]
        extra = ""
        if dt == 2:
            extra = f" num_iface={raw[i+4]} attr=0x{raw[i+7]:02x}"
        elif dt == 4:
            extra = (f" iface={raw[i+2]} nEP={raw[i+4]} "
                     f"class=0x{raw[i+5]:02X} sub=0x{raw[i+6]:02X} "
                     f"proto={raw[i+7]} str={raw[i+8]}")
        elif dt == 5:
            kind = {0: "OUT", 1: "IN"}.get(raw[i + 2] & 0x03, "?")
            extra = (f" addr=0x{raw[i+2]:02X} {kind} attr=0x{raw[i+3]:02X} "
                     f"pkt={raw[i+4]} iv={raw[i+7]}")
        elif dt == 0x0B:
            extra = (f" first={raw[i+2]} count={raw[i+3]} "
                     f"class=0x{raw[i+4]:02X} sub=0x{raw[i+5]:02X} "
                     f"proto=0x{raw[i+6]:02X}")
        print(f"@{i:3d} len={bl:2d} {names.get(dt, hex(dt)):12s}{extra}")
        i += bl
    return 0


if __name__ == "__main__":
    sys.exit(main())
