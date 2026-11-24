"""
一键重建 movo APK（带 BLE 桥）。

做的事：
    1. javac + d8 把 dev/apk/javasrc 编成 classes.dex
    2. apktool b 重新打包反编译目录
    3. 把 dex 以 classes4.dex 塞进 APK 根（minSdk 26 支持多 dex）
    4. zipalign + apksigner 签名

用法：
    python dev/apk/build_apk.py --out C:/tmp/Movo-1.2.4-ble.apk
    python dev/apk/build_apk.py --src C:/tmp/apkdec3 --apktool C:/path/apktool.jar

依赖（本机路径，可用参数覆盖）：
    JDK 17        C:/Program1/android/jdk-17.0.20.1+1
    android.jar   C:/Program1/android/sdk/platforms/android-34/android.jar
    build-tools   C:/Program1/android/sdk/build-tools/34.0.0
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

DEFAULTS = {
    "java_home": r"C:/Program1/android/jdk-17.0.20.1+1",
    "android_jar": r"C:/Program1/android/sdk/platforms/android-34/android.jar",
    "build_tools": r"C:/Program1/android/sdk/build-tools/34.0.0",
    "apktool": r"C:/Users/Figo/AppData/Local/Temp/apktools/apktool.jar",
    "keystore": None,       # 不给就在 releases/ 里找最新的 movo.keystore
    "src": r"C:/tmp/apkdec3",
}


def find_keystore() -> Path:
    hits = sorted(REPO.joinpath("releases").glob("*/movo.keystore"))
    if not hits:
        raise SystemExit("找不到 movo.keystore（releases/*/movo.keystore）")
    return hits[-1]


ENV = dict(os.environ)


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd[:6]), "..." if len(cmd) > 6 else "")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=ENV, **kw)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise SystemExit(f"命令失败：{cmd[0]}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--src", default=DEFAULTS["src"], help="apktool 反编译目录")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "movo-ble.apk"))
    ap.add_argument("--java-home", default=DEFAULTS["java_home"])
    ap.add_argument("--android-jar", default=DEFAULTS["android_jar"])
    ap.add_argument("--build-tools", default=DEFAULTS["build_tools"])
    ap.add_argument("--apktool", default=DEFAULTS["apktool"])
    ap.add_argument("--keystore", default="")
    args = ap.parse_args()

    src = Path(args.src)
    if not (src / "AndroidManifest.xml").is_file():
        raise SystemExit(f"{src} 不是 apktool 目录（缺 AndroidManifest.xml）")

    # apksigner.bat 是批处理，靠 JAVA_HOME 找 java —— 必须显式传进去
    ENV["JAVA_HOME"] = args.java_home
    ENV["PATH"] = str(Path(args.java_home) / "bin") + os.pathsep + ENV.get("PATH", "")

    java = Path(args.java_home) / "bin" / "javac.exe"
    d8 = Path(args.build_tools) / "lib" / "d8.jar"
    zipalign = Path(args.build_tools) / "zipalign.exe"
    apksigner = Path(args.build_tools) / "apksigner.bat"
    keystore = Path(args.keystore) if args.keystore else find_keystore()

    work = Path(tempfile.mkdtemp(prefix="movo_apk_"))
    classes = work / "classes"
    dex = work / "dex"
    classes.mkdir()
    dex.mkdir()

    java_src = HERE / "javasrc" / "com" / "dsnaaa" / "movo" / "BleClient.java"
    if not java_src.is_file():
        raise SystemExit(f"缺源码：{java_src}")

    print("[1/4] javac + d8")
    run([str(java), "-encoding", "UTF-8", "--release", "8",
         "-classpath", args.android_jar, "-d", str(classes), str(java_src)])
    class_files = [str(p) for p in classes.rglob("*.class")]
    run([str(Path(args.java_home) / "bin" / "java.exe"), "-cp", str(d8),
         "com.android.tools.r8.D8", "--min-api", "26",
         "--lib", args.android_jar, "--output", str(dex), *class_files])

    print("[2/4] apktool b")
    # 冷启动补丁必须在打包前打：离线页面不能被弹回设置页（见 patch_apk_nav.py）
    run([sys.executable, str(HERE / "patch_apk_nav.py"), str(src)])

    # 启动图标：换成项目自己的 logo（不能用原 movo 的默认图标）
    sys.path.insert(0, str(REPO / "dev" / "client" / "build"))
    import patch_movo_apk  # noqa: E402
    icons = patch_movo_apk.make_icon_pngs(work / "_icons")
    for density in ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"):
        d = src / "res" / ("mipmap-" + density)
        if not d.is_dir():
            continue
        for kind in ("fg", "bg"):
            target = d / ("ic_launcher_%s.png" % kind)
            data = icons.get("%s_%s" % (kind, density))
            if target.is_file() and data:
                target.write_bytes(data)
    unsigned = work / "unsigned.apk"
    run([str(Path(args.java_home) / "bin" / "java.exe"), "-jar", args.apktool,
         "b", str(src), "-o", str(unsigned)])

    print("[3/4] 塞 classes4.dex")
    with_dex = work / "withdex.apk"
    shutil.copyfile(unsigned, with_dex)
    with zipfile.ZipFile(with_dex, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dex / "classes.dex", "classes4.dex")

    print("[4/4] zipalign + 签名")
    aligned = work / "aligned.apk"
    run([str(zipalign), "-f", "4", str(with_dex), str(aligned)])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    run([str(apksigner), "sign", "--ks", str(keystore),
         "--ks-pass", "pass:movo2026", "--ks-key-alias", "movo",
         "--key-pass", "pass:movo2026", "--out", str(out), str(aligned)])

    print()
    print("完成：", out)
    print("签名库：", keystore)
    return 0


if __name__ == "__main__":
    sys.exit(main())
