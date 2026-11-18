"""
固件烧录：通过 USB CDC 让设备跳到下载模式，再用 esptool 烧写。

关键点：
  - ESP32-S3 的原生 USB 支持 USB-Serial-JTAG，esptool 可以直接通过它下载，
    不需要按 BOOT 键
  - esptool v5.x 的 API：esptool.main([...]) 或 python -m esptool
  - 烧录是耗时操作，必须放在子线程，通过回调汇报进度

固件文件格式：
  - 整包固件（merged/工厂固件）：一个 .bin，烧到 0x0
  - 分段固件：bootloader@0x0, partition@0x8000, app@0x10000
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

_logger = logging.getLogger(__name__)


@dataclass
class FlashTarget:
    """一个待烧录的固件文件及其偏移。"""

    path: Path
    offset: int = 0

    def to_args(self) -> List[str]:
        return [f"0x{self.offset:X}", str(self.path)]


@dataclass
class FlashResult:
    ok: bool = False
    message: str = ""
    output: str = ""


ProgressFn = Callable[[str], None]


def _find_idf_esptool() -> List[str]:
    """
    在 ESP-IDF 安装目录里找 esptool 可执行文件。

    ESP-IDF 的 Windows 安装把 python_env 放在 C:\\Espressif\\python_env\\<版本>\\，
    esptool 在其中的 Scripts/ 下。

    ★ 这是绿色版在没有内置 esptool 时的主要依赖 ——
      用户装了 ESP-IDF 就能刷固件（对开发者来说这是标配）。
    """
    out: List[str] = []
    roots = [Path(r"C:\Espressif\python_env")]
    # 也支持自定义的 IDF_TOOLS_PATH
    idf_tools = os.environ.get("IDF_TOOLS_PATH")
    if idf_tools:
        roots.append(Path(idf_tools) / "python_env")

    for root in roots:
        if not root.exists():
            continue
        try:
            env_dirs = sorted(root.iterdir(), reverse=True)
        except OSError:
            continue
        for env_dir in env_dirs:
            for name in ("esptool.exe", "esptool"):
                exe = env_dir / "Scripts" / name
                if exe.exists():
                    out.append(str(exe))
                    break
    return out


class FirmwareFlasher:
    """固件烧录器。"""

    def __init__(self, port: str):
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    # ------------------------------------------------------------ 固件自动发现

    @staticmethod
    def find_bundled_firmware() -> Optional[Path]:
        """
        找出随包分发的固件目录。

        ★ 这是「一键刷写」的关键 ——
        绿色版里 firmware/ 目录就在 EXE 同级，用户不需要自己去挑 .bin 文件。

        查找顺序：
          1. EXE 同级 firmware/            （绿色包）
          2. PyInstaller 的 _internal/firmware/
          3. 开发目录 dev/firmware/ble_hid_bridge/build/

        返回包含三个 bin 的目录，找不到返回 None。
        """
        candidates: List[Path] = []

        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            candidates.append(exe_dir / "firmware")
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / "firmware")
        else:
            # 开发目录：从 src/core/flasher.py 往上四级到项目根，再进固件工程
            here = Path(__file__).resolve()
            # here = <root>/dev/client/src/core/flasher.py
            # parents[0]=core  [1]=src  [2]=client  [3]=dev  [4]=root
            root = here.parents[4] if len(here.parents) > 4 else here.parent.parent.parent
            candidates.append(root / "dev" / "firmware" / "bridge" / "build")
            # 兜底：再往上找一层（万一目录结构变了）
            candidates.append(root.parent / "dev" / "firmware" / "bridge" / "build")

        for base in candidates:
            if not base.is_dir():
                continue
            # ★ 支持两种目录布局：
            #   开发版 build/:  bootloader/bootloader.bin
            #                    partition_table/partition-table.bin
            #                    ble_hid_bridge.bin
            #   绿色包 firmware/: 三个 bin 平铺在同一层
            boot = base / "bootloader" / "bootloader.bin"
            part = base / "partition_table" / "partition-table.bin"
            # ★ 应用镜像现在叫 remote_bridge.bin ★
            #   以前这里还找旧的 ble_hid_bridge.bin —— 早就没有这个文件了，
            #   所以打包版「全新刷写」连应用区都取不到。
            app = base / "remote_bridge.bin"
            if not app.exists():
                app = base / "ble_hid_bridge.bin"
            if not boot.exists():
                boot = base / "bootloader.bin"
            if not part.exists():
                part = base / "partition-table.bin"
            if app.exists() and boot.exists() and part.exists():
                return base
        return None

    @staticmethod
    def find_bundle_files(base: Path) -> List[Path]:
        """
        从固件目录里取出三个待烧录文件（顺序：bootloader/分区表/应用）。

        ★ 兼容两种布局：开发版的 build/（带子目录）与绿色包的 firmware/（平铺）。
        """
        boot = base / "bootloader" / "bootloader.bin"
        part = base / "partition_table" / "partition-table.bin"
        if not boot.exists():
            boot = base / "bootloader.bin"
        if not part.exists():
            part = base / "partition-table.bin"
        app = base / "remote_bridge.bin"
        if not app.exists():
            app = base / "ble_hid_bridge.bin"
        files = [
            boot,
            part,
            app,
        ]
        return [f for f in files if f.exists()]

    @staticmethod
    def bundle_version(base: Path) -> str:
        """读固件目录里的版本信息（如果有）。"""
        vfile = base / "VERSION.txt"
        if vfile.exists():
            try:
                return vfile.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        return ""

    # ------------------------------------------------------------ esptool 定位

    @staticmethod
    def find_esptool() -> Optional[List[str]]:
        """
        找出可用的 esptool 调用方式。

        ★ 打包版（frozen）优先**进程内调用**：
          绿色版里没有独立的 python.exe，`python -m esptool` 用不了。
          如果 esptool 已被收进 _internal，就用 `esptool.main(argv)` 直接跑。

        优先级（非打包版）：
          1. 当前 Python 环境的 esptool 模块
          2. ESP-IDF 自带的 esptool（Espressif 安装目录）
          3. 随包附带的 tools/esptool 目录
          4. PATH 上的 esptool 可执行文件
        """
        # ★ 打包版：先试进程内导入
        if getattr(sys, "frozen", False):
            try:
                import esptool  # noqa: F401
                return None      # None = 用进程内调用
            except ImportError:
                pass

        # 1) 当前 Python 环境的 esptool 模块
        try:
            import esptool  # noqa: F401
            return [sys.executable, "-m", "esptool"]
        except ImportError:
            pass

        # 2) ESP-IDF 自带的 esptool（Windows 上是 .exe）
        #    ★ 开发环境下用户很可能装过 ESP-IDF —— 那里的 esptool 版本与
        #      固件工具链匹配，是最可靠的选择。
        for exe in _find_idf_esptool():
            return [exe]

        # 3) 随包附带的 tools/esptool 目录（绿色版手工放置的情况）
        bases: List[Path] = []
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            bases.append(exe_dir)
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                bases.append(Path(meipass))
        else:
            here = Path(__file__).resolve()
            client_dir = here.parents[2]          # <root>/dev/client
            root = here.parents[4] if len(here.parents) > 4 else client_dir
            bases.append(client_dir)
            bases.append(root)

        for base in bases:
            cand = base / "tools" / "esptool"
            if cand.is_dir() and (cand / "__init__.py").exists():
                parent = str(cand.parent)
                if parent not in sys.path:
                    sys.path.insert(0, parent)
                return [sys.executable, "-m", "esptool"]

        # 4) PATH
        exe = shutil.which("esptool") or shutil.which("esptool.exe")
        if exe:
            return [exe]

        return None

    @staticmethod
    def esptool_available() -> bool:
        """能否找到可用的 esptool（用于 UI 提示）。"""
        if FirmwareFlasher.find_esptool() is not None:
            return True
        # 打包版可能是进程内调用
        if getattr(sys, "frozen", False):
            try:
                import esptool  # noqa: F401
                return True
            except ImportError:
                pass
        return False

    @staticmethod
    def esptool_diagnostics() -> str:
        """
        诊断 esptool 的可用性（写进启动日志，排查用）。

        ★ 绿色版没有控制台，日志是唯一的诊断渠道 ——
          用户报「一键刷写失败」时，第一件事就是看这行日志。
        """
        cmd = FirmwareFlasher.find_esptool()
        if cmd:
            return f"esptool: {' '.join(cmd)}"
        if getattr(sys, "frozen", False):
            try:
                import esptool
                return (f"esptool: 进程内调用 (v{getattr(esptool, '__version__', '?')})")
            except ImportError as exc:
                return f"esptool: 不可用 ({exc})"
        return "esptool: 不可用（未找到）"

    @staticmethod
    def esptool_hint() -> str:
        """找不到 esptool 时给用户的提示。"""
        return ("未找到 esptool。请任选一种：\n"
                "  1) 安装 ESP-IDF（自带 esptool）\n"
                "  2) pip install esptool\n"
                "  3) 把 esptool 放到程序目录的 tools/esptool/ 下")

    # ------------------------------------------------------------ 设备信息

    @staticmethod
    def detect_chip(port: str, timeout: float = 10.0) -> Dict[str, str]:
        """探测芯片信息（chip type / MAC / flash size）。"""
        cmd = FirmwareFlasher.find_esptool()
        if not cmd:
            raise RuntimeError("未找到 esptool，无法探测设备")
        args = cmd + ["--chip", "esp32s3", "--port", port, "chip-id"]
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        info: Dict[str, str] = {}
        m = re.search(r"Chip type:\s*(.+)", out)
        if m:
            info["chip"] = m.group(1).strip()
        m = re.search(r"Features:\s*(.+)", out)
        if m:
            info["features"] = m.group(1).strip()
        m = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", out)
        if m:
            info["mac"] = m.group(1)
        m = re.search(r"Detecting flash size.*?(\d+)\s*MB", out, re.S)
        if m:
            info["flash_mb"] = m.group(1)
        info["raw"] = out
        return info

    @staticmethod
    def list_ports() -> List[Dict[str, str]]:
        """列出可用串口。"""
        try:
            import serial.tools.list_ports as lp
        except ImportError:
            return []
        out = []
        for p in lp.comports():
            # ★ 必须带 vid ★
            #   调用方靠 vid 0x1A86 认出「板载 CH343 = 烧录通道」，
            #   没有 vid 就永远认不出来 → 烧录直接报「找不到可用的烧录端口」。
            out.append({"device": p.device, "description": p.description or "",
                        "hwid": p.hwid or "", "vid": p.vid, "pid": p.pid})
        return out

    # ------------------------------------------------------------ 烧录

    def reset_into_download_mode(self) -> bool:
        """
        DTR/RTS 脉冲让芯片进 ROM 下载模式。

        抄 dev/firmware/bridge/flash_auto.py 的做法 —— 板子上有自动复位电路，
        脉冲一下就进下载模式，esptool 才连得上。
        失败不致命：esptool 自己的 --before default-reset 也会再试一次。
        """
        try:
            import serial
            s = serial.Serial(self.port, 115200, timeout=0.2)
            try:
                s.dtr = False
                s.rts = False
                time.sleep(0.15)
                s.dtr = True
                s.rts = True
                time.sleep(0.08)
                s.dtr = False
                s.rts = False
                time.sleep(0.05)
            finally:
                s.close()
            return True
        except Exception as exc:
            _logger.info("复位脉冲失败（忽略）：%s", exc)
            return False

    def reset_to_app(self) -> bool:
        """
        把芯片从 ROM 下载模式放回应用程序。

        ★ 烧录失败时必须做这一步 ★
          烧录前会先 pulse 一下让芯片进下载模式（esptool 连得上）。
          万一烧录失败，芯片就**留在下载模式**里 ——
          固件不跑，原生 USB 的 CDC 端口根本不出现，
          用户看到「开发板不见了」，还以为板子坏了。
          这里用相反的脉冲把它放回去。
        """
        try:
            import serial
            s = serial.Serial(self.port, 115200, timeout=0.2)
            try:
                s.dtr = False
                s.rts = True
                time.sleep(0.12)
                s.dtr = False
                s.rts = False
                time.sleep(0.15)
            finally:
                s.close()
            return True
        except Exception as exc:
            _logger.info("放回应用模式失败（忽略）：%s", exc)
            return False

    def erase_flash(self, progress: Optional[ProgressFn] = None) -> FlashResult:
        """
        擦掉整片 flash。

        ★ 全新刷写用这个 ★
          会连 NVS 一起清掉 —— 遥控器配对表、按键映射、语音快捷键全没了。
          「仅更新应用」模式不走这里。
        """
        args = ["--chip", "esp32s3", "--port", self.port, "erase-flash"]
        cmd = self.find_esptool()
        if cmd is None:
            return self._flash_inprocess(args, progress)

        full = cmd + args
        if progress:
            progress("擦除整片 flash …")
        try:
            proc = subprocess.run(full, capture_output=True, text=True,
                                  timeout=300, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return FlashResult(False, "擦除超时（300s）")
        except Exception as exc:
            return FlashResult(False, f"擦除失败: {exc}")

        out = (proc.stdout or "") + (proc.stderr or "")
        if progress and out:
            progress(out)
        ok = proc.returncode == 0
        return FlashResult(ok, "擦除完成" if ok
                           else f"擦除失败（退出码 {proc.returncode}）", out)

    def flash(self, targets: List[FlashTarget], progress: Optional[ProgressFn] = None,
              baud: int = 460800, use_uart: bool = False) -> FlashResult:
        """
        同步烧录。调用方应放在子线程里。

        use_uart=True 时走 UART 通道（板子上的 CH343 串口）。
        这条通道在 TinyUSB 启用后依然可用（USB-Serial-JTAG 会被 TinyUSB 占用），
        是固件出问题时唯一的救援通道。
        """
        if not targets:
            return FlashResult(False, "没有待烧录的文件")
        cmd = self.find_esptool()
        if cmd is None and not getattr(sys, "frozen", False):
            return FlashResult(False, self.esptool_hint())

        for t in targets:
            if not t.path.exists():
                return FlashResult(False, f"文件不存在: {t.path}")

        args = [
            "--chip", "esp32s3",
            "--port", self.port,
            "-b", str(baud),
            "--before", "default-reset",
            "--after", "hard-reset",
            "write-flash",
        ]
        # ★ esptool v5 的 flash 选项必须放在 write-flash **之后** ——
        #   放在前面会报 "No such option '--flash-mode'"。
        args += [
            "--flash-mode", "dio",
            "--flash-freq", "80m",
            "--flash-size", "16MB",
        ]
        for t in targets:
            args += t.to_args()

        # ★ 打包版走**进程内调用** ——
        #   绿色版里没有独立的 python.exe，`python -m esptool` 用不了。
        if cmd is None:
            result = self._flash_inprocess(args, progress)
            if not result.ok:
                # ★ 进程内失败就换外部 esptool 再试一次 ★
                #   打包版缺 stub 数据（PyInstaller 不收 .json）是已知坑；
                #   原来只在输出里出现 "stub" 才退，别的错就白失败。
                fallback = self._external_esptool()
                if fallback:
                    if progress:
                        progress("进程内 esptool 失败，改用外部 esptool 重试："
                                 + " ".join(fallback))
                    return self._run_external(fallback + args, progress)
            return result

        return self._run_external(cmd + args, progress)

    @staticmethod
    def _external_esptool() -> Optional[List[str]]:
        """
        外部 esptool 的调用方式（进程内失败时的退路）。

        优先 `idf python -m esptool` —— 那个环境的 esptool 带完整的 stub 数据；
        其次是 ESP-IDF 装出来的 esptool.exe。
        """
        root = Path(r"C:\Espressif\python_env")
        if root.exists():
            try:
                env_dirs = sorted(root.iterdir(), reverse=True)
            except OSError:
                env_dirs = []
            for env_dir in env_dirs:
                py = env_dir / "Scripts" / "python.exe"
                if py.is_file():
                    return [str(py), "-m", "esptool"]
        for exe in _find_idf_esptool():
            return [exe]
        return None

    @staticmethod
    def _run_external(full: List[str],
                      progress: Optional[ProgressFn] = None) -> FlashResult:
        """跑外部 esptool 进程。"""
        if progress:
            progress(f"执行: {' '.join(full)}")
        try:
            proc = subprocess.run(full, capture_output=True, text=True,
                                  timeout=300, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return FlashResult(False, "烧录超时（300s）")
        except Exception as exc:
            return FlashResult(False, f"烧录失败: {exc}")

        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0 and "Hash of data verified" in out
        msg = "烧录成功" if ok else f"烧录失败（退出码 {proc.returncode}）"
        if not ok:
            # ★ 失败也要把芯片放回应用模式 ★
            #   不放回去，芯片就留在下载模式里，
            #   原生 USB 的 CDC 端口不出现 —— 用户以为板子坏了。
            self.reset_to_app()
        return FlashResult(ok, msg, out)

    @staticmethod
    def _flash_inprocess(args: List[str],
                         progress: Optional[ProgressFn] = None) -> FlashResult:
        """
        在当前进程里调用 esptool（打包版专用）。

        绿色版里没有独立的 Python 解释器，只能这样调：
            import esptool; esptool.main(argv)

        esptool 会往 stdout/stderr 写进度，这里临时重定向到管道收集，
        烧录结束后再交给调用方显示。
        """
        try:
            import esptool
        except ImportError:
            return FlashResult(False, "打包版里找不到 esptool 模块")

        if progress:
            progress(f"执行: esptool {' '.join(args)}")

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        code = 0
        try:
            sys.stdout = buf_out
            sys.stderr = buf_err
            try:
                esptool.main(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except Exception as exc:
                buf_err.write(f"\n{exc}\n")
                code = 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        out = buf_out.getvalue() + buf_err.getvalue()
        if progress and out:
            progress(out)
        ok = code == 0 and "Hash of data verified" in out
        msg = "烧录成功" if ok else f"烧录失败（退出码 {code}）"
        if not ok:
            # 失败也要把芯片放回应用模式（别留在下载模式里）
            self.reset_to_app()
        return FlashResult(ok, msg, out)

    def flash_async(self, targets: List[FlashTarget], progress: Optional[ProgressFn] = None,
                    baud: int = 460800) -> threading.Thread:
        """在子线程里烧录，返回线程对象。"""
        def _run() -> None:
            result = self.flash(targets, progress, baud)
            if progress:
                progress(("OK: " if result.ok else "ERR: ") + result.message)

        t = threading.Thread(target=_run, name="flasher", daemon=True)
        self._thread = t
        t.start()
        return t

    # ------------------------------------------------------------ 固件文件分组

    @staticmethod
    def classify_firmware(files: List[Path]) -> List[FlashTarget]:
        """
        把固件文件按约定分组，返回带偏移的烧录目标列表。

        识别规则（按文件名关键词）：
          bootloader*.bin   -> 0x0
          partition*.bin    -> 0x8000
          boot_app0*.bin    -> 0xE000
          app/merged/*.bin  -> 0x10000
        """
        targets: List[FlashTarget] = []
        app: Optional[Path] = None

        for f in files:
            name = f.name.lower()
            if "bootloader" in name:
                targets.append(FlashTarget(f, 0x0))
            elif "partition" in name:
                targets.append(FlashTarget(f, 0x8000))
            elif "boot_app0" in name:
                targets.append(FlashTarget(f, 0xE000))
            elif "merged" in name or "firmware" in name or "app" in name:
                app = f
            else:
                app = f

        if app is not None:
            targets.append(FlashTarget(app, 0x10000))
        return targets
