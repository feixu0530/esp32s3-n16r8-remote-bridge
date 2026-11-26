#!/usr/bin/env bash
# 编译 G.722.1 解码器 decode.exe（MinGW-w64 / MSYS2）
#
# 许可：libg722_1 是 GPL-2.0，decode.exe 这个二进制按 GPL-2.0 分发。
# 主程序只以独立进程调用它（传文件句柄，不链接），GPL 不传染主程序。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="$HERE/libg7221"
OUT="$HERE/build"
mkdir -p "$OUT"

SRC=$(ls "$LIB"/src/*.c | grep -v make_dct4_tables | grep -v make_tables | tr '\n' ' ')

gcc -O2 -o "$OUT/decode.exe" \
    "$HERE/decode_main.c" \
    -I"$LIB/src" -I"$LIB/src/g722_1" \
    $SRC \
    -DG722_1_USE_FIXED_POINT \
    -lm

echo "built: $OUT/decode.exe"
ls -la "$OUT/decode.exe"
