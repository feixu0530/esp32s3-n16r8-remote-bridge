"""
补齐发布目录里 package.py 不管的那部分。

package.py 只产出「客户端目录 + changes.md」，
win64 压缩包、源码包、固件、校验和这些得另外打 —— 这里一次做完，
避免每次发布都手工拼、拼漏。

★ 校验和的规矩（用户定）★
    1. 用 -print0 生成，文件名里有空格/中文也不会错
    2. 文件必须是 LF 换行（CRLF 会让 sha256sum -c 在别的机器上读不懂）
    3. 生成完必须自己跑一遍 sha256sum -c 自检，不通过不算完

用法：
    python dev/client/build/make_release.py --version 1.1.0
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent
REPO = CLIENT.parent.parent
RELEASES = REPO / "releases"
FW_BUILD = REPO / "dev" / "firmware" / "bridge" / "build"


def log(msg: str) -> None:
    print(msg, flush=True)


def copy_docs(outdir: Path, version: str) -> None:
    for name in ("LICENSE", "THIRD_PARTY.md"):
        src = REPO / name
        if not src.is_file():
            raise SystemExit(f"缺少发布文档：{src}")
        shutil.copy2(src, outdir / name)
        log(f"  复制 {name}")

    readme = REPO / "README.md"
    if not readme.is_file():
        raise SystemExit(f"缺少发布文档：{readme}")
    text = readme.read_text(encoding="utf-8")
    (outdir / "README.md").write_text(text, encoding="utf-8", newline="\n")
    log(f"  生成 README.md（来自仓库当前版本 {version}）")

    manual = REPO / "docs" / "使用手册.md"
    if not manual.is_file():
        raise SystemExit(f"缺少发布手册：{manual}")
    manual_text = manual.read_text(encoding="utf-8")
    (outdir / f"MANUAL-{version}-zh.md").write_text(
        manual_text, encoding="utf-8", newline="\n")
    log(f"  生成 MANUAL-{version}-zh.md")


def copy_firmware(outdir: Path, version: str) -> None:
    fw_dir = outdir / "firmware"
    fw_dir.mkdir(exist_ok=True)
    src = FW_BUILD / "remote_bridge.bin"
    if not src.exists():
        raise SystemExit(f"找不到固件：{src}（先跑 build_fw.py）")
    dst = fw_dir / f"remote_bridge-{version}.bin"
    shutil.copy2(src, dst)
    shutil.copy2(src, outdir / dst.name)
    log(f"  固件 → {dst.name} + firmware/{dst.name}（{dst.stat().st_size} 字节）")


def copy_phone_apk(outdir: Path, version: str) -> None:
    """把改好的手机端 APK 一起放进发布目录。

    应用基于 movo 改写（见 THIRD_PARTY.md），现名 Fremote。
    连同签名用的 keystore 一起给出去 —— 以后重打包还得用它，
    换密钥签名的话手机上就得先卸载。
    """
    src_dir = Path(os.environ.get("MOTO_APK_DIR", ""))
    if not src_dir:
        raise SystemExit("缺少 MOTO_APK_DIR：正式发布必须提供当前版本 APK 来源")
    apk_src = src_dir / f"Fremote-{version}.apk"
    if not apk_src.is_file():
        raise SystemExit(f"找不到当前版本 APK：{apk_src}")
    shutil.copy2(apk_src, outdir / apk_src.name)
    print("  复制 %s" % apk_src.name)
    keystore = src_dir / "movo.keystore"
    if keystore.is_file():
        shutil.copy2(keystore, outdir / keystore.name)
        print("  复制 %s" % keystore.name)


def zip_win64(outdir: Path, version: str) -> Path:
    app = outdir / "ESP32S3RemoteBridge"
    zp = outdir / f"ESP32S3RemoteBridge-{version}-win64.zip"
    # ★ 文件可能被占用 ★
    #   打包自检会真的起一次 EXE，它（以及它的 bootloader 子进程）会占着
    #   _internal 里的文件；进程刚被杀时，Windows 释放锁还要一小会儿。
    #   遇到 PermissionError 就等一下重试，别直接失败。
    deadline = time.time() + 60
    while True:
        try:
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=1) as z:
                for path in sorted(app.rglob("*")):
                    if path.is_dir():
                        continue
                    z.write(path, str(Path(app.name) / path.relative_to(app)))
            break
        except PermissionError as exc:
            if time.time() > deadline:
                raise SystemExit(f"压缩绿色版失败（文件一直被占用）：{exc}")
            print("  文件被占用，等一下重试 …")
            time.sleep(5)
    log(f"  绿色版 → {zp.name}（{zp.stat().st_size} 字节）")
    return zp


def zip_src(outdir: Path, version: str, prev: Path) -> Path:
    zp = outdir / f"ESP32S3-all-remote-{version}-src.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for name in (".gitignore",):
            src = REPO / name
            if src.exists():
                z.write(src, name)
        for name in ("LICENSE", "THIRD_PARTY.md", "README.md"):
            src = REPO / name
            if src.exists():
                z.write(src, name)
        rel_readme = outdir / "README.md"
        if rel_readme.exists():
            z.write(rel_readme, "RELEASE_README.md")
        for top in ("dev", "tools", "docs"):
            base = REPO / top
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_dir():
                    continue
                # 别把打包中间产物、缓存塞进源码包
                rel = path.relative_to(REPO)
                excluded = {"__pycache__", "build", ".venv", ".pytest_cache",
                            "dist", "dist_scratch", "work_scratch", "build_work",
                            "node_modules", "models", "temp", "logs"}
                if rel.parts[:3] == ("dev", "client", "build"):
                    excluded.discard("build")
                if any(p in excluded or p.startswith("build_")
                       for p in rel.parts):
                    continue
                # ★ 二进制与大文件不进源码包 ★
                #   ASR 模型、固件、依赖 DLL 都是几十上百 MB 的东西，
                #   塞进源码包会让它变成几百 MB —— 源码包只放源码。
                if rel.name.startswith(("_patch", "notes")) or \
                        rel.suffix.lower() == ".spec":
                    continue
                if path.suffix.lower() in (".pyc", ".bin", ".elf", ".onnx",
                                          ".wav", ".exe", ".pyd", ".dll",
                                          ".zip", ".pth", ".so", ".log",
                                          ".key", ".pem", ".p12", ".pfx"):
                    continue
                try:
                    if path.stat().st_size > 2 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                z.write(path, str(path.relative_to(REPO)))
    log(f"  源码包 → {zp.name}（{zp.stat().st_size} 字节）")
    return zp


def make_sums(outdir: Path) -> None:
    """
    生成 SHA256SUMS.txt。

    ★ LF 换行 + -print0 ★
      路径里有中文（使用说明.txt），必须用 -print0 传给 sha256sum；
      换行符必须是 LF —— CRLF 会让 sha256sum -c 找不到文件。
    """
    sums = outdir / "SHA256SUMS.txt"
    if sums.exists():
        sums.unlink()
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    # ★ 路径里有中文（使用说明.txt）⇒ 子进程输出必须按 UTF-8 解码 ★
    #   用系统默认的 GBK 解码会直接 UnicodeDecodeError，校验和就生成不出来。
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        ["bash", "-lc",
         "find . -type f ! -name SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum"],
        cwd=outdir, capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"生成校验和失败：{(proc.stderr or '')[-500:]}")
    data = proc.stdout.replace("\r\n", "\n")
    sums.write_bytes(data.encode("utf-8"))
    log(f"  校验和 → SHA256SUMS.txt（{len(data.splitlines())} 条）")

    # ★ 自检：不通过不算完 ★
    check = subprocess.run(["bash", "-lc", "sha256sum -c SHA256SUMS.txt --quiet"],
                           cwd=outdir, capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="replace")
    if check.returncode != 0:
        print((check.stdout or "")[-2000:])
        print((check.stderr or "")[-2000:])
        raise SystemExit("校验和自检不通过")
    log("  校验和自检 sha256sum -c 通过")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--no-apk", action="store_true",
                    help="本次只发 PC 客户端，不打包手机 APK")
    ap.add_argument("--fw-version", default="",
                    help="固件版本号；默认跟发布版本一致（客户端单独修 Bug 时可指定旧值）")
    args = ap.parse_args()
    version = args.version
    fw_version = args.fw_version or version

    outdir = RELEASES / version
    if not outdir.exists():
        raise SystemExit(f"没有 releases/{version}/ 目录，先跑 package.py")
    prev = RELEASES / "1.0.0"

    log(f"=== 补齐 releases/{version} ===")
    copy_docs(outdir, version)
    copy_firmware(outdir, fw_version)
    if args.no_apk:
        log("  跳过手机 APK（--no-apk）")
    else:
        copy_phone_apk(outdir, version)
    zip_win64(outdir, version)
    zip_src(outdir, version, prev)
    make_sums(outdir)
    log("=== 完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
