"""查板子当前状态：固件版本 + 遥控器名单 + 每台的映射条数。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-probe-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core.controller import BridgeController  # noqa: E402

app = QCoreApplication(sys.argv)
ctl = BridgeController()

print("连接…", flush=True)
ok = ctl.connectPort("COM12")
print("connectPort ->", ok, flush=True)
time.sleep(1.5)
print("状态:", ctl.statusMessage, flush=True)
print("固件:", ctl.firmwareVersion, flush=True)

ctl.refreshRemotes()
time.sleep(1.5)

n = ctl.remoteModel.rowCount()
print(f"遥控器 {n} 台", flush=True)
for i in range(n):
    r = ctl.remoteModel.get(i)
    print(f"  [{i}] name={r.get('name')!r} addr={r.get('addr')} "
          f"id={r.get('identity')} conn={r.get('connected')} "
          f"板子={r.get('bindsBoard')} 本机={r.get('binds')}", flush=True)

ctl.shutdown()
