"""
客户端地基自测：配置 / 日志 / 导出 / 开机自启 / 映射模型。

运行：
    python dev/client/tests/test_foundation.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# ★ 必须在 import core.* 之前 ★
#   本测试会写配置、最后还会把配置重置成 DEFAULT_CONFIG。
#   不重定向数据根的话，跑一次打包检查就把用户真机配对好的遥控器全清了。
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-foundation-")

os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def test_config() -> None:
    print("\n[配置]")
    from core import config as cfgmod

    cfg = cfgmod.load_config()
    check("默认配置可读", isinstance(cfg, dict) and "version" in cfg)
    check("默认输出开启", cfg.get("output_enabled") is True)

    cfg["log_level"] = "DEBUG"
    check("保存成功", cfgmod.save_config(cfg))
    back = cfgmod.load_config()
    check("读回一致", back.get("log_level") == "DEBUG")

    # 原子写不能留下临时文件
    leftovers = list(cfgmod.config_path().parent.glob(".*.tmp"))
    check("无残留临时文件", not leftovers, str(leftovers))

    # 坏文件要能降级
    cfgmod.config_path().write_text("{not json", encoding="utf-8")
    bad = cfgmod.load_config()
    check("坏 JSON 降级为默认",
          bad.get("version") == cfgmod.DEFAULT_CONFIG.get("version"))
    cfgmod.save_config(cfgmod.DEFAULT_CONFIG)


def test_logging() -> None:
    print("\n[日志]")
    from core import logging_setup

    m = logging_setup.setup("DEBUG")
    log = m.logger("app")
    for i in range(200):
        log.info("测试日志行 %d", i)
    log.error("这是一条错误")
    check("flush 后队列排空", m.flush_all(3.0))

    files = m.log_files()
    check("生成了日志文件", len(files) == 3, str(files))
    content = Path(files[0]).read_text(encoding="utf-8")
    check("日志里有内容", "测试日志行" in content)
    check("错误级别被记录", "这是一条错误" in content)

    # 隐私过滤
    log.info("mac=AABBCCDDEEFF")
    m.flush_all(2.0)
    content = Path(files[0]).read_text(encoding="utf-8")
    check("敏感字段被脱敏", "AABBCCDDEEFF" not in content)

    # 级别切换
    m.set_level("ERROR")
    check("级别可切换", m.get_level() == "ERROR")
    m.set_level("INFO")


def test_log_export() -> None:
    print("\n[日志导出]")
    from core import log_export

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "logs.zip"
        out = log_export.export_logs(target)
        check("导出文件存在", out.is_file())
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            check("ZIP 里有 manifest", "export-info.json" in names)
            manifest = json.loads(zf.read("export-info.json"))
            check("manifest 标记不含配置",
                  manifest.get("includes_configuration") is False)
            check("manifest 标记不含录音",
                  manifest.get("includes_recordings") is False)
            check("manifest 有快照稳定标记",
                  "snapshot_stable" in manifest)


def test_startup() -> None:
    print("\n[开机自启]")
    from core import startup
    from platforms.base import StartupState

    state = startup.read_state()
    check("能读到启动项状态", isinstance(state, StartupState))
    check("状态有 enabled 字段", isinstance(state.enabled, bool))
    # 注意：测试**不写**注册表，只读
    print(f"        当前状态：enabled={state.enabled} detail={state.detail}")


def test_key_mapping() -> None:
    print("\n[映射模型]")
    from core.key_mapping import (ActionKind, ButtonAction, ButtonTrigger,
                                  button_action_for, clear_button_action,
                                  firmware_bindings, has_secondary_action,
                                  set_button_action, set_display_note)

    a = ButtonAction(ActionKind.KEY_COMBO, ("ctrl", "p"))
    d = a.to_dict()
    check("to_dict 往返", ButtonAction.from_dict(d) == a)

    # fail-closed：非法数据必须抛异常
    for bad in ({}, {"kind": "nonexistent"}, {"kind": "key_combo"},
                {"kind": "escape", "keys": ["a"]},
                {"kind": "key_combo", "keys": [""]},
                {"kind": "key_combo", "keys": "abc"}):
        try:
            ButtonAction.from_dict(bad)
            check(f"非法输入被拒 {bad}", False, "没抛异常")
        except (KeyError, TypeError, ValueError):
            check(f"非法输入被拒 {bad}", True)

    # 读取映射
    bindings: dict = {}
    bindings = set_button_action(
        bindings, "key_up", ButtonTrigger.SINGLE_CLICK,
        ButtonAction(ActionKind.KEY_COMBO, ("ctrl", "p")))
    bindings = set_button_action(
        bindings, "key_mic", ButtonTrigger.SINGLE_CLICK,
        ButtonAction(ActionKind.VOICE_HOLD))
    bindings = set_display_note(
        bindings, "key_up", ButtonTrigger.SINGLE_CLICK, "打开命令面板")

    got = button_action_for(bindings, "key_up")
    check("读回单击动作", got.kind == ActionKind.KEY_COMBO)
    check("读回语音动作",
          button_action_for(bindings, "key_mic").kind == ActionKind.VOICE_HOLD)
    check("备注可读",
          bindings["display_notes"]["key_up"]["single_click"] == "打开命令面板")
    check("未配置的键是 DISABLED",
          button_action_for(bindings, "nope").kind == ActionKind.DISABLED)

    # 语音键的双击/长按必须被禁掉
    bindings = set_button_action(
        bindings, "key_mic", ButtonTrigger.DOUBLE_CLICK,
        ButtonAction(ActionKind.KEY_COMBO, ("x",)))
    check("语音键的双击被禁",
          button_action_for(bindings, "key_mic",
                             ButtonTrigger.DOUBLE_CLICK).kind == ActionKind.DISABLED)

    # 固件下发只包含 KeyCombo
    fb = firmware_bindings(bindings)
    check("固件下发只含 KeyCombo",
          all(k == "key_up" for k, _, _ in fb), str(fb))

    # 清除
    cleared = clear_button_action(bindings, "key_up", ButtonTrigger.SINGLE_CLICK)
    check("清除生效",
          button_action_for(cleared, "key_up").kind == ActionKind.DISABLED)


def test_platform() -> None:
    print("\n[平台层]")
    import platforms as plat

    summary = plat.platform_summary()
    check("平台已识别", summary["platform"] in ("windows", "macos"),
          str(summary))
    check("串口可用", plat.serial_port is not None)
    check("文字注入可用", plat.text_injector is not None)
    check("开机自启可用", plat.startup_manager is not None)

    if plat.serial_port is not None:
        ports = plat.serial_port.list_ports()
        check("能枚举串口", isinstance(ports, list))
        print(f"        发现 {len(ports)} 个串口："
              + ", ".join(p.device for p in ports[:8]))


def main() -> int:
    print("=" * 60)
    print("客户端地基自测")
    print("=" * 60)
    test_config()
    test_logging()
    test_log_export()
    test_startup()
    test_key_mapping()
    test_platform()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
