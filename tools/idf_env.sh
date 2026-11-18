#!/usr/bin/env bash
# ESP-IDF v6.1 环境激活脚本 (Git Bash / MSYS)
# 用法: source tools/idf_env.sh && idf.py build
#
# 注意: IDF 自带的 export.sh 在 Git Bash 下不可靠（它按 cmd/PowerShell 语义找工具），
# 这里改为显式拼 PATH，版本取自 C:/Espressif/tools 实际安装情况。

export IDF_PATH="C:/esp/v6.1/esp-idf"
export IDF_TOOLS_PATH="C:/Espressif"

# IDF Python 虚拟环境（3.11）
_IDF_PY="C:/Espressif/python_env/idf6.1_py3.11_env/Scripts"

# 工具链
_CMAKE_BIN="C:/Espressif/tools/cmake/4.0.3/bin"
_NINJA_BIN="C:/Espressif/tools/ninja/1.12.1"

export PATH="$IDF_PY:$_CMAKE_BIN:$_NINJA_BIN:$IDF_PATH/tools:$PATH"

# xtensa-esp-elf 与 riscv32-esp-elf（取最新版本目录）
for _d in "$IDF_TOOLS_PATH"/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin; do
    [ -d "$_d" ] && export PATH="$_d:$PATH"
done
for _d in "$IDF_TOOLS_PATH"/tools/riscv32-esp-elf/*/riscv32-esp-elf/bin; do
    [ -d "$_d" ] && export PATH="$_d:$PATH"
done

# OpenOCD / dfu-util 等
for _d in "$IDF_TOOLS_PATH"/tools/openocd-esp32/*/openocd-esp32/bin \
           "$IDF_TOOLS_PATH"/tools/dfu-util/*/dfu-util-*/; do
    [ -d "$_d" ] && export PATH="$_d:$PATH"
done

# Python 变量（idf.py 靠这些找解释器）
export IDF_PYTHON_ENV_PATH="C:/Espressif/python_env/idf6.1_py3.11_env"
