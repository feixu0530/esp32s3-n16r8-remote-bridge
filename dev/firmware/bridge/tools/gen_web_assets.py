"""把 web/ 目录下的静态资源压成 C 数组，交给固件内置。

为什么要在构建期压：
    开发板上没文件系统，网页得进固件。
    但网页有 ~200 KB（触控板页 97 KB + 键盘页 43 KB + 四个 js），
    塞进 4 MB 的 app 分区虽然塞得下，编译出来的 .bin 会很难看。

为什么压完不用解压：
    ★ 服务端直接带 `Content-Encoding: gzip` 头把压缩字节发出去，
      浏览器（WebView）自己会解压。板子上一个字节都不用解。★
    这是 HTTP 的标准做法，不是 hack。

用法（在 dev/firmware/bridge 下）：
    python tools/gen_web_assets.py

产物：
    main/br_web_assets.c   —— 资源表
    main/br_web_assets.h   —— 对外声明
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
WEB_DIR = os.path.join(BRIDGE, "web")
OUT_C = os.path.join(BRIDGE, "main", "br_web_assets.c")
OUT_H = os.path.join(BRIDGE, "main", "br_web_assets.h")

# 路由前缀 → 文件名。板子上的 URL 与 movo 的服务端保持一致，
# 这样 movo 的页面一行都不用改。
ROUTES = [
    ("/touchpad", "touchpad.html"),
    ("/k", "keyboard.html"),
    ("/phone-shot.jpg", "static/img/phone-shot.jpg"),
    ("/static/js/i18n.js", "static/js/i18n.js"),
    ("/static/js/link.js", "static/js/link.js"),
    ("/static/js/touchpad.js", "static/js/touchpad.js"),
    ("/static/js/voice.js", "static/js/voice.js"),
    ("/static/js/socket.io.min.js", "static/js/socket.io.min.js"),
]


def c_identifier(name: str) -> str:
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    ident = "".join(out)
    if ident and ident[0].isdigit():
        ident = "_" + ident
    return ident


# 页面里引用 js 的查询串。构建时会被替换成**本次构建的指纹**，
# 目的只有一个：手机 WebView 永远拿最新的 js。
#
# ★ 为什么必须变 ★
#   原来写死 ?v=20261028m，页面改了 js 之后手机还是用缓存里的旧版 ——
#   排查时表现为「固件明明改了，手机行为却没变」，最坑的一次。
#   指纹取所有资源内容的哈希，改一个字就变，手机必然重新拉。
CACHE_QUERY = re.compile(r"(\?v=)[0-9a-zA-Z]+")


def stamp_cache_query(raw: bytes, stamp: str) -> bytes:
    """把资源里的 ?v=xxx 换成构建指纹。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    if "?v=" not in text:
        return raw
    return CACHE_QUERY.sub(r"\g<1>" + stamp, text).encode("utf-8")


def main() -> int:
    if not os.path.isdir(WEB_DIR):
        print(f"找不到 web 目录: {WEB_DIR}", file=sys.stderr)
        return 1

    entries = []
    for route, rel in ROUTES:
        path = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(path):
            print(f"缺少资源: {rel}", file=sys.stderr)
            return 1
        with open(path, "rb") as fh:
            raw = fh.read()
        entries.append((route, rel, raw))

    # 指纹 = 所有资源内容的哈希（内容不变则指纹不变，缓存照旧有效）
    digest = hashlib.sha256()
    for _route, _rel, raw in entries:
        digest.update(raw)
    stamp = digest.hexdigest()[:10]

    packed_entries = []
    for route, rel, raw in entries:
        stamped = stamp_cache_query(raw, stamp)
        packed = gzip.compress(stamped, 9)
        packed_entries.append((route, rel, packed, len(stamped)))
    entries = packed_entries

    total_raw = sum(e[3] for e in entries)
    total_gz = sum(len(e[2]) for e in entries)

    with open(OUT_C, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("/*\n")
        fh.write(" * br_web_assets - 内置网页资源（由 tools/gen_web_assets.py 生成，勿手改）\n")
        fh.write(" *\n")
        fh.write(" * 内容是 gzip 压缩后的字节，服务端带 Content-Encoding: gzip 头发出去，\n")
        fh.write(" * 浏览器自己解压 —— 板子上不做任何解压。\n")
        fh.write(" */\n")
        fh.write('#include "br_web_assets.h"\n\n')

        for route, rel, packed, raw_len in entries:
            ident = c_identifier("web_" + rel.replace("/", "_"))
            fh.write(f"/* {route} ← {rel}（原始 {raw_len} 字节） */\n")
            fh.write(f"static const uint8_t {ident}[] = {{\n")
            for i in range(0, len(packed), 16):
                chunk = packed[i:i + 16]
                fh.write("    " + "".join(f"0x{b:02X}, " for b in chunk) + "\n")
            fh.write("};\n\n")

        fh.write("const br_web_asset_t br_web_assets[] = {\n")
        for route, rel, packed, raw_len in entries:
            ident = c_identifier("web_" + rel.replace("/", "_"))
            fh.write(f'    {{ "{route}", {ident}, sizeof({ident}), {raw_len} }},\n')
        fh.write("};\n\n")
        fh.write(f"const size_t br_web_assets_count = "
                 f"sizeof(br_web_assets) / sizeof(br_web_assets[0]);\n")

    with open(OUT_H, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("/*\n")
        fh.write(" * br_web_assets - 内置网页资源（由 tools/gen_web_assets.py 生成，勿手改）\n")
        fh.write(" */\n")
        fh.write("#pragma once\n\n")
        fh.write("#include <stddef.h>\n#include <stdint.h>\n\n")
        fh.write("typedef struct {\n"
                 "    const char *route;   /* 请求路径 */\n"
                 "    const uint8_t *data;   /* gzip 后的字节 */\n"
                 "    size_t len;            /* 压缩后长度 */\n"
                 "    size_t raw_len;        /* 原始长度（诊断用） */\n"
                 "} br_web_asset_t;\n\n")
        fh.write("extern const br_web_asset_t br_web_assets[];\n")
        fh.write("extern const size_t br_web_assets_count;\n")

    print(f"已生成 {len(entries)} 个资源：{total_raw} 字节 → {total_gz} 字节（gzip）")
    print(f"  {OUT_C}")
    print(f"  {OUT_H}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
