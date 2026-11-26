"""
发布物一致性自查（源码 / 固件 bin / APK 资源 / 绿色版）。

用途：每次 `make_release.py` 之后跑一遍，确认「发出去的包」确实带着
本次的改动，而不是打了个旧产物。

用法：
    python tools/check_release.py            # 查 releases/ 里版本号最大的一个
    python tools/check_release.py 1.3.3      # 指定版本

检查项：
    1. firmware/remote_bridge-<版本>.bin 与 dev/firmware/bridge/build/ 同哈希
    2. 发布根目录固件与 firmware/ 同哈希，且两个 bin 都含对应版本字符串
    3. firmware-wifi/remote_bridge.bin 与 build_wifi/ 同哈希（若存在）
    4. APK 里的 setup.html / touchpad.html / i18n.js 是当前仓库版本
    5. 绿色版（_internal/firmware）里的固件与 firmware/ 同哈希
    6. 源码 ZIP 含发布脚本，且脚本内容与当前仓库一致
    7. SHA256SUMS.txt 自检
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "releases"
FW_BUILD = ROOT / "dev" / "firmware" / "bridge" / "build"
FW_WIFI = ROOT / "dev" / "firmware" / "bridge" / "build_wifi"
SRC_HTML = {
    "assets/setup.html": ROOT / "dev" / "firmware" / "bridge" / "apk" / "setup.html",
    "assets/touchpad.html": ROOT / "dev" / "firmware" / "bridge" / "web" / "touchpad.html",
    "assets/i18n.js": ROOT / "dev" / "firmware" / "bridge" / "web" / "static" / "js" / "i18n.js",
}

FAILS: list[str] = []


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  PASS", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, detail)


def latest_version() -> str:
    cand = []
    for d in RELEASES.iterdir():
        if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", d.name):
            cand.append(tuple(int(x) for x in d.name.split(".")))
    if not cand:
        raise SystemExit("releases/ 下没有版本目录")
    return ".".join(str(x) for x in max(cand))


def fix_html(path: Path) -> str:
    """与 pack_apk_assets.py 的改写保持一致（只做会影响内容比对的部分）。"""
    sys.path.insert(0, str(ROOT / "dev" / "firmware" / "bridge" / "tools"))
    import pack_apk_assets as pa
    return pa.fix_html(path.read_text(encoding="utf-8"))


def ble_firmware_path(rel: Path, ver: str) -> Path:
    return rel / "firmware" / f"remote_bridge-{ver}.bin"


def root_firmware_path(rel: Path, ver: str) -> Path:
    return rel / f"remote_bridge-{ver}.bin"


def source_archive_path(rel: Path, ver: str) -> Path:
    return rel / f"ESP32S3-all-remote-{ver}-src.zip"


def main() -> int:
    ver = sys.argv[1] if len(sys.argv) > 1 else latest_version()
    rel = RELEASES / ver
    if not rel.is_dir():
        raise SystemExit(f"没有 {rel}")
    print(f"=== 审计 releases/{ver} ===")

    print("[固件]")
    fw = ble_firmware_path(rel, ver)
    root_fw = root_firmware_path(rel, ver)
    wf = rel / "firmware-wifi" / "remote_bridge.bin"
    check("BLE 固件与 build/ 同哈希", fw.is_file() and fw.read_bytes() == (FW_BUILD / "remote_bridge.bin").read_bytes())
    check("发布根目录固件与 firmware/ 同哈希",
          root_fw.is_file() and fw.is_file() and root_fw.read_bytes() == fw.read_bytes())
    tail = ver.encode()
    check("BLE 固件含版本字符串", fw.is_file() and tail in fw.read_bytes())
    check("发布根目录固件含版本字符串", root_fw.is_file() and tail in root_fw.read_bytes())
    if wf.is_file():
        check("WiFi 固件与 build_wifi/ 同哈希", wf.read_bytes() == (FW_WIFI / "remote_bridge.bin").read_bytes())
        check("WiFi 固件含版本字符串", tail in wf.read_bytes())
    else:
        print("  SKIP WiFi 固件（已停用，不再随包发布）")

    print("[APK]")
    apks = sorted(rel.glob(f"Fremote-{ver}*.apk")) or sorted(rel.glob(f"Movo-{ver}*.apk"))
    check("APK 存在", bool(apks))
    if apks:
        with zipfile.ZipFile(apks[0]) as z:
            for arc, src in SRC_HTML.items():
                try:
                    got = z.read(arc).decode("utf-8")
                except KeyError:
                    check(f"APK 内 {arc} 存在", False)
                    continue
                # pack_apk_assets：touchpad/keyboard 走 fix_html 改写；
                # setup.html 是原样复制（它本来就不在 web/ 下）
                if arc == "assets/setup.html":
                    want = src.read_text(encoding="utf-8")
                elif arc.endswith(".html"):
                    want = fix_html(src)
                else:
                    want = src.read_text(encoding="utf-8")
                check(f"APK 内 {arc} 与仓库一致", got == want)

    print("[绿色版]")
    exe_fw = rel / "ESP32S3RemoteBridge" / "_internal" / "firmware" / "remote_bridge.bin"
    check("绿色版固件与 firmware/ 同哈希", exe_fw.is_file() and fw.is_file() and exe_fw.read_bytes() == fw.read_bytes())

    print("[源码包]")
    src_zip = source_archive_path(rel, ver)
    check("源码 ZIP 存在", src_zip.is_file())
    required_sources = {
        "dev/client/build/make_release.py": ROOT / "dev" / "client" / "build" / "make_release.py",
        "dev/client/build/package.py": ROOT / "dev" / "client" / "build" / "package.py",
        "dev/client/build/make_readme.py": ROOT / "dev" / "client" / "build" / "make_readme.py",
        "tools/check_release.py": ROOT / "tools" / "check_release.py",
    }
    if src_zip.is_file():
        with zipfile.ZipFile(src_zip) as z:
            names = set(z.namelist())
            for arc, src in required_sources.items():
                check(f"源码 ZIP 含 {arc}", arc in names)
                if arc in names:
                    check(f"源码 ZIP 的 {arc} 与仓库一致",
                          z.read(arc) == src.read_bytes())

    print("[校验和]")
    sums = rel / "SHA256SUMS.txt"
    check("SHA256SUMS.txt 存在", sums.is_file())
    if sums.is_file():
        r = subprocess.run(["sha256sum", "-c", "--quiet", sums.name], cwd=rel,
                           capture_output=True, text=True)
        check("sha256sum -c 通过", r.returncode == 0, r.stdout[-500:] + r.stderr[-500:])

    print()
    if FAILS:
        print(f"✘ {len(FAILS)} 项不通过：{', '.join(FAILS)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
