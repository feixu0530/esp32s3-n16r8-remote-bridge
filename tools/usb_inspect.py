"""
USB 描述符检查工具（诊断用）。

用 ctypes 直接调 libusb-1.0.dll，绕开 pyusb 的后端探测。
作用：打印设备真实的配置描述符，排查 Windows 枚举失败的原因。

用法：
    python tools/usb_inspect.py [vid:pid]
    python tools/usb_inspect.py 303a:4009

libusb DLL 位置（按序尝试）：
    - LIBUSB_PATH 环境变量
    - C:/Program1/scrcpy-win64-v2.3.1/
    - PATH
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import c_char_p, c_int, c_uint8, c_void_p

# ---- libusb 常量 ----
LIBUSB_SUCCESS = 0
LIBUSB_TYPE_BULK = 2
LIBUSB_TYPE_INTERRUPT = 3

DESCRIPTOR_TYPES = {
    1: "DEVICE", 2: "CONFIG", 3: "STRING", 4: "INTERFACE", 5: "ENDPOINT",
    6: "DEVICE_QUALIFIER", 7: "CS_INTERFACE", 8: "CS_ENDPOINT",
    0x0B: "IAD", 0x21: "HID", 0x22: "REPORT",
}

DLL_CANDIDATES = [
    os.environ.get("LIBUSB_PATH", ""),
    r"C:\Program1\scrcpy-win64-v2.3.1",
    r"C:\Program Files\LibreOffice\program",
    os.path.dirname(sys.executable),
]


def load_libusb():
    """加载 libusb-1.0.dll。"""
    import glob
    for base in DLL_CANDIDATES:
        if not base:
            continue
        if os.path.isfile(base):
            candidates = [base]
        else:
            candidates = glob.glob(os.path.join(base, "libusb-1.0*.dll"))
        for path in candidates:
            try:
                os.add_dll_directory(os.path.dirname(path))
                dll = ctypes.CDLL(path)
                return dll, path
            except OSError:
                continue
    return None, None


def main() -> int:
    dll, dll_path = load_libusb()
    if not dll:
        print("找不到 libusb-1.0.dll")
        print("设置 LIBUSB_PATH 环境变量指向 DLL 所在目录")
        return 2
    print(f"libusb: {dll_path}")

    # libusb_init
    dll.libusb_init.argtypes = [ctypes.c_void_p]
    dll.libusb_init.restype = c_int
    ctx = c_void_p()
    if dll.libusb_init(ctypes.byref(ctx)) != LIBUSB_SUCCESS:
        print("libusb_init 失败")
        return 2

    dll.libusb_get_device.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
    dll.libusb_get_device.restype = c_void_p
    dll.libusb_open.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.libusb_open.restype = c_int
    dll.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.libusb_get_device_descriptor.restype = c_int
    dll.libusb_get_active_config_descriptor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.libusb_get_active_config_descriptor.restype = c_int
    dll.libusb_get_config_descriptor_by_value.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_void_p]
    dll.libusb_get_config_descriptor_by_value.restype = c_int
    dll.libusb_control_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
        ctypes.c_uint16, ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint32]
    dll.libusb_control_transfer.restype = c_int
    dll.libusb_close.argtypes = [ctypes.c_void_p]
    dll.libusb_exit.argtypes = [ctypes.c_void_p]
    dll.libusb_free_config_descriptor.argtypes = [ctypes.c_void_p]
    dll.libusb_free_device_list.argtypes = [
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)), ctypes.c_int]

    want_vid, want_pid = 0x303A, 0x4009
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        # 列出所有设备，找 VID_303A 的
        dll.libusb_get_device_list.argtypes = [
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
        dll.libusb_get_device_list.restype = ctypes.c_long
        n = dll.libusb_get_device_list(None)
        if n < 0:
            print("list failed"); dll.libusb_exit(ctx); return 2
        arr = (ctypes.POINTER(ctypes.c_void_p) * n)()
        n2 = dll.libusb_get_device_list(arr)
        if n2 < 0:
            print("list failed"); dll.libusb_exit(ctx); return 2
        print(f"共 {n2} 个设备：")
        for i in range(n2):
            d = arr[i]
            de = ctypes.create_string_buffer(18)
            dll.libusb_get_device_descriptor(d, de)
            vid = int.from_bytes(de.raw[8:10], "little")
            pid = int.from_bytes(de.raw[10:12], "little")
            cls = de.raw[4]
            mark = "  <== ESP32-S3" if vid == 0x303A else ""
            print(f"  {i:2d}: {vid:04x}:{pid:04x} class=0x{cls:02X}{mark}")
        dll.libusb_exit(ctx)
        return 0
    if len(sys.argv) > 1:
        try:
            a, b = sys.argv[1].split(":")
            want_vid, want_pid = int(a, 16), int(b, 16)
        except ValueError:
            pass

    target = dll.libusb_get_device(ctx, want_vid, want_pid)
    if not target:
        print(f"没找到设备 {want_vid:04x}:{want_pid:04x}")
        dll.libusb_exit(ctx)
        return 1

    handle = c_void_p()
    if dll.libusb_open(target, ctypes.byref(handle)) != LIBUSB_SUCCESS:
        print("libusb_open 失败（设备可能被其它程序独占）")
        dll.libusb_exit(ctx)
        return 2

    print(f"=== 设备 {want_vid:04x}:{want_pid:04x} ===")
    desc = ctypes.create_string_buffer(18)
    dll.libusb_get_device_descriptor(target, desc)
    bcd = int.from_bytes(desc.raw[2:4], "little")
    print(f"bcdUSB {bcd >> 8}.{bcd & 0xff:02x}  "
          f"bDeviceClass 0x{desc.raw[4]:02X}  "
          f"bDeviceSubClass 0x{desc.raw[5]:02X}  "
          f"bDeviceProtocol 0x{desc.raw[6]:02X}  "
          f"bNumConfigurations {desc.raw[17]}")

    dll.libusb_close(handle)
    dll.libusb_exit(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
