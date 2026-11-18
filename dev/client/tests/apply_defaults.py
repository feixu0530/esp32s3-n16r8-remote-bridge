"""把出厂默认映射铺到板子上（修好消费者键/多手势之后的一次性修复）。

★ 为什么需要这个 ★
    0.18.0 的客户端读回板子映射时会丢掉消费者页的条目（音量+/−），
    又把读回来的（缺项的）整份推回去 —— 音量键被反复抹掉。
    返回键的单击则被「快照覆盖多手势」的 bug 删掉。
    所以这里用修好的代码，按当前遥控器重铺一次出厂默认。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core.controller import BridgeController  # noqa: E402
from core.key_mapping import firmware_bindings  # noqa: E402


def pump(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    app = QCoreApplication(sys.argv)
    ctl = BridgeController()
    try:
        print("连接 COM12 …", flush=True)
        if not ctl.connectPort("COM12"):
            print("连接失败", flush=True)
            return 1
        pump(app, 1.5)
        ctl.refreshRemotes()
        pump(app, 2.0)
        n = ctl.remoteModel.rowCount()
        print(f"板子上 {n} 台遥控器", flush=True)
        for i in range(n):
            row = ctl.remoteModel.get(i)
            name = str(row.get("name", "") or "")
            print(f"[{i}] {name} addr={row.get('addr')} 板子={row.get('bindsBoard')} 条",
                  flush=True)
            ctl._current = i
            ctl._bindings = ctl._default_bindings_for_current()
            print(f"     准备下发：proto={ctl._proto is not None} "
                  f"addr={ctl.remoteModel.get(i).get('addr')!r} "
                  f"条数={len(firmware_bindings(ctl._bindings))}", flush=True)
            ctl._save_bindings(write_archive=True, push=True)
            pump(app, 1.2)
            local = ctl._remote_bindings(i)
            print(f"     本机已下发 {len(firmware_bindings(local))} 条", flush=True)
        # 回读确认
        ctl.refreshRemotes()
        pump(app, 2.0)
        print("回读：", flush=True)
        for i in range(ctl.remoteModel.rowCount()):
            row = ctl.remoteModel.get(i)
            print(f"  [{i}] {row.get('name')} 板子={row.get('bindsBoard')} 条 "
                  f"本机={row.get('binds')} 条", flush=True)
        return 0
    finally:
        ctl.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
