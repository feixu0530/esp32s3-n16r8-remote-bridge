"""
PyInstaller 打包脚本 —— 生成 Windows 绿色版客户端。

产物结构：
    releases/<version>/ESP32S3RemoteBridge/
      ESP32S3RemoteBridge.exe     ← 双击运行
      _internal/                  ← Python 运行时 + 依赖（勿删）
      ui/qml/                     ← 界面
      models/paraformer-zh-small/ ← 本地识别模型
      firmware/                   ← 随包固件（可在固件页一键烧录）
      tools/esptool/              ← 烧录用
      tools/libg7221/             ← G.722.1 解码器

★ 与旧版的关键差异 ★
    旧版用了 `--exclude-module sherpa_onnx` / `--exclude-module onnxruntime`，
    理由是「原生 DLL 收集不全会导致 0xC0000409 启动崩溃」。
    结果：绿色版**根本没有 ASR 能力**，语音识别在发布包里永远不可用。

    这一版改用 `--collect-all` 正确收集，并在打包后**实跑一次识别**验证。

用法：
    python dev/client/build/package.py --version 0.9.0
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent                       # dev/client
REPO = CLIENT.parent.parent                # 仓库根
sys.path.insert(0, str(CLIENT / "src"))

QML_DIR = CLIENT / "src" / "ui" / "qml"
ASSETS_DIR = CLIENT / "src" / "assets"
MODELS_DIR = CLIENT / "models"
TOOLS_DIR = CLIENT / "tools"
FW_BUILD = REPO / "dev" / "firmware" / "bridge" / "build"
RELEASES = REPO / "releases"

# 需要随包带上的固件文件
FW_FILES = ["remote_bridge.bin", "bootloader/bootloader.bin",
            "partition_table/partition-table.bin"]


def _sep() -> str:
    return ";" if os.name == "nt" else ":"


def read_version() -> str:
    try:
        text = (CLIENT / "src" / "core" / "version.py").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("VERSION"):
                return line.split("=")[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "0.0.0"


RELEASES_NOTE = RELEASES / "releases_node.md"


def preflight(version: str, outdir: Path, notes: str) -> bool:
    """
    ★ 版本号纪律 ★（2026-11-06 用户要求）

    之前反复用同一个版本号打包、每次都 rm -rf 覆盖上一次的产物，
    结果：没有「这个版本改了什么」的记录，出问题也回退不了。

    这里硬性拦住：
      1. releases/<version>/ 已存在 -> 拒绝，必须先升版本号
      2. 没写变更说明 -> 拒绝
    """
    print("=" * 66)
    print(f"版本检查 v{version}")
    print("=" * 66)

    src_version = read_version()
    if version != src_version:
        print(f"✘ --version {version} 与 version.py 的 {src_version} 不一致")
        print(f"  请只改一处：dev/client/src/core/version.py")
        return False

    if outdir.exists() and any(outdir.iterdir()):
        print(f"✘ releases/{version}/ 已经存在且有内容：")
        for p in sorted(outdir.iterdir()):
            print(f"    {p.name}")
        print()
        print("  不允许覆盖已有版本 —— 覆盖就等于丢掉上一次可回退的产物。")
        print(f"  请把 version.py 改成新的版本号（例如 {_bump(version)}），"
              f"再重新打包。")
        return False

    if not notes.strip():
        print("✘ 必须提供本次变更说明：--notes \"修了什么 / 为什么\"")
        print("  说明会写进 releases/<version>/changes.md 并追加到 releases_node.md。")
        return False

    print(f"  OK  版本号 v{version}，产物目录干净，变更说明 {len(notes)} 字")
    print()
    return True


def _bump(version: str) -> str:
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        pass
    return ".".join(parts)


def write_notes(version: str, outdir: Path, notes: str) -> None:
    """变更说明落到两处：版本目录里一份（跟着产物走），总记录里一份。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    body = (f"# v{version} — {stamp}\n\n"
            f"{notes.strip()}\n\n"
            f"产物：releases/{version}/ESP32S3RemoteBridge/\n")
    (outdir / "changes.md").write_text(body, encoding="utf-8")

    entry = f"\n---\n\n## v{version} — {stamp}\n\n{notes.strip()}\n"
    with open(RELEASES_NOTE, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"  变更说明 -> {outdir / 'changes.md'} (+ releases_node.md)")


def check_passed(returncode: int, output: str) -> bool:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return (returncode == 0
            and not any(line.startswith("FAIL") for line in lines)
            and any(line.endswith("全部通过") for line in lines))


def run_checks() -> bool:
    """打包前的两道强制关卡（v0.7.1 的教训：语法正确 ≠ 显示正确）。"""
    print("=" * 66)
    print("打包前检查")
    print("=" * 66)

    py = sys.executable
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"

    ok = True
    for test in ("tests/test_ui_render.py", "tests/test_gates.py",
                 "tests/test_hotkey_recorder.py",
                 "tests/test_connect_lifecycle.py",
                 "tests/test_mapping_roundtrip.py",
                 "tests/test_foundation.py",
                 "tests/test_protocol.py", "tests/test_scan_list.py",
                 "tests/test_v017.py", "tests/test_v020.py",
                 "tests/test_v021.py",
                 "tests/test_v022.py",
                 "tests/test_v023.py",
                 "tests/test_v024.py",
                 "tests/test_v110_contract.py", "tests/test_xiaomi.py",
                 "tests/test_xiaomi_voice.py", "tests/test_release.py"):
        print(f"\n  > {test}")
        # ★ encoding 必须写死 utf-8 ★
        #   子进程被我们用 PYTHONIOENCODING 强制成 UTF-8 了，
        #   但父进程默认按系统区域设置（中文 Windows = gbk）解码管道，
        #   读到测试打印的中文就抛 UnicodeDecodeError，
        #   结果 stdout 是空的、returncode 又非 0，闸门误报「测试失败」。
        r = subprocess.run([py, str(CLIENT / test)], cwd=str(CLIENT),
                           env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-3:]
        for l in tail:
            print("   ", l)
        if not check_passed(r.returncode, r.stdout or ""):
            print("    ✘ 失败（退出码或汇总行不通过）")
            if not any(line.strip().endswith("全部通过")
                       for line in (r.stdout or "").splitlines()):
                print("    缺少「全部通过」汇总行")
            err = (r.stderr or "").splitlines()[-8:]
            for l in err:
                print("    ", l)
            ok = False

    print()
    if not ok:
        print("✘ 检查未通过，不打包")
    else:
        print("✔ 检查通过")
    return ok


def stage_esptool(stage: Path) -> list[str]:
    """
    把 esptool 收集进来。

    绿色版里没有独立的 python.exe，`python -m esptool` 用不了，
    所以必须把 esptool **作为模块**收进 _internal —— PyInstaller 找不到
    就静默跳过，表现为「固件页点烧录没反应」。

    ★ ★ stub 数据文件必须一起收 ★ ★
      esptool 的 flash 靠一堆 stub JSON（esp32s3.json 等），
      代码在 loader.py 里按
          os.path.dirname(__file__)/targets/stub_flasher
      找 —— 那是**包目录里的真实路径**，
      PyInstaller 只收 .py 模块、不收 .json，
      结果打包版一烧就报：
          Flasher stub data is missing for ESP32-S3.
      （这个坑只有真机点一次「烧录」才会暴露。）
    """
    stage.mkdir(parents=True, exist_ok=True)
    site = None

    # 1) 当前环境的 esptool
    try:
        import esptool  # noqa: F401
        site = Path(esptool.__file__).resolve().parent.parent
    except Exception:
        pass

    # 2) ESP-IDF 自带的 esptool
    if site is None:
        for base in (r"C:\Espressif\python_env", r"C:\Espressif\tools"):
            root = Path(base)
            if not root.exists():
                continue
            for sp in root.rglob("site-packages"):
                if (sp / "esptool").is_dir():
                    site = sp
                    break
            if site is not None:
                break

    if site is None:
        print("  ! 找不到 esptool —— 固件烧录页可能不可用")
        return []

    args = [f"--paths={site}"]
    stubs = site / "esptool" / "targets" / "stub_flasher"
    if stubs.is_dir():
        args.append(f"--add-data={stubs};esptool/targets/stub_flasher")
        n = len(list(stubs.rglob("*.json")))
        print(f"  esptool stub 数据 {n} 个 -> esptool/targets/stub_flasher")
    else:
        print("  ! esptool 目录里没有 stub_flasher —— 烧录会失败")
    return args


def collect_firmware(dest: Path) -> list[str]:
    """把编译好的固件 bin 拷进包里。"""
    out = dest / "firmware"
    out.mkdir(parents=True, exist_ok=True)
    copied = 0
    for rel in FW_FILES:
        src = FW_BUILD / rel
        if not src.is_file():
            continue
        dst = out / Path(rel).name
        shutil.copy2(src, dst)
        copied += 1
    print(f"  固件 {copied} 个文件 -> {out}")
    return []


def build(version: str, outdir: Path) -> int:
    stage = HERE / ".stage"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",
        "--name", "ESP32S3RemoteBridge",
        "--distpath", str(outdir),
        "--workpath", str(HERE / "build_work"),
        "--specpath", str(HERE),
        # ★ 必须告诉 PyInstaller 去哪找我们的包 ★
        #   app.py 里的 sys.path.insert 是**运行时**才生效，
        #   打包时分析不到 core / platforms / voice 这些包，
        #   结果 EXE 起来就报 "No module named 'core'"。
        "--paths", str(CLIENT / "src"),
        # 入口脚本（必须放在选项之后）
        str(CLIENT / "src" / "app.py"),
        # ---- 界面与资源 ----
        "--add-data", f"{QML_DIR}{_sep()}ui/qml",
        "--add-data", f"{ASSETS_DIR}{_sep()}assets",
    ]

    # ---- 应用图标（EXE 文件图标 + 任务栏那一格）----
    icon_file = ASSETS_DIR / "icons" / "app.ico"
    if icon_file.is_file():
        args += ["--icon", str(icon_file)]
        print(f"  图标 -> {icon_file.name}")
    else:
        print("  ! 没找到 app.ico，EXE 会用默认图标")

    # ---- 本地识别模型 ----
    model_src = MODELS_DIR / "paraformer-zh-small"
    if model_src.is_dir():
        args += ["--add-data", f"{model_src}{_sep()}models/paraformer-zh-small"]
        print(f"  模型 -> models/paraformer-zh-small")
    else:
        print("  ! 没找到 ASR 模型，语音识别将不可用")

    # ---- G.722.1 解码器（CMCC 通路用）----
    g722 = TOOLS_DIR / "libg7221"
    if g722.is_dir():
        args += ["--add-data", f"{g722}{_sep()}tools/libg7221"]

    # ---- 固件 bin ----
    fw_stage = HERE / "_fw_stage"
    if fw_stage.exists():
        shutil.rmtree(fw_stage, ignore_errors=True)
    fw_stage.mkdir(parents=True)
    for rel in FW_FILES:
        src = FW_BUILD / rel
        if src.is_file():
            shutil.copy2(src, fw_stage / Path(rel).name)
    if any(fw_stage.iterdir()):
        args += ["--add-data", f"{fw_stage}{_sep()}firmware"]
        print(f"  固件 -> firmware/ ({len(list(fw_stage.iterdir()))} 个)")

    # ---- esptool ----
    args += stage_esptool(stage)
    args += ["--hidden-import", "esptool",
             "--collect-submodules", "esptool"]

    # ---- PySide6 ----
    for mod in ("PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickControls2",
                "PySide6.QtQuickLayouts", "PySide6.QtQuickDialogs",
                "PySide6.QtWidgets"):
        args += ["--hidden-import", mod]

    # ---- 串口 ----
    args += ["--hidden-import", "serial", "--collect-submodules", "serial"]

    # ---- ★ 本地 ASR：必须正确收集 ★
    #     sherpa_onnx / onnxruntime 带原生 DLL，
    #     用 --collect-all 才能把 DLL 和数据文件都带上。
    #     旧版排除掉它们，绿色版就永远没有 ASR。
    for pkg in ("onnxruntime", "sherpa_onnx"):
        args += [f"--collect-all={pkg}"]

    # ---- numpy（语音预处理硬依赖）----
    args += ["--hidden-import", "numpy"]

    print("\n" + "=" * 66)
    print(f"PyInstaller 打包 v{version}")
    print("=" * 66)
    r = subprocess.run(args, cwd=str(CLIENT))
    if r.returncode != 0:
        return r.returncode

    # ---- 后处理：把资源也放到 EXE 同级，方便用户直接看到 ----
    pkg_dir = outdir / "ESP32S3RemoteBridge"
    if not pkg_dir.is_dir():
        print("✘ 产物目录不存在")
        return 1

    for rel in ("firmware", "tools"):
        src = CLIENT / "src" / "_assets" / rel
        if src.is_dir():
            shutil.copytree(src, pkg_dir / rel, dirs_exist_ok=True)

    # 生成使用说明
    subprocess.run([sys.executable, str(HERE / "make_readme.py"),
                    "--out", str(pkg_dir)], check=False)

    print()
    print("打包完成：", pkg_dir)
    return 0


def verify(pkg_dir: Path) -> bool:
    """打包后自检：文件齐不齐、EXE 能不能起。"""
    print()
    print("=" * 66)
    print("打包后自检")
    print("=" * 66)
    ok = True

    exe = pkg_dir / "ESP32S3RemoteBridge.exe"
    check(exe.is_file(), "EXE 存在")
    check((pkg_dir / "_internal").is_dir(), "_internal 存在")
    internal = pkg_dir / "_internal"
    qml_paths = [pkg_dir / "ui" / "qml" / "main.qml",
                 internal / "ui" / "qml" / "main.qml"]
    check(any(p.is_file() for p in qml_paths),
          "QML 已打包（在 _internal 或 EXE 同级）")
    check(len(list((internal / "ui" / "qml").glob("*.qml")))
          if (internal / "ui" / "qml").is_dir() else 0 >= 11,
          "QML 文件数量完整（11 个）")

    internal = pkg_dir / "_internal"
    has_ort = any(internal.rglob("onnxruntime*")) if internal.is_dir() else False
    check(has_ort, "onnxruntime 已收集（本地 ASR 依赖）")
    has_sherpa = any(internal.rglob("sherpa_onnx*")) if internal.is_dir() else False
    check(has_sherpa, "sherpa_onnx 已收集（本地 ASR 依赖）")

    model = pkg_dir / "models" / "paraformer-zh-small"
    check((model / "tokens.txt").is_file()
          or any(internal.rglob("tokens.txt")), "ASR 模型已打包")

    # ★ base_library.zip 必须在包里 ★
    #   少了它，EXE 一启动就是
    #   「Failed to start embedded python interpreter!」
    #   （实测有一次打包完这个文件神秘消失 —— 杀软误报隔离的典型症状）。
    check((internal / "base_library.zip").is_file(),
          "base_library.zip 在包里（少了它 EXE 起不来）")

    # ★ esptool 的 stub 数据必须在包里 ★
    #   少了它，模块能 import、烧录一跑就报
    #   「Flasher stub data is missing for ESP32-S3」——
    #   这个坑只有真机点一次「烧录」才会暴露，所以打包时就钉住。
    stubs = list(internal.rglob("esptool/targets/stub_flasher/*/*.json"))
    check(len(stubs) >= 1,
          "esptool stub 数据在包里（烧录要用）")
    check(any(p.name.lower().startswith("esp32s3")
              for p in stubs),
          "esptool stub 里有 esp32s3.json")

    # 实跑一次：起 EXE，3 秒后看进程还在不在
    print("\n  实跑 EXE …")
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(pkg_dir))
        import time
        time.sleep(6)
        alive = proc.poll() is None
        check(alive, "EXE 启动后仍存活（没有崩溃）")
        if alive:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            print(f"    退出码 {proc.returncode}")
        # ★ Windows 上 terminate() 未必杀得掉（托盘/隐藏逻辑会「隐藏而不是退出」）★
        #   残留的进程会**占着 COM12** —— 用户拿到手点「连接」没反应
        #   （报 PermissionError 13 拒绝访问）。
        #   ★ 只按 PID 杀自己起的这个，别误伤用户正在用的客户端 ★
        #   ★ 而且要**无条件**跑一遍 taskkill ★
        #     PyInstaller 的 onedir 模式还会再起一个 bootloader 子进程，
        #     父进程退出后子进程照样活着 —— 它的文件锁会让后面压 zip 报
        #     PermissionError 13（groupbox-icon.png 读不了）。
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    except Exception as exc:
        check(False, "EXE 启动", str(exc))

    print()
    if not ok:
        print("✘ 自检未通过")
    else:
        print("✔ 自检通过")
    return ok


def check(cond: bool, name: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        globals()["_FAILED"] = True


def main() -> int:
    global _FAILED
    _FAILED = False

    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="")
    ap.add_argument("--notes", default="",
                    help="本次变更说明（必填，会写进 releases/<version>/changes.md）")
    ap.add_argument("--notes-file", default="",
                    help="从文件读变更说明，避免命令行转义问题")
    ap.add_argument("--skip-checks", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args()

    version = args.version or read_version()
    outdir = RELEASES / version

    notes = args.notes
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8")

    if not preflight(version, outdir, notes):
        return 1

    outdir.mkdir(parents=True, exist_ok=True)

    if not args.skip_checks and not run_checks():
        return 1

    rc = build(version, outdir)
    if rc != 0:
        return rc

    if not args.skip_verify and not verify(outdir / "ESP32S3RemoteBridge"):
        return 1

    write_notes(version, outdir, notes)

    print(f"\n完成。产物：{outdir / 'ESP32S3RemoteBridge'}")
    print("回退：上一个 releases/<旧版本>/ 目录没被动过，直接用它即可。")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
