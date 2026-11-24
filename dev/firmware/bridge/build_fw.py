"""用 Python 调起 ESP-IDF 的构建（绕过 Git Bash 对 cmd.exe 的限制）。"""
import subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

env = dict(os.environ)
env["PATH"] = (
    r"C:\Espressif\tools\cmake\4.0.3\bin;"
    r"C:\Espressif\tools\ninja\1.12.1;"
    # ★ S3 是 Xtensa 架构，工具链在 xtensa-esp-elf 下（不是 riscv32）
    r"C:\Espressif\tools\xtensa-esp-elf\esp-15.2.0_20251204\xtensa-esp-elf\bin;"
    r"C:\Espressif\tools\riscv32-esp-elf\esp-15.2.0_20251204\riscv32-esp-elf\bin;"
    r"C:\Espressif\python_env\idf6.1_py3.11_env\Scripts;"
    + env.get("PATH", "")
)
env["IDF_PATH"] = r"C:\esp\v6.1\esp-idf"
env["IDF_TOOLS_PATH"] = r"C:\Espressif"
env["IDF_TARGET"] = "esp32s3"

# idf.py 会读这些变量，少一个就报错
env["IDF_PYTHON_ENV_PATH"] = r"C:\Espressif\python_env\idf6.1_py3.11_env"
env["ESP_IDF_VERSION"] = "6.1"
env["IDF_CCACHE_ENABLE"] = "0"

# ★ 必须清掉 MSys/MinGW 的环境标记 ——
#   idf.py 靠这些变量判断自己是否跑在 MSys 下，是就直接退出。
#   即使是 Windows 原生的 python.exe，从 Git Bash 启动也会继承它们。
for _k in list(env.keys()):
    if _k.upper().startswith(("MSYSTEM", "MINGW", "MSYS")):
        del env[_k]
env.pop("SHELL", None)

PYTHON = r"C:\Espressif\python_env\idf6.1_py3.11_env\Scripts\python.exe"

print("=" * 70, flush=True)
print("ESP-IDF build: remote_bridge", flush=True)
print("=" * 70, flush=True)

cmd = [PYTHON, os.path.join(env["IDF_PATH"], "tools", "idf.py"), "build"]

try:
    # ★ web/ 改了必须先重新嵌入 ★
    #   br_web_assets.c 是生成后入库的源码，idf.py 不会自己重跑它 ——
    #   漏了这步，固件里还是旧网页（改了 shim 却看不到效果）。
    subprocess.run([PYTHON, os.path.join(HERE, "tools", "gen_web_assets.py")],
                   env=env, check=True)
    proc = subprocess.run(cmd, env=env, timeout=900)
    print(f"\n=== exit code: {proc.returncode} ===", flush=True)
    sys.exit(proc.returncode)
except subprocess.TimeoutExpired:
    print("BUILD TIMEOUT (>900s)", flush=True)
    sys.exit(2)
except Exception as exc:
    print(f"EXCEPTION: {type(exc).__name__}: {exc}", flush=True)
    sys.exit(3)
