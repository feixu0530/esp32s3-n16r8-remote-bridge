"""给已打过补丁的 APK 再打一层「文案补丁」（不改图标/名字/签名逻辑）。

用途：原始 movo APK 不在手边时，直接在现成 APK 上改设置页文案。
用法：python patch_apk_text.py <in.apk> <out.apk>
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patch_movo_apk import patch_assets_i18n  # noqa: E402


def patch_setup(text: str) -> str:
    text = text.replace('data-i18n="set.addr">电脑服务地址<',
                        'data-i18n="set.addr">开发板地址<')
    return text


def main() -> int:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    with zipfile.ZipFile(src, "r") as zin, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            name = item.filename
            data = zin.read(name)
            base = name.rsplit("/", 1)[-1].upper()
            if name.startswith("META-INF/") and (
                    base == "MANIFEST.MF" or base.endswith((".SF", ".RSA", ".DSA", ".EC"))):
                continue
            if name == "assets/i18n.js":
                data = patch_assets_i18n(data.decode("utf-8")).encode("utf-8")
                print("  ✓ i18n.js 文案")
            elif name == "assets/setup.html":
                data = patch_setup(data.decode("utf-8")).encode("utf-8")
                print("  ✓ setup.html 标签")
            zi = zipfile.ZipInfo(name, date_time=item.date_time)
            zi.compress_type = item.compress_type
            zi.external_attr = item.external_attr
            zout.writestr(zi, data)
    print("已生成 %s（%d 字节）" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
