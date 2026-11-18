#!/usr/bin/env python3
"""
用 libusb 转储 ESP32-S3 复合设备的**真实运行时** USB 描述符。

用法：
    python tools/usb_desc_dump.py            # 找 VID_303A 的设备并转储
    python tools/usb_desc_dump.py 303a:0002  # 指定 VID:PID

用途：对比本项目固件与 CMCC 固件的接口/端点分配差异。
"""
from __future__ import annotations

import ctypes
import glob
import os
import sys

DLL_CANDIDATES = [
    os.environ.get("LIBUSB_PATH", ""),
    r"C:\Program1\scrcpy-win64-v2.3.1",
    r"C:\Program Files\LibreOffice\program",
    os.path.dirname(sys.executable),
]

DESC_TYPES = {
    0x01: "DEVICE", 0x02: "CONFIG", 0x03: "STRING", 0x04: "INTERFACE",
    0x05: "ENDPOINT", 0x06: "CS_INTERFACE", 0x0B: "IAD", 0x21: "HID",
}


def load_libusb():
    for base in DLL_CANDIDATES:
        if not base:
            continue
        cands = [base] if os.path.isfile(base) else \
            glob.glob(os.path.join(base, "libusb-1.0*.dll"))
        for path in cands:
            try:
                os.add_dll_directory(os.path.dirname(path))
                return ctypes.CDLL(path), path
            except OSError:
                continue
    return None, None


def main() -> int:
    dll, dll_path = load_libusb()
    if not dll:
        print("找不到 libusb-1.0.dll")
        return 2
    print(f"libusb: {dll_path}\n")

    want_vid, want_pid = 0x303A, 0x0002
    if len(sys.argv) > 1 and ":" in sys.argv[1]:
        a, b = sys.argv[1].split(":")
        want_vid, want_pid = int(a, 16), int(b, 16)

    dll.libusb_init.argtypes = [ctypes.c_void_p]
    dll.libusb_init.restype = ctypes.c_int
    ctx = ctypes.c_void_p()
    if dll.libusb_init(ctypes.byref(ctx)) != 0:
        print("libusb_init 失败")
        return 2

    dll.libusb_get_device.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
    dll.libusb_get_device.restype = ctypes.c_void_p
    dll.libusb_open.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.libusb_open.restype = ctypes.c_int
    dll.libusb_get_config_descriptor.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_void_p, ctypes.c_int]
    dll.libusb_get_config_descriptor.restype = ctypes.c_int
    dll.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.libusb_get_device_descriptor.restype = ctypes.c_int

    dev = dll.libusb_get_device(ctx, want_vid, want_pid)
    if not dev:
        print(f"没找到设备 {want_vid:04x}:{want_pid:04x}")
        dll.libusb_exit(ctx)
        return 1

    handle = ctypes.c_void_p()
    if dll.libusb_open(dev, ctypes.byref(handle)) != 0:
        print("libusb_open 失败（设备可能被串口程序占用）")
        dll.libusb_exit(ctx)
        return 2

    ddesc = ctypes.create_string_buffer(18)
    dll.libusb_get_device_descriptor(dev, ddesc)
    bcd = int.from_bytes(ddesc.raw[2:4], "little")
    print(f"=== 设备 {want_vid:04x}:{want_pid:04x} ===")
    print(f"bcdUSB {bcd >> 8}.{bcd & 0xFF:02x}  "
          f"bDeviceClass 0x{ddesc.raw[4]:02X}  "
          f"bDeviceSubClass 0x{ddesc.raw[5]:02X}  "
          f"bDeviceProtocol 0x{ddesc.raw[6]:02X}")

    buf = ctypes.create_string_buffer(512)
    rc = dll.libusb_get_config_descriptor(dev, 0, buf, 512)
    if rc != 0:
        print(f"取配置描述符失败: {rc}")
        dll.libusb_close(handle)
        dll.libusb_exit(ctx)
        return 2

    raw = buf.raw
    total = raw[2] | (raw[3] << 8)
    nif = raw[4]
    print(f"配置: total_len={total} num_interfaces={nif} "
          f"attr=0x{raw[7]:02X} mA={raw[8] | (raw[9] << 8)}\n")

    p = 0
    while p < total:
        ln = raw[p]
        if ln == 0:
            break
        typ = raw[p + 1]
        nm = DESC_TYPES.get(typ, f"0x{typ:02X}")
        extra = ""
        if typ == 0x04:
            extra = (f"itf={raw[p+2]} alt={raw[p+3]} nEp={raw[p+4]} "
                     f"class=0x{raw[p+5]:02X} sub=0x{raw[p+6]:02X}")
        elif typ == 0x05:
            addr = raw[p + 2]
            attr = raw[p + 3]
            mps = raw[p + 4] | (raw[p + 5] << 8)
            extra = (f"addr=0x{addr:02X} {'IN ' if addr & 0x80 else 'OUT'} "
                     f"attr=0x{attr:02X} mps={mps}")
        elif typ == 0x0B:
            extra = (f"first_itf={raw[p+2]} count={raw[p+3]} "
                     f"class=0x{raw[p+4]:02X} sub=0x{raw[p+5]:02X} "
                     f"proto=0x{raw[p+6]:02X}")
        elif typ == 0x06:
            extra = f"subtype=0x{raw[p+2]:02X}"
        elif typ == 0x21:
            extra = (f"bcdHID=0x{raw[p+3]:02X}{raw[p+2]:02X} "
                     f"country={raw[p+4]} nDesc={raw[p+5]} "
                     f"ep=0x{raw[p+6]:02X}")
        print(f"  [{p:3d}] {nm:<13}{extra}")
        p += ln

    dll.libusb_close(handle)
    dll.libusb_exit(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
