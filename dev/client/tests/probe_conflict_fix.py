"""用真实板子数据验证「用板子的（覆盖本机）」这条路径。

★ 用配置副本，不动用户真配置 ★
  只读板子 + 写副本。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="bridge-v201-")
os.environ["LOCALAPPDATA"] = tmp
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

real_cfg = Path(os.environ["USERPROFILE"]) / "AppData" / "Local" / \
    "ESP32S3RemoteBridge" / "config.json"
dst = Path(tmp) / "ESP32S3RemoteBridge" / "config.json"
dst.parent.mkdir(parents=True, exist_ok=True)
shutil.copy(real_cfg, dst)
print("配置副本:", dst, flush=True)

import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core.controller import BridgeController  # noqa: E402
from core.key_mapping import firmware_bindings  # noqa: E402

app = QCoreApplication(sys.argv)
ctl = BridgeController()


def wait(seconds: float) -> None:
    """★ 必须转事件循环 ★
       协议响应是排队信号派发的，不跑事件循环就永远收不到。
    """
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


seen: list[dict] = []
# ★ 监听要先挂上 ★
#   冲突信号是连上板子后头一秒内就发出来的，
#   挂晚了就错过了（第一版探针就栽在这）。
ctl.mappingConflict.connect(lambda info: seen.append(
    {k: v for k, v in dict(info).items() if k != "board"}))

print("连接 COM12 …", flush=True)
ctl.connectPort("COM12")
wait(1.5)
ctl.refreshRemotes()
wait(2.0)

n = ctl.remoteModel.rowCount()
print(f"遥控器 {n} 台", flush=True)
for i in range(n):
    r = ctl.remoteModel.get(i)
    print(f"  [{i}] addr={r.get('addr')} 板子={r.get('bindsBoard')} "
          f"本机={r.get('binds')} conn={r.get('connected')}", flush=True)

wait(1.0)

print()
print("弹出的冲突:", seen, flush=True)
if seen:
    info = seen[-1]
    print("→ 选「用板子的（覆盖本机）」", flush=True)
    ctl.resolveMappingConflict(False)
    wait(0.5)
    print("状态:", ctl.statusMessage, flush=True)
    idx = ctl._current
    now = ctl._remote_bindings(idx)
    fb = {(k, t): toks for k, t, toks in firmware_bindings(now)}
    print(f"本机条数: {len(now.get('bindings') or {})}", flush=True)
    print("固件看到的键序列:", sorted(fb.items(), key=lambda x: x[0]), flush=True)
    cfg = json.loads(dst.read_text(encoding="utf-8"))
    arch = (cfg.get("mapping_archive") or {}).get(info.get("addr", ""), {})
    print("归档条数:",
          len((arch.get("bindings") or {}).get("bindings") or {}), flush=True)
    # 再读一次板子 → 不该再弹
    seen.clear()
    ctl.refreshRemotes()
    wait(2.5)
    print("再读一次后有没有又弹:", seen, flush=True)

ctl.shutdown()
