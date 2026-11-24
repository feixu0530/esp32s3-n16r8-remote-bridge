"""
自动选择烧录端口并烧录固件。

端口识别（按 VID/PID，不靠 COM 号 —— COM 号会随插拔变）：
  - CH343 (VID=0x1A86, PID=0x55D3)  -> 板载 UART，最稳
  - ESP32-S3 原生 USB (VID=0x303A)  -> CDC，被固件占用时走不通

流程：
  1. 找到 CH343 端口
  2. 发 DTR/RTS 脉冲让芯片进下载模式
  3. esptool 烧录
"""
import subprocess, sys, time, serial
import serial.tools.list_ports as lp

os = __import__('os')
PROJECT = r"C:/Users/Figo/Desktop/Dev/ESP32S3-all-remote/dev/firmware/bridge"
os.chdir(PROJECT)

env = dict(os.environ)
env["PATH"] = r"C:\Espressif\python_env\idf6.1_py3.11_env\Scripts;" + env.get("PATH", "")

ESPTOOL = r"C:\Espressif\python_env\idf6.1_py3.11_env\Scripts\esptool.exe"
BUILD = os.path.join(PROJECT, "build")

# ★★★ 正确的烧录偏移（来自 build/flash_args）★★★
#   0x0     bootloader
#   0x8000  partition table
#   0x10000 app
#
# ★ 之前误烧到 0x0 会用应用镜像覆盖 bootloader，
#   结果 ROM 启动时 SHA-256 校验失败 + 反复看门狗复位，
#   表现为设备根本起不来。
FLASH_ARGS = [
    ("0x0", os.path.join(BUILD, "bootloader", "bootloader.bin")),
    ("0x8000", os.path.join(BUILD, "partition_table", "partition-table.bin")),
    ("0x10000", os.path.join(BUILD, "remote_bridge.bin")),
]


def find_ch343():
    """找 CH343 串口（板载 UART，烧录最稳）。"""
    for p in lp.comports():
        if p.vid == 0x1A86:
            return p.device
    return None


def find_esp_usb():
    """找 ESP32-S3 原生 USB。"""
    for p in lp.comports():
        if p.vid == 0x303A:
            return p.device
    return None


print("=" * 66)
print("烧录 remote_bridge")
print("=" * 66)

print("当前串口：")
for p in lp.comports():
    print(f"  {p.device:8} VID=0x{p.vid:04X} PID=0x{p.pid:04X}  {p.description}")

PORT = find_ch343()
if PORT:
    print(f"\n使用板载 CH343 UART：{PORT}")
    try:
        s = serial.Serial(PORT, 115200, timeout=0.2)
        s.dtr = False; s.rts = False; time.sleep(0.15)
        s.dtr = True;  s.rts = True;  time.sleep(0.08)
        s.dtr = False; s.rts = False; time.sleep(0.05)
        s.close()
        print("复位脉冲已发送")
    except Exception as exc:
        print(f"复位脉冲失败（忽略）：{exc}")
    time.sleep(1.0)
else:
    usb = find_esp_usb()
    if usb:
        PORT = usb
        print(f"\n未找到 CH343，改用 ESP32-S3 原生 USB：{PORT}")
    else:
        print("\n错误：找不到可用的烧录端口")
        sys.exit(1)

try:
    # esptool v5 的 flash 选项必须放在 write-flash **之后**
    cmd = [ESPTOOL, "--chip", "esp32s3", "-b", "460800", "-p", PORT,
           # ★ 刷完不要立刻硬复位 ★（--after 是**全局**选项，放在子命令前面）
           #   以前是默认的 hard-reset：固件还没起来 USB 就重新枚举，
           #   Windows 抓到半枚举的设备 → 弹「无法识别的 USB 设备」
           #   （还会在设备管理器里留一条未连接的旧实例，刷一次多一条）。
           #   现在先不复位，等 USB 干净注销后自己发复位脉冲。
           "--after", "no-reset",
           "write-flash",
           "--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", "16MB"]
    for offset, path in FLASH_ARGS:
        cmd += [offset, path]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    print("=" * 66)
    lines = []
    if proc.stdout:
        lines.extend(proc.stdout.rstrip().split("\n"))
    if proc.stderr:
        lines.extend(proc.stderr.rstrip().split("\n"))
    for line in lines[-20:]:
        if line.strip():
            print(line)
    print("=" * 66)
    if proc.returncode == 0:
        print("烧录成功！")
        # ★ 干净复位 ★
        #   等 1.5 秒让 Windows 把 USB 设备干净注销，
        #   再发一次复位脉冲 —— 这样重新枚举的是**完整**的复合设备，
        #   不会再弹「无法识别的 USB 设备」。
        print("等 USB 干净断开…")
        time.sleep(1.5)
        try:
            s = serial.Serial(PORT, 115200, timeout=0.2)
            s.dtr = False; s.rts = False; time.sleep(0.15)
            s.dtr = True;  s.rts = True;  time.sleep(0.08)
            s.dtr = False; s.rts = False
            s.close()
            print("已复位，等板子重新枚举…")
        except Exception as exc:
            print(f"（复位脉冲失败，请手动拔插一次：{exc}）")
        # 板子复位 → USB 重新枚举 → Windows 会留下一条「未连接的旧实例」。
        # 顺手清一下（没管理员权限就只提示，不影响刷写）。
        try:
            import subprocess as _sp
            _sp.run([sys.executable, os.path.join(PROJECT, "cleanup_stale_devices.py")],
                     timeout=180)
        except Exception as _exc:
            print(f"（清理旧设备实例失败，忽略：{_exc}）")
    else:
        print(f"烧录失败，返回码 {proc.returncode}")
        # ★ 给明确的下一步指引 ★
        if PORT and PORT.startswith("COM"):
            pass
        out_text = "\n".join(lines)
        if "No serial data received" in out_text or "Failed to connect" in out_text:
            print()
            print("─" * 66)
            print("★ ESP32-S3 原生 USB（CDC）在进下载模式后**不可用**")
            print("  —— 这是芯片的限制，不是脚本的问题。")
            print()
            print("  请插上板载 CH343（USB 口旁边那颗 UART 芯片），")
            print("  然后重新运行本脚本。它会自动选 CH343 端口烧录。")
            print("─" * 66)
    sys.exit(proc.returncode)
except subprocess.TimeoutExpired:
    print("错误：烧录超时")
    sys.exit(2)
except Exception as exc:
    print(f"异常：{type(exc).__name__}: {exc}")
    sys.exit(3)
