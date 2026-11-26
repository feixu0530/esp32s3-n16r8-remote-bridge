"""发布产物硬闸门：文档来源、版本替换、校验和自检。"""
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))

import check_release  # noqa: E402
import make_release  # noqa: E402
import package as packaging  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        print(f"       {detail}")
        FAILURES.append(name)


def test_docs_from_current_repo() -> None:
    old_repo = make_release.REPO
    with tempfile.TemporaryDirectory(prefix="bridge-release-docs-") as td:
        root = Path(td)
        old = root / "releases" / "1.0.0"
        out = root / "releases" / "2.1.0"
        old.mkdir(parents=True)
        out.mkdir(parents=True)
        (root / "LICENSE").write_text("current license", encoding="utf-8")
        (root / "THIRD_PARTY.md").write_text("current third party", encoding="utf-8")
        (root / "README.md").write_text(
            "# Remote Bridge 2.1.0\n\n小米一代按键已支持。", encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / "使用手册.md").write_text(
            "# 当前 2.1.0 使用手册\n", encoding="utf-8")
        (old / "README.md").write_text("# stale 1.0.0\n", encoding="utf-8")
        make_release.REPO = root
        try:
            make_release.copy_docs(out, "2.1.0")
        finally:
            make_release.REPO = old_repo

        text = (out / "README.md").read_text(encoding="utf-8")
        check("README 来自仓库当前版本", "小米一代按键已支持" in text and "stale" not in text)
        check("README 写入目标版本号", "2.1.0" in text)
        license_path = out / "LICENSE"
        third_party_path = out / "THIRD_PARTY.md"
        check("LICENSE 来自仓库当前版本",
              license_path.is_file()
              and license_path.read_text(encoding="utf-8") == "current license")
        check("第三方许可来自仓库当前版本",
              third_party_path.is_file()
              and third_party_path.read_text(encoding="utf-8") == "current third party")
        manual_path = out / "MANUAL-2.1.0-zh.md"
        check("版本化中文手册来自仓库当前版本",
              manual_path.is_file()
              and manual_path.read_text(encoding="utf-8") == "# 当前 2.1.0 使用手册\n")


def test_current_repo_has_release_documents() -> None:
    check("仓库根目录有当前 LICENSE", (make_release.REPO / "LICENSE").is_file())
    check("仓库根目录有当前第三方许可说明",
          (make_release.REPO / "THIRD_PARTY.md").is_file())
    check("仓库有当前中文手册",
          (make_release.REPO / "docs" / "使用手册.md").is_file())


def test_firmware_has_release_root_copy() -> None:
    old_build = make_release.FW_BUILD
    with tempfile.TemporaryDirectory(prefix="bridge-release-firmware-") as td:
        root = Path(td)
        build = root / "build"
        build.mkdir()
        (build / "remote_bridge.bin").write_bytes(b"firmware-2.1.0")
        out = root / "releases" / "2.1.0"
        out.mkdir(parents=True)
        make_release.FW_BUILD = build
        try:
            make_release.copy_firmware(out, "2.1.0")
        finally:
            make_release.FW_BUILD = old_build
        check("发布目录包含版本号固件",
              (out / "remote_bridge-2.1.0.bin").is_file())
        check("固件子目录保留同一份版本号固件",
              (out / "firmware" / "remote_bridge-2.1.0.bin").is_file())


def test_checksums_lf_and_unicode() -> None:
    with tempfile.TemporaryDirectory(prefix="bridge-release-sums-",
                                     ignore_cleanup_errors=True) as td:
        out = Path(td)
        (out / "固件.bin").write_bytes(b"firmware")
        (out / "使用说明.txt").write_text("manual\n", encoding="utf-8", newline="\n")
        make_release.make_sums(out)
        raw = (out / "SHA256SUMS.txt").read_bytes()
        check("校验和文件是 LF", b"\r\n" not in raw)
        check("中文文件名没有乱码", "固件.bin" in raw.decode("utf-8"))


def test_packaging_requires_summary_line() -> None:
    check("退出码为 0 且汇总通过才算闸门通过",
          packaging.check_passed(0, "前置输出\n全部通过\n"))
    check("退出码非 0 即使打印通过也失败",
          not packaging.check_passed(1, "全部通过\n"))
    check("缺少汇总行不能静默放行",
          not packaging.check_passed(0, "前置输出\nPASS  某一项\n"))
    check("契约闸门的带前缀汇总可识别",
          packaging.check_passed(0, "v1.1.0 契约闸门：全部通过\n"))
    check("汇总通过但仍有 FAIL 行也失败",
          not packaging.check_passed(0, "FAIL  某一项\n全部通过\n"))


def test_release_audit_finds_versioned_firmware() -> None:
    with tempfile.TemporaryDirectory(prefix="bridge-release-audit-") as td:
        rel = Path(td)
        versioned = rel / "firmware" / "remote_bridge-2.1.0.bin"
        versioned.parent.mkdir()
        versioned.write_bytes(b"firmware")
        check("发布审计定位带版本号的应用固件",
              check_release.ble_firmware_path(rel, "2.1.0") == versioned)
        check("缺固件时审计路径仍可判定为缺失",
              not check_release.ble_firmware_path(
                  rel / "missing", "2.1.0").exists())
        check("发布审计定位根目录版本化固件",
              check_release.root_firmware_path(rel, "2.1.0") == rel / "remote_bridge-2.1.0.bin")
        check("发布审计定位版本化源码包",
              check_release.source_archive_path(rel, "2.1.0") == rel / "ESP32S3-all-remote-2.1.0-src.zip")


def test_source_zip_keeps_release_scripts() -> None:
    old_repo = make_release.REPO
    with tempfile.TemporaryDirectory(prefix="bridge-release-src-") as td:
        root = Path(td)
        scripts = root / "dev" / "client" / "build"
        scripts.mkdir(parents=True)
        (scripts / "make_release.py").write_text("# release\n", encoding="utf-8")
        (scripts / "package.py").write_text("# package\n", encoding="utf-8")
        cache = scripts / "__pycache__"
        cache.mkdir()
        (cache / "package.pyc").write_bytes(b"cache")
        fw_build = root / "dev" / "firmware" / "bridge" / "build"
        fw_build.mkdir(parents=True)
        (fw_build / "remote_bridge.bin").write_bytes(b"firmware")
        (fw_build / "generated.txt").write_text("generated\n", encoding="utf-8")
        out = root / "releases" / "2.1.0"
        out.mkdir(parents=True)
        make_release.REPO = root
        try:
            archive = make_release.zip_src(out, "2.1.0", root / "releases" / "1.0.0")
        finally:
            make_release.REPO = old_repo
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
        check("源码包保留 make_release.py",
              "dev/client/build/make_release.py" in names)
        check("源码包保留 package.py",
              "dev/client/build/package.py" in names)
        check("源码包仍排除固件构建目录",
              "dev/firmware/bridge/build/generated.txt" not in names)
        check("源码包仍排除 Python 缓存",
              "dev/client/build/__pycache__/package.pyc" not in names)


def test_phone_apk_requires_explicit_source() -> None:
    old = os.environ.pop("MOTO_APK_DIR", None)
    with tempfile.TemporaryDirectory(prefix="bridge-release-apk-") as td:
        out = Path(td)
        stale = out / "Fremote-2.1.0.apk"
        stale.write_bytes(b"stale")
        raised = False
        try:
            make_release.copy_phone_apk(out, "2.1.0")
        except SystemExit:
            raised = True
        finally:
            if old is not None:
                os.environ["MOTO_APK_DIR"] = old
        check("没有明确 APK 来源时发布硬失败", raised)
        check("失败时不会把旧 APK 当成本次产物", stale.read_bytes() == b"stale")


def test_phone_apk_copies_fresh_source() -> None:
    with tempfile.TemporaryDirectory(prefix="bridge-release-apk-src-") as td:
        root = Path(td)
        src = root / "source"
        out = root / "release"
        src.mkdir()
        out.mkdir()
        (src / "Fremote-2.1.0.apk").write_bytes(b"fresh")
        (out / "Fremote-2.1.0.apk").write_bytes(b"stale")
        old = os.environ.get("MOTO_APK_DIR")
        os.environ["MOTO_APK_DIR"] = str(src)
        try:
            make_release.copy_phone_apk(out, "2.1.0")
        finally:
            if old is None:
                os.environ.pop("MOTO_APK_DIR", None)
            else:
                os.environ["MOTO_APK_DIR"] = old
        check("APK 来自明确来源并覆盖旧文件",
              (out / "Fremote-2.1.0.apk").read_bytes() == b"fresh")


def main() -> int:
    print("=" * 66)
    print("发布产物闸门")
    print("=" * 66)
    test_docs_from_current_repo()
    test_current_repo_has_release_documents()
    test_firmware_has_release_root_copy()
    test_source_zip_keeps_release_scripts()
    test_phone_apk_requires_explicit_source()
    test_phone_apk_copies_fresh_source()
    test_checksums_lf_and_unicode()
    test_packaging_requires_summary_line()
    test_release_audit_finds_versioned_firmware()
    print()
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：{', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
