"""
v0.24 闸门：端口占用 + HID 抬起不丢 + 默认档案按 MAC 记录。

★ 每条闸门对应用户实测报的一条 ★
    1. PC 端连不上板子（点「连接」无效）
    2. 按一下返回键，输入框里的文字全被删光
    3. 默认档案要按「遥控器型号 + MAC」分，并记录在板子上

运行：
    python dev/client/tests/test_v024.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="bridge-v024-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["BRIDGE_NO_AUTO_CONNECT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core.controller import BridgeController  # noqa: E402

FAILURES: list[str] = []

ROOT = Path(__file__).resolve().parents[3]
FW_MAIN = ROOT / "dev" / "firmware" / "bridge" / "main"
CLIENT = ROOT / "dev" / "client"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def test_port_busy_message() -> None:
    """端口被别的程序占着，要说人话。"""
    src = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("认得出端口被占用（PermissionError）",
          "PermissionError" in src, "")
    check("提示里说清了怎么办",
          "被别的程序占着" in src and "另一个客户端" in src, "")


def test_single_instance() -> None:
    """单实例：两个客户端抢串口是「点连接没反应」的根源。"""
    app_py = (CLIENT / "src" / "app.py").read_text(encoding="utf-8")
    check("有单实例锁（Win32 命名互斥量）",
          "CreateMutexW" in app_py, "")
    check("已有一个实例时提示用户",
          "已经有一个客户端在运行" in app_py, "")
    check("已有一个实例时把它叫到前台（藏在托盘也找得到）",
          "_focus_existing_window()" in app_py and "FindWindowW" in app_py
          and "ShowWindow(hwnd, 9)" in app_py, "")
    check("没用 QSharedMemory（tryLock 在段刚创建时返回假，会误判）",
          "QSharedMemory(" not in app_py, "")

    pkg = (CLIENT / "build" / "package.py").read_text(encoding="utf-8")
    check("打包自检跑完会清掉残留进程",
          "taskkill" in pkg and "ESP32S3RemoteBridge.exe" in pkg, "")


def test_hid_release_never_dropped() -> None:
    """★ 抬起不能因为 USB 暂不可用就整个丢掉 ★

    实测（用户）：板子的日志把 CDC 写队列塞满 → USB 复合设备卡住 →
    `tud_hid_ready()` 返回假 → 以前 `bridge_usb_release_keyboard()`
    开头直接 return → **抬起事件丢了** → 那个键永远按在 PC 上 →
    系统自动重复 → 输入框文字被一下删光。
    """
    usb = (FW_MAIN / "br_usb.c").read_text(encoding="utf-8")
    rel = usb[usb.index("void bridge_usb_release_keyboard"):]
    rel = rel[:rel.index("\n}")]
    # ★ 关键不是「有没有 tud_hid_ready 检查」，而是「**状态修改在检查之前**」★
    #   USB 不 ready 时要把「这个键不再按着」先落到状态里，
    #   报告等 USB 恢复了再补发。
    i_state = rel.index("s_kbd_keys[k] == usage")
    i_ready = rel.index("tud_hid_ready()")
    check("抬起：先改「不再按着」的状态，再看 USB 能不能发报告",
          i_state < i_ready, rel[:200])
    check("抬起会记下待补发", "s_report_pending = true" in rel, "")
    check("★ 真发失败也算待补发（ready 说了算、发的时候仍可能失败）★",
          "s_report_pending = !send_hid_keyboard_checked" in usb, "")
    check("有补发机制", "补发之前发不出去的 HID 报告" in usb, "")

    press = usb[usb.index("void bridge_usb_press_keyboard"):]
    press = press[:press.index("\n}")]
    check("按下也会记下待补发（状态先改、报告后补）",
          "s_report_pending = true" in press, "")

    wd = usb[usb.index("void bridge_usb_hid_watchdog"):]
    wd = wd[:wd.index("\n}")]
    check("看门狗负责补发", "s_report_pending" in wd, "")


def test_profile_recorded_per_mac() -> None:
    """默认档案按「型号 + MAC」分，并记录在板子的配对表里。"""
    mapping = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    check("档案表有 id 字段",
          "const char              *id;" in mapping, "")
    check("按档案 id 查的接口",
          "br_mapping_profile_id_for_name" in mapping, "")
    check("apply_defaults 接受档案 id 并回填命中结果",
          "profile_in" in mapping and "out_profile" in mapping, "")

    core = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("配对表条目存了档案 id",
          "char     profile[16];" in core, "")
    check("档案 id 存在独立的 NVS 键 p<idx>",
          '"p%d"' in core, "")
    check("铺默认时按 identity/地址找到配对表条目",
          "pl_find_by_id" in core and "pl_get_profile" in core, "")


def test_no_empty_push() -> None:
    """空映射绝不下发（MAPBEGIN+MAPCOMMIT 会清空开发板）。"""
    src = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("空映射会跳过下发",
          "本机没有可下发的映射" in src, "")


def test_release_uses_press_record() -> None:
    """按住期间映射被改，抬起也必须发出去（按按下记录释放）。"""
    src = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    check("按下时记下实际发出的键",
          "fire_record(addr, key_id, trig, &b);" in src, "")
    check("抬起查不到绑定时按按下记录释放",
          "} else if (!have_bind) {" in src and
          "fire_take(addr, key_id, trig, &rec)" in src, "")
    check("记录表有并发保护",
          "portMUX_INITIALIZER_UNLOCKED" in src, "")
    check("正常抬起也会清记录（不留脏数据）",
          "(void)fire_take(addr, key_id, trig, &rec);" in src, "")


def test_key_tokens_and_pc_actions() -> None:
    """标点/左右修饰键能录入；PC 专属动作（打开应用/鼠标）有执行器。"""
    win = (CLIENT / "src" / "platforms" / "windows.py").read_text(encoding="utf-8")
    check("录制表认识逗号", '0xBC: "comma"' in win, "")
    check("HID 表有标点",
          '"comma": 0x36' in win and '"semicolon": 0x33' in win, "")
    check("HID 表有左右修饰键（QML 也用）",
          '"lwin": 0xE3' in win and '"rwin": 0xE7' in win, "")

    qml = (CLIENT / "src" / "ui" / "qml" / "ButtonsPage.qml").read_text(
        encoding="utf-8")
    check("QML 认录制出来的 lwin", '"lwin": "lwin"' in qml, "")
    check("QML 认标点符号", '",": "comma"' in qml, "")

    act = (CLIENT / "src" / "core" / "action_options.py").read_text(
        encoding="utf-8")
    check("能解析应用启动命令", "def application_command" in act, "")
    pca = (CLIENT / "src" / "core" / "pc_actions.py").read_text(encoding="utf-8")
    check("PC 动作执行器存在", "def run_pc_action" in pca, "")
    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("EV KEY 会分发给 PC 动作", "self._pc_key_event(kv)" in ctl, "")
    check("PC 动作按 key_id 查绑定（绑定字典的键是 2:120）",
          ("remote_layout.button_key_id_for(kind, button_id)" in ctl or
           "remote_layout.button_key_id(button_id)" in ctl), "")
    cap = (CLIENT / "src" / "platforms" /
           "hotkey_capture_process.py").read_text(encoding="utf-8")
    check("录制子进程共用主进程键表",
          "from platforms.windows import _MODIFIER_TOKENS, _VK_NAMES" in cap, "")
    check("录制子进程没有第二份键表", "_VK_NAMES = {" not in cap, "")
    check("PC 侧手势阈值与固件一致",
          "PC_LONG_MS = 550" in ctl and "PC_DOUBLE_MS = 300" in ctl, "")


def test_key_tables_and_combos() -> None:
    """三张键表全表比对 + 常见组合键转换。"""
    from platforms import windows as win
    from core.controller import build_map_rows

    named = ["backspace", "tab", "enter", "escape", "space", "pageup",
             "pagedown", "end", "home", "left", "up", "right", "down",
             "insert", "delete", "apps", "printscreen", "comma", "period",
             "slash", "backslash", "semicolon", "quote", "minus", "equal",
             "grave", "bracketleft", "bracketright",
             "lctrl", "rctrl", "lshift", "rshift", "lalt", "ralt",
             "lwin", "rwin"]
    named += [f"f{i}" for i in range(1, 13)]
    missing = [t for t in named if win.hid_usage_for_token(t) is None]
    check("所有命名键都有 HID usage", not missing, ", ".join(missing))

    qml = (CLIENT / "src" / "ui" / "qml" /
           "ButtonsPage.qml").read_text(encoding="utf-8")
    not_in_qml = [t for t in named if f'"{t}"' not in qml]
    check("QML 校验认得所有命名键", not not_in_qml, ", ".join(not_in_qml))

    def rows_for(tokens):
        bindings = {"bindings": {"2:1": {"kind": "key_combo",
                                         "keys": list(tokens)}}}
        return build_map_rows(bindings)

    cases = [
        (("lalt", "a"), 4, [0x04]),
        (("ctrl", "c"), 1, [0x06]),
        (("lwin", "e"), 8, [0x08]),
        (("lalt", "f4"), 4, [0x3D]),
        (("lctrl", "lshift", "escape"), 3, [0x29]),
        (("rshift", "f10"), 0x20, [0x43]),
        (("comma",), 0, [0x36]),
        (("semicolon", "slash"), 0, [0x33, 0x38]),
        (("alt", "tab"), 4, [0x2B]),
    ]
    for tokens, mods, keys in cases:
        rows = rows_for(tokens)
        ok = (len(rows) == 1 and rows[0][3] == mods
              and list(rows[0][5:]) == keys)
        check("组合键 " + "+".join(tokens) + f" → mods={mods} keys={keys}",
              ok, str(rows))


def test_push_is_paced() -> None:
    """下发必须逐条等 OK —— 连发会把最后的 MAPCOMMIT 丢掉。"""
    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("MAPSET 走等应答的发送",
          '_send_wait("MAPSET"' in ctl, "")
    check("MAPCOMMIT 走等应答的发送",
          '_send_wait("MAPCOMMIT")' in ctl, "")
    check("有下发中断保护",
          "下发中断，板子保持原映射" in ctl, "")


def test_custom_apps_and_voice_page() -> None:
    """自定义启动项 + 语音页即时生效 + 下拉不再有「按住说话」。"""
    from core import action_options
    from core.key_mapping import ActionKind

    groups = action_options.action_option_groups()
    labels = [opt for _t, opts in groups for opt in opts]
    check("下拉里没有「按住说话」（改到语音页配置）",
          "按住说话" not in labels, "")

    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("自定义启动项有增删接口",
          "def addCustomApp" in ctl and "def removeCustomApp" in ctl, "")
    check("自定义启动项写进下拉",
          "action_option_groups(self._custom_app_labels())" in ctl, "")
    check("自定义启动项解析成打开应用",
          "ButtonAction(ActionKind.OPEN_APP, uri=entry[\"path\"])" in ctl, "")

    pca = (CLIENT / "src" / "core" / "pc_actions.py").read_text(encoding="utf-8")
    check("自定义路径真的会启动", "launch_command([action.uri])" in pca, "")

    voice = (CLIENT / "src" / "ui" / "qml" /
             "VoicePage.qml").read_text(encoding="utf-8")
    check("语音热键边输边生效",
          "onTextEdited: hotkeyApplyTimer.restart()" in voice, "")
    check("空热键不保存（清空后不会让板子按空）",
          "if not tokens:\n            return False" in ctl, "")

    qml = (CLIENT / "src" / "ui" / "qml" /
           "ButtonsPage.qml").read_text(encoding="utf-8")
    check("按键页有启动应用管理入口",
          'objectName: "customAppsButton"' in qml and
          "Bridge.addCustomApp(" in qml, "")


def test_voice_page_reorg() -> None:
    """语音页瘦身 + 输入法设置入口 + 自检挪到设备页并写日志。"""
    voice = (CLIENT / "src" / "ui" / "qml" /
             "VoicePage.qml").read_text(encoding="utf-8")
    check("语音页不再有「测试验证」块", "测试验证" not in voice, "")
    check("语音页不再有按键自检按钮", "keySelfTestButton" not in voice, "")
    check("语音页有「输入法设置」按钮",
          "openImeSettingsButton" in voice and
          "Bridge.openImeSettings()" in voice, "")

    dev = (CLIENT / "src" / "ui" / "qml" /
           "DevicePage.qml").read_text(encoding="utf-8")
    check("设备页有按键自检（在诊断日志之前）",
          dev.index("keySelfTestRow") < dev.index("diagnosticLogRow"), "")
    check("设备页日志面板固定高度（页面不再整体拉伸）",
          "Layout.preferredHeight: 160" in dev and
          "Math.max(implicitHeight" not in dev, "")

    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("打开微信输入法设置（wetype_update -showsetting）",
          "wetype_update.exe" in ctl and '"-showsetting"' in ctl, "")
    check("其它输入法退回到系统设置页",
          "ms-settings:regionlanguage" in ctl, "")
    check("自检会往日志写一行",
          "按键自检：板子已执行" in ctl, "")
    check("语音热键写入后立刻回读",
          'self._proto.send("VOICEKEY", mods, len(keys), *keys)' in ctl
          and 'on_ok=self._on_voice_key' in ctl, "")


def test_voice_names_and_logs() -> None:
    """语音键名显示、录制即保存、自检提示、日志导出位置。"""
    voice = (CLIENT / "src" / "ui" / "qml" /
             "VoicePage.qml").read_text(encoding="utf-8")
    check("录制完立即写入板子",
          "Bridge.setImeHotkey(Bridge.capturedHotkey)" in voice, "")

    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("语音热键显示键名而不是 0x 键值",
          'f"0x{usage:02X}"' not in ctl.split("def _voice_key_label")[1][:600], "")
    check("日志里自检显示「测试按键」", '"测试按键"' in ctl, "")
    check("键盘 usage 显示键名",
          "token_for_keyboard_usage" in ctl, "")

    dev = (CLIENT / "src" / "ui" / "qml" /
           "DevicePage.qml").read_text(encoding="utf-8")
    check("诊断日志去掉实时/拉取/拨钮",
          "logLiveLink" not in dev and "logHistoryLink" not in dev
          and "diagnosticTraceSwitch" not in dev, "")

    exp = (CLIENT / "src" / "core" / "log_export.py").read_text(encoding="utf-8")
    check("日志默认导出到下载文件夹",
          "def downloads_dir" in exp and "downloads_dir() /" in exp, "")

    hid = (Path("C:/Users/Figo/Desktop/Dev/ESP32S3-all-remote/dev/firmware/"
                "bridge/main/br_hid.c")).read_text(encoding="utf-8")
    check("自检按键按住 150ms（灯看得见）",
          "vTaskDelay(pdMS_TO_TICKS(150))" in hid, "")


def test_detect_and_model_defaults() -> None:
    """检测保持状态、型号默认模板、界面清理。"""
    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    det = ctl.split("def _detect_key")[1][:2400]
    check("检测真实按键不再自动退出",
          "_learn_active = False" not in det, "")
    check("检测到键后仍提示继续",
          "继续按其它键" in det, "")

    check("能保存型号默认", "def saveModelDefault" in ctl, "")
    check("恢复默认优先用型号模板",
          "存过型号默认就用它" in ctl, "")
    check("新配对自动套用型号默认",
          "def _apply_model_defaults" in ctl and
          "_apply_model_defaults()" in ctl.split("def _on_remotes_listed")[1][:400],
          "")

    main = (CLIENT / "src" / "ui" / "qml" / "main.qml").read_text(encoding="utf-8")
    check("左下角状态小灯已移除", "开发板连接状态小灯" not in main, "")

    page = (CLIENT / "src" / "ui" / "qml" /
            "ButtonsPage.qml").read_text(encoding="utf-8")
    check("按键页有「存为型号默认」按钮",
          'objectName: "saveModelDefaultButton"' in page, "")


def test_app_title() -> None:
    """窗口标题一处定义，QML / 单实例查找 / 托盘都用它。"""
    from core.version import APP_TITLE
    check("标题是用户指定的",
          APP_TITLE == "ESP32 S3 N16R8 遥控器桥接 By Figo  email：feixu0530@qq.com",
          APP_TITLE)
    qml = (CLIENT / "src" / "ui" / "qml" / "main.qml").read_text(encoding="utf-8")
    check("窗口标题取自 APP_TITLE", "title: Bridge.appTitle" in qml, "")
    app_py = (CLIENT / "src" / "app.py").read_text(encoding="utf-8")
    check("单实例找窗口用 APP_TITLE",
          "FindWindowW(None, APP_TITLE)" in app_py, "")
    check("旧标题已清理",
          "ESP32-S3 遥控器桥接" not in qml and
          "ESP32-S3 遥控器桥接" not in app_py, "")


def test_flash_safety() -> None:
    """刷写：不整片擦除、线程兜异常、失败退回外部 esptool。"""
    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    # controller 里现在有多个 run()（推映射/推自定义键/刷写），
    # 必须挑「刷写」那个来检查
    _parts = ctl.split("def run() -> None:")
    run = next(p_ for p_ in _parts[1:]
               if "刷写线程必须兜住异常" in p_[:600])[:1500]
    check("刷写不再先整片擦除", "erase_flash" not in ctl, "")
    check("刷写线程兜住异常", "except Exception as exc:" in run and
          "刷写异常" in run, "")
    check("刷写过程写进 app.log", 'self.log.info("[flash] %s"' in ctl, "")

    fl = (CLIENT / "src" / "core" / "flasher.py").read_text(encoding="utf-8")
    check("进程内失败一律退回外部 esptool",
          "进程内 esptool 失败，改用外部 esptool 重试" in fl, "")
    check("三个镜像的偏移正确",
          'FlashTarget(f, 0x0)' in fl and 'FlashTarget(f, 0x8000)' in fl
          and 'FlashTarget(app, 0x10000)' in fl, "")


def test_flash_paths_and_labels() -> None:
    """刷写吃三种路径形态；「恢复型号默认」改名到位。"""
    ctl = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    seg = ctl.split("def _flash(")[1][:1400]
    helper = ctl.split("def as_local_path")[1][:1200]
    check("有统一的路径转换", "def as_local_path" in ctl, "")
    check("转换认识 {path, offset} 字典", 'isinstance(p, dict)' in helper, "")
    check("转换认识 file:// URL", 'text.startswith("file:")' in helper, "")
    check("转换兜住 QUrl 的 repr", 'PySide6.QtCore.QUrl(' in helper, "")
    check("刷写调用统一转换", "as_local_path(p)" in seg, "")
    pick = ctl.split("def pickFirmwareFiles")[1][:600]
    check("选固件也调用统一转换", "as_local_path(p)" in pick, "")

    page = (CLIENT / "src" / "ui" / "qml" /
            "ButtonsPage.qml").read_text(encoding="utf-8")
    check("按钮改名为「恢复型号默认」", "恢复型号默认" in page, "")
    check("旧文案已清理", "恢复内置默认" not in page, "")

    dev = (CLIENT / "src" / "ui" / "qml" /
           "DevicePage.qml").read_text(encoding="utf-8")
    check("仅应用排在烧录前面",
          dev.index('objectName: "flashAppButton"') < dev.index('objectName: "flashButton"'),
          "")
    check("仅应用与烧录同为高亮样式",
          dev.count("flashAppButton") and
          dev.split('objectName: "flashAppButton"')[1][:500].count("highlighted: true") == 1,
          "")


def test_path_normalization() -> None:
    """QUrl / 字典 / 字符串 / file: URL 都要转成本地路径。"""
    from PySide6.QtCore import QUrl
    from core.controller import as_local_path

    q = QUrl.fromLocalFile("C:/tmp/a.bin")
    # 前提：PySide6 6.x 的 str(QUrl) 是 repr —— 这正是之前刷不了的根因
    check("前提：str(QUrl) 是 repr（不是路径）",
          str(q).startswith("PySide6.QtCore.QUrl("), str(q))
    check("QUrl 转本地路径", as_local_path(q).replace("\\", "/") == "C:/tmp/a.bin",
          as_local_path(q))
    check("字典取 path",
          as_local_path({"path": "C:/tmp/a.bin", "offset": 1}).replace("\\", "/")
          == "C:/tmp/a.bin", "")
    check("字符串原样",
          as_local_path("C:/tmp/a.bin") == "C:/tmp/a.bin", "")
    check("file: URL 转本地",
          as_local_path("file:///C:/tmp/a.bin").replace("\\", "/") == "C:/tmp/a.bin",
          "")


def test_flash_row_shows_firmware_name() -> None:
    """刷写行不再重复显示「烧录通道」，改为显示选中的固件名。"""
    dev = (CLIENT / "src" / "ui" / "qml" /
           "DevicePage.qml").read_text(encoding="utf-8")
    check("不再用 flashPortHint 当描述",
          "descriptionText: Bridge.flashPortHint" not in dev, "")
    check("没选固件时有提示",
          "还没选固件（将使用包内固件）" in dev, "")
    check("选了固件显示文件名", '"已选："' in dev and "firmwareFiles" in dev, "")


def main() -> int:
    print("=" * 66)
    print("v0.24 闸门：端口占用 / HID 抬起 / 档案按 MAC")
    print("=" * 66)

    app = QCoreApplication(sys.argv)
    ctl = BridgeController()
    try:
        test_port_busy_message()
        test_single_instance()
        test_hid_release_never_dropped()
        test_profile_recorded_per_mac()
        test_no_empty_push()
        test_release_uses_press_record()
        test_key_tokens_and_pc_actions()
        test_key_tables_and_combos()
        test_push_is_paced()
        test_custom_apps_and_voice_page()
        test_voice_page_reorg()
        test_voice_names_and_logs()
        test_detect_and_model_defaults()
        test_app_title()
        test_flash_safety()
        test_flash_paths_and_labels()
        test_path_normalization()
        test_flash_row_shows_firmware_name()
    finally:
        ctl.shutdown()

    print()
    print("=" * 66)
    if FAILURES:
        print(f"✘ {len(FAILURES)} 项未通过：{', '.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
