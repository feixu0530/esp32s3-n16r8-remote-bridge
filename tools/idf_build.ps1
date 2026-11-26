# ESP-IDF v6.1 环境激活 + 构建脚本 (PowerShell)
# 用法: powershell -ExecutionPolicy Bypass -File tools/idf_build.ps1 -Command build
#   -Command: build | flash | size | fullclean | monitor | set-target

param(
    [string]$Command = "build",
    [string]$ProjectPath = "",
    [string]$Port = ""
)

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# ★ 从 Git Bash / MSYS 触发时必须清掉 MSYSTEM ★
#   IDF 6.1 的 idf.py 在检测到 MSYSTEM 时只打一行警告、**不执行 main()**，
#   退出码还是 0 —— 看起来"编译成功"，其实一个文件都没编。
#   （MSYS 运行时会把它注入给子进程，bash 里 unset 也没用。）
Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue

$env:IDF_PATH = "C:\esp\v6.1\esp-idf"
$env:IDF_TOOLS_PATH = "C:\Espressif"
$env:IDF_PYTHON_ENV_PATH = "C:\Espressif\python_env\idf6.1_py3.11_env"
$env:ESP_IDF_VERSION = "6.1"

# esp-rom-elfs：IDF 生成 gdbinit 时需要（否则报 "ESP_ROM_ELF_DIR environment variable is not defined"）
$romElf = @(Get-ChildItem "C:\Espressif\tools\esp-rom-elfs" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
if ($romElf.Count -gt 0) {
    $env:ESP_ROM_ELF_DIR = $romElf[0].FullName
}

# 工具链路径（IDF 需要 xtensa-esp-elf 与 riscv32-esp-elf 都在 PATH）
$toolPaths = @(
    "C:\Espressif\python_env\idf6.1_py3.11_env\Scripts",
    "C:\Espressif\tools\cmake\4.0.3\bin",
    "C:\Espressif\tools\ninja\1.12.1",
    "C:\esp\v6.1\esp-idf\tools"
)
$xtensaDirs = @(Get-ChildItem "C:\Espressif\tools\xtensa-esp-elf" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
foreach ($d in $xtensaDirs) {
    $p = Join-Path $d.FullName "xtensa-esp-elf\bin"
    if (Test-Path $p) { $toolPaths += $p }
}
$riscvDirs = @(Get-ChildItem "C:\Espressif\tools\riscv32-esp-elf" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
foreach ($d in $riscvDirs) {
    $p = Join-Path $d.FullName "riscv32-esp-elf\bin"
    if (Test-Path $p) { $toolPaths += $p }
}
$openocdDirs = @(Get-ChildItem "C:\Espressif\tools\openocd-esp32" -Directory -ErrorAction SilentlyContinue)
foreach ($d in $openocdDirs) {
    $p = Join-Path $d.FullName "openocd-esp32\bin"
    if (Test-Path $p) { $toolPaths += $p }
}
$env:PATH = ($toolPaths -join ";") + ";" + $env:PATH

if ([string]::IsNullOrEmpty($ProjectPath)) {
    $ProjectPath = Join-Path $PSScriptRoot "..\dev\firmware\bridge"
}
$ProjectPath = (Resolve-Path $ProjectPath).Path
Set-Location $ProjectPath

$idfArgs = @("C:\esp\v6.1\esp-idf\tools\idf.py", $Command)
if ($Port -ne "") { $idfArgs += @("-p", $Port) }

# 把子进程输出写到临时日志再回显。
# 原因：直接 & 调用时，若子进程输出量大，外层 PowerShell 的输出捕获会失败，
#       表现为命令静默返回 exit 1 —— 看起来像构建失败，其实构建是成功的。
$logFile = Join-Path $env:TEMP ("idf_build_" + [System.Diagnostics.Process]::GetCurrentProcess().Id + ".log")
& "C:\Espressif\python_env\idf6.1_py3.11_env\Scripts\python.exe" @idfArgs *> $logFile
$code = $LASTEXITCODE
if (Test-Path $logFile) {
    Get-Content $logFile | Write-Output
    Remove-Item $logFile -Force -ErrorAction SilentlyContinue
}
exit $code
