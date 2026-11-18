"""
主控制器 —— QML 与后端之间的唯一入口。

职责：
    串口连接 / 扫描配对 / 遥控器管理 / 按键学习 / 映射 / 平面图标记 /
    语音状态 / 固件烧录 / 设置项

模型（给 QML 用）：
    RemoteListModel    已配对遥控器
    ScanModel          扫描候选
    MappingModel       某个遥控器的映射
    MappingModel      遥控器固定键位清单（按键页的行）
"""
from __future__ import annotations

import os
import threading
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (QAbstractListModel, QByteArray, QModelIndex,
                            QObject, Property, QTimer, Signal, Slot)
from PySide6.QtGui import QGuiApplication

from core import config as cfgmod
from core import human_log, logging_setup, startup
from core.action_options import (action_option_groups, kind_to_label,
                                 label_to_kind)
from core.key_mapping import (ActionKind, ButtonAction, ButtonTrigger,
                              CONSUMER_TOKENS, INDEX_TRIGGER, SEMANTIC_KEY_TOKENS,
                              TRIGGER_INDEX, button_action_for,
                              clear_button_action, display_note,
                              firmware_bindings, firmware_tokens,
                              set_button_action,
                              set_display_note)
from core import remote_layout
from core.pc_actions import is_pc_only, run_pc_action
from core.protocol import ProtocolClient, escape, unescape
from platforms import audio_player, hotkey_recorder, serial_port, text_injector
from platforms.windows import (hid_usage_for_token, launch_command, open_uri,
                               token_for_keyboard_usage, tokens_from_firmware)


def _fmt_kv(kv: dict) -> str:
    """把 k=v 字典拼成一行可读文本。"""
    return " ".join(f"{k}={v}" for k, v in kv.items())


def as_local_path(p) -> str:
    """
    把 QML 传来的「路径」统一成本地路径字符串。

    ★ PySide6 6.x 里 str(QUrl) 返回的是 repr！★
      实测：str(QUrl.fromLocalFile("C:/tmp/a.bin"))
            == "PySide6.QtCore.QUrl('file:///C:/tmp/a.bin')"
      直接当路径用永远是「文件不存在」。以前只处理字符串，
      「选固件」给的是 QUrl 对象，所以整体刷不了。
    """
    if isinstance(p, dict):
        p = p.get("path", "")
    if p is None:
        return ""
    local = getattr(p, "toLocalFile", None)
    if callable(local):
        return str(local() or "")
    text = str(p).strip()
    if text.startswith("PySide6.QtCore.QUrl("):
        import re
        m = re.search(r"QUrl\('(.*)'\)\s*$", text)
        if m:
            text = m.group(1)
    if text.startswith("file:"):
        from urllib.parse import unquote, urlsplit
        from urllib.request import url2pathname
        return url2pathname(unquote(urlsplit(text).path))
    return text


def build_map_rows(bindings: dict) -> list[tuple]:
    """
    把绑定文档转成 MAPSET 参数行（纯函数，闸门直接测它）。

    行格式：(key, trig, out, mods, nkeys, *usages)
        out=1 键盘页（mods + usages），out=2 消费者页（usage）

    ★ 修饰键必须进 mods，不能混进 keys ★
      lalt = 0xE2 —— 放进 keys 数组里 PC 不会当修饰键。
      「Alt+A 录得下、按下去不出来」就是这里错了会出的毛病。
    """
    rows: list[tuple] = []
    for key_id, trig, tokens in firmware_bindings(bindings):
        try:
            src_s, usage_s = key_id.split(":", 1)
            key = (int(src_s, 0) << 12) | int(usage_s, 0)
            usages = [hid_usage_for_token(token) for token in tokens]
        except (ValueError, TypeError):
            continue
        if not usages or any(usage is None for usage in usages):
            continue
        mods = 0
        keyboard_keys = []
        consumer_usage = 0
        for token, usage in zip(tokens, usages):
            if token in CONSUMER_TOKENS:
                consumer_usage = CONSUMER_TOKENS[token]
            elif 0xE0 <= usage <= 0xE7:
                mods |= 1 << (usage - 0xE0)
            else:
                keyboard_keys.append(usage)
        if consumer_usage and (keyboard_keys or mods):
            continue          # 一个触发只能有一种输出类型
        if consumer_usage:
            rows.append((key, trig, 2, 0, 1, consumer_usage))
        else:
            rows.append((key, trig, 1, mods, len(keyboard_keys), *keyboard_keys))
    return rows


# ★ 消费者 usage → token ★
#   板子上存的是消费者页的 usage（音量+ = 0xE9），
#   本机存的是 token（volume_up）—— 反查表就是给读回时用的。
CONSUMER_TOKEN_BY_USAGE = {v: k for k, v in CONSUMER_TOKENS.items()}


def _clamp01(v: float) -> float:
    """平面图坐标一律是 0..1 的归一化值 —— 出界就画到图片外面了。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)


# ==================================================================== 模型


class RemoteListModel(QAbstractListModel):
    """已配对遥控器列表。"""

    countChanged = Signal()

    IdxRole = QtUserRole = 256
    NameRole = 257
    AddrRole = 258
    TypeRole = 259
    ConnRole = 260
    VoiceRole = 261
    ImageRole = 262
    BindsRole = 263
    BindsBoardRole = 264

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[dict] = []

    def roleNames(self):
        return {256: b"idx", 257: b"name", 258: b"addr", 259: b"addrType",
                260: b"connected", 261: b"voice", 262: b"image",
                263: b"binds", 264: b"bindsBoard"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        return {
            256: index.row(),
            257: item.get("name", ""),
            258: item.get("addr", ""),
            259: item.get("addr_type", 0),
            260: item.get("connected", False),
            261: item.get("voice", ""),
            262: item.get("image", ""),
            263: item.get("binds", 0),
            264: item.get("binds_board", 0),
        }.get(role)

    # -- 变更 --
    def set_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()
        self.countChanged.emit()

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """行数。

        ★ 必须做成带通知信号的属性 ★
          QML 里写 `RemoteListModel.rowCount()` 是**方法调用**，
          绑定没有可通知的依赖 —— 界面会永远停在启动时算出的那个值。
          （v0.16.0 实测：下面列着 2 台，上面却写「已配对 0 台」）
        """
        return len(self._items)

    def update_item(self, row: int, **kwargs) -> None:
        if row < 0 or row >= len(self._items):
            return
        self._items[row].update(kwargs)
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx)

    # ★ 必须是 Slot：普通 Python 方法在 QML 里不可调用。
    @Slot(int, result=dict)
    def get(self, row: int) -> dict:
        if 0 <= row < len(self._items):
            return self._items[row]
        return {}


class ScanModel(QAbstractListModel):
    """扫描候选列表。"""

    AddrRole = 256
    NameRole = 257
    RssiRole = 258
    ScoreRole = 259
    TypeRole = 260

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[dict] = []

    def roleNames(self):
        return {256: b"addr", 257: b"name", 258: b"rssi", 259: b"score",
                260: b"addrType"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        return {256: item.get("addr", ""), 257: item.get("name", ""),
                258: item.get("rssi", 0), 259: item.get("score", 0),
                260: item.get("addr_type", 0)}.get(role)

    def set_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def add_item(self, item: dict) -> None:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(item)
        self.endInsertRows()

    @Slot(int, result="QVariantMap")
    # ★ 必须是 Slot：普通 Python 方法在 QML 里不可调用。
    @Slot(int, result=dict)
    def get(self, row: int) -> dict:
        if 0 <= row < len(self._items):
            return self._items[row]
        return {}

    def update_item(self, row: int, **kwargs) -> None:
        if row < 0 or row >= len(self._items):
            return
        self._items[row].update(kwargs)
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx)


class MappingModel(QAbstractListModel):
    """某个遥控器的映射（按物理键分组）。"""

    KeyRole = 256
    NameRole = 257
    SingleRole = 258
    DoubleRole = 259
    LongRole = 260
    NoteRole = 261

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[dict] = []

    def roleNames(self):
        return {256: b"buttonId", 257: b"buttonName", 258: b"keyId",
                259: b"singleText", 260: b"doubleText", 261: b"longText",
                262: b"singleNote", 263: b"isVoice", 264: b"visualRow",
                265: b"side"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        return {256: item.get("button_id", ""),
                257: item.get("button_name", ""),
                258: item.get("key_id", ""),
                259: item.get("single", ""),
                260: item.get("double", ""),
                261: item.get("long", ""),
                262: item.get("single_note", ""),
                263: item.get("is_voice", False),
                264: item.get("visual_row", 0),
                265: item.get("side", "left")}.get(role)

    def set_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    # ★ 必须是 Slot：普通 Python 方法在 QML 里不可调用。
    @Slot(int, result=dict)
    def get(self, row: int) -> dict:
        if 0 <= row < len(self._items):
            return self._items[row]
        return {}


# ================================================================== 控制器


# 快捷键录制最多录多久（秒）—— 超时自动停，防止钩子一直挂着吞键盘
CAPTURE_TIMEOUT_SECONDS = 30.0


class BridgeController(QObject):
    """QML 侧看到的唯一对象。"""

    connectedChanged = Signal()
    statusChanged = Signal()
    remotesChanged = Signal()
    scanChanged = Signal()
    mappingsChanged = Signal()

    learnChanged = Signal()
    logLine = Signal(str)
    voiceStateChanged = Signal()
    flashStateChanged = Signal()
    hotkeyCaptureChanged = Signal()
    logLevelChanged = Signal()
    launchAtLoginChanged = Signal()
    settingsChanged = Signal()
    flashLogChanged = Signal()
    pairingChanged = Signal()
    deviceInfoChanged = Signal()
    portCandidatesChanged = Signal()
    pairWindowChanged = Signal()
    # ★ 协议线程 → GUI 线程的中转 ★
    #   协议读线程直接改 Qt 模型会破坏 Qt 的内部状态（beginResetModel
    #   必须在 GUI 线程调用），实测会随机崩。
    #   读线程只 emit 信号，真正的处理放到 GUI 线程的槽里。
    protocolEventReceived = Signal(str, dict)
    protocolDataReceived = Signal(str, dict)
    protocolLogReceived = Signal(str, str)
    protocolRawReceived = Signal(str)
    guiCallRequested = Signal(object, object)
    logsChanged = Signal()
    closeBehaviorChanged = Signal()
    voiceKeyChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.log = logging_setup.get_logger("app")
        self.device_log = logging_setup.get_logger("device")
        self._cfg = cfgmod.load_config()
        if int(self._cfg.get("version", 1) or 1) < 2:
            self._cfg["voice_sink"] = "ime"
            self._cfg["ime_provider"] = "wechat"
            self._cfg["ime_hotkey"] = "lctrl+lwin"
            self._cfg["version"] = 2
            cfgmod.save_config(self._cfg)
        self._proto: Optional[ProtocolClient] = None
        self._port = serial_port

        self.remoteModel = RemoteListModel(self)
        self.scanModel = ScanModel(self)
        self.mappingModel = MappingModel(self)
        self._button_items: list[dict] = []
        self._selected_button_id: str = ""

        self._status = "未连接"
        self._connected = False
        self._scanning = False
        self._pairing = False
        self._pairing_target = ""
        self._current = -1
        self._learn_active = False
        self._learn_target = ""
        self._learn_result = ""
        self._learned: dict[str, str] = {}
        self._bindings: dict = {}
        self._voice_state = ""
        self._voice_text = ""
        self._flash_busy = False
        self._flash_port: str = ""
        self._last_voice_text: str = ""
        self._flash_progress = 0.0
        self._flash_log: list[str] = []
        self._last_export = ""
        self._firmware_files: list[str] = []
        self._hotkey_capturing = False
        self._captured_hotkey = ""
        self._show_all = False
        self._scan_items: list[dict] = []

        # ---- 设备页 / 日志 / 固件 ----
        self._fw_version = ""
        self._device_present = False
        self._device_port = ""
        self._port_candidates: list[str] = []
        self._port_signature = ""
        self._port_cache: Optional[list] = None
        self._reconnect_backoff = 0.0
        self._last_connect_try = 0.0
        # ★ 端口被别的程序占着（另一个客户端还在后台）★
        self._port_busy = False
        # 测试要能关掉自动连接（不然测试会连上真板子，行为不可预期）
        self._auto_connect = os.environ.get(
            "BRIDGE_NO_AUTO_CONNECT", "") in ("", "0")
        self._close_behavior = str(self._cfg.get("close_behavior", "hide") or "hide")
        self._log_lines: list[str] = []
        self._log_dirty = False
        self._board_addrs: set[str] = set()
        self._board_mapping: dict = {}
        self._collecting_map = False
        self._pair_window_until = 0.0
        self._pair_window_auto = False
        self._log_live = False
        self._capture_deadline = 0.0
        self._collecting_logs = False
        self._voice_key_mods = 0
        self._voice_key_keys: list[int] = []

        cfgmod.ensure_data_dirs()

        # ★ 协议读线程 → GUI 线程 ★
        #   Qt 的跨线程连接默认是队列连接，槽会在接收方所在的线程执行。
        self.protocolEventReceived.connect(self._apply_event)
        self.protocolDataReceived.connect(self._apply_data)
        self.protocolLogReceived.connect(self._apply_board_log)
        self.protocolRawReceived.connect(self._apply_raw_line)
        self.guiCallRequested.connect(self._run_gui_call)

        # 日志合并 flush：攒 200 ms 通知一次界面
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(200)
        self._log_timer.timeout.connect(self.flushLogs)
        self._log_timer.start()

        # 每 2 秒看一次开发板还在不在（插拔检测）
        self._port_timer = QTimer(self)
        self._port_timer.setInterval(2000)
        self._port_timer.timeout.connect(self._poll_ports)
        self._port_timer.start()

        # 录制超时兜底
        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(1000)
        self._capture_timer.timeout.connect(self._check_capture_deadline)
        self._capture_timer.start()

        # PC 专属动作（打开应用 / 鼠标）的手势判定 —— 板子做不了这些，
        # 依据 EV KEY 在 PC 侧自己判单击/双击/长按（阈值与固件一致）。
        self._pc_gesture: dict[tuple[str, str], dict] = {}
        self._pc_timer = QTimer(self)
        self._pc_timer.setInterval(40)
        self._pc_timer.timeout.connect(self._pc_gesture_tick)
        self._pc_timer.start()
        self._poll_ports()

        # 按键页的行来自固定档案，跟有没有连上、选没选遥控器无关 ——
        # 先填好，打开页面就有完整的一列键可配。
        self._load_mappings()

    # ------------------------------------------------------------ 属性
    @Property(str, notify=statusChanged)
    def statusMessage(self) -> str:
        return self._status

    @Property(bool, notify=connectedChanged)
    def connected(self) -> bool:
        return self._connected

    @Property(bool, notify=scanChanged)
    def scanning(self) -> bool:
        return self._scanning

    @Property(bool, notify=pairingChanged)
    def pairing(self) -> bool:
        """
        配对窗口是否还开着。

        ★ 为什么要单独一个状态 ★
          遥控器睡着了完全不广播，固件在 2 分钟窗口里反复试是**正常的**，
          期间每一轮都会报 CONNFAIL。界面上如果只写"连接失败"，
          用户会以为流程已经结束、就停手了 —— 而实际再等几秒就能连上。
        """
        return self._pairing

    @Property(bool, notify=scanChanged)
    def showAllDevices(self) -> bool:
        """
        「显示所有设备」。

        ★ 学 Windows 的「添加设备」对话框 ★
        默认只列「有名字 + 信号强」的设备（这些就是用户真正想连的），
        剩下的（未命名的、信号弱的）折叠起来，需要时再展开。
        """
        return self._show_all

    @Property(int, notify=scanChanged)
    def nearbyCount(self) -> int:
        """默认视图里会显示多少个设备。"""
        return self._count_nearby()

    @Property(int, notify=scanChanged)
    def totalCount(self) -> int:
        """一共扫到多少个设备。"""
        return len(self._scan_items)

    @Property(int, notify=remotesChanged)
    def currentRemote(self) -> int:
        return self._current

    @Property(bool, notify=learnChanged)
    def learningActive(self) -> bool:
        return self._learn_active

    @Property(str, notify=learnChanged)
    def learningTarget(self) -> str:
        return self._learn_target

    @Property(str, notify=voiceStateChanged)
    def voiceState(self) -> str:
        return self._voice_state

    @Property(str, notify=voiceStateChanged)
    def voiceText(self) -> str:
        return self._voice_text

    @Property(bool, notify=settingsChanged)
    def voiceEnabled(self) -> bool:
        return bool(self._cfg.get("voice_enabled", True))

    @Property(str, notify=settingsChanged)
    def imeProvider(self) -> str:
        return str(self._cfg.get("ime_provider", "wechat"))

    @Property(str, notify=settingsChanged)
    def voiceSink(self) -> str:
        return str(self._cfg.get("voice_sink", "ime"))

    @Property(str, notify=settingsChanged)
    def imeHotkey(self) -> str:
        return str(self._cfg.get("ime_hotkey", "lctrl+lwin"))

    @Property(bool, notify=settingsChanged)
    def asrEnabled(self) -> bool:
        return bool(self._cfg.get("asr_enabled", True))

    @Property(bool, notify=settingsChanged)
    def outputEnabled(self) -> bool:
        return bool(self._cfg.get("output_enabled", True))

    @Property(bool, notify=settingsChanged)
    def asrAvailable(self) -> bool:
        """本地识别模型是否就位。"""
        from core.resources import models_dir
        d = models_dir()
        try:
            if (d / "tokens.txt").is_file():
                return True
        except OSError:
            pass
        return any(d.glob("*.onnx")) if d.is_dir() else False

    @Property(str, notify=flashLogChanged)
    def lastExportPath(self) -> str:
        return self._last_export

    @Property(bool, notify=flashStateChanged)
    def flashBusy(self) -> bool:
        return self._flash_busy

    @Property(float, notify=flashStateChanged)
    def flashProgress(self) -> float:
        return self._flash_progress

    @Property(str, notify=flashStateChanged)
    def flashLog(self) -> str:
        return "\n".join(self._flash_log[-200:])

    @Property(str, notify=logLevelChanged)
    def logLevel(self) -> str:
        return self._cfg.get("log_level", "INFO")

    @Property(bool, notify=launchAtLoginChanged)
    def launchAtLogin(self) -> bool:
        return startup.read_state().enabled

    @Property("QVariantList", constant=True)
    def actionGroups(self) -> list:
        """
        动作下拉的分组清单（QML 用）。

        展平成 [{label, isHeader}]，组标题 isHeader=True 不可选。
        """
        out: list = []
        for title, options in action_option_groups():
            out.append({"label": title, "isHeader": True})
            for opt in options:
                out.append({"label": opt, "isHeader": False})
        return out

    @Slot(str, result=str)
    def actionLabelFor(self, kind: str) -> str:
        """动作类型 -> 显示名。"""
        try:
            return kind_to_label(ActionKind(kind))
        except ValueError:
            return kind

    # ---------------------------------------------------- 快捷键录制
    @Property(bool, notify=hotkeyCaptureChanged)
    def hotkeyCapturing(self) -> bool:
        return self._hotkey_capturing

    @Property(str, notify=hotkeyCaptureChanged)
    def capturedHotkey(self) -> str:
        return self._captured_hotkey

    @Slot()
    def startHotkeyCapture(self) -> None:
        """开始录制快捷键（全局钩子，不影响前台）。"""
        if hotkey_recorder is None:
            self._set_status("当前平台不支持键盘录制")
            return
        self._captured_hotkey = ""
        # ★ 超时兜底 ★
        #   万一用户忘了取消，钩子不能一直挂着 ——
        #   它会吞掉全系统键盘输入。
        self._capture_deadline = time.monotonic() + CAPTURE_TIMEOUT_SECONDS

        def on_capture(cap) -> None:
            text = "+".join(cap.tokens)
            self._captured_hotkey = text
            self._hotkey_capturing = False
            self.hotkeyCaptureChanged.emit()
            self._set_status(f"已录制：{text}")

        try:
            hotkey_recorder.start(on_capture)
            self._hotkey_capturing = True
            self.hotkeyCaptureChanged.emit()
            self._set_status("正在录制快捷键…")
        except Exception as exc:
            self.log.error("键盘录制启动失败：%s", exc)
            self._set_status(f"键盘录制失败：{exc}")

    @Slot()
    def stopHotkeyCapture(self) -> None:
        self._capture_deadline = 0.0
        if hotkey_recorder is not None:
            try:
                hotkey_recorder.stop()
            except Exception:
                pass
        self._hotkey_capturing = False
        self.hotkeyCaptureChanged.emit()

    @Property(str, constant=True)
    def appVersion(self) -> str:
        from core.version import VERSION
        return VERSION

    @Property(str, constant=True)
    def appTitle(self) -> str:
        """窗口标题 / 托盘名 —— 只在 core/version.py 里定义一处。"""
        from core.version import APP_TITLE
        return APP_TITLE

    @Property(str, constant=True)
    def dataDirectory(self) -> str:
        from core.resources import data_root
        return str(data_root())

    # ------------------------------------------------------------ 串口
    @Slot(result="QVariantList")
    def availablePorts(self) -> list:
        if self._port is None:
            return []
        ports = self._port.list_ports()
        # ★ 优先把 ESP32-S3 排在前面 ★
        #   原生 USB(VID 0x303A) 是协议通道，板载 CH343(0x1A86) 是烧录通道。
        #   协议要用前者 —— 排在前面省得用户选错。
        def rank(p):
            if p.vid == 0x303A:
                return 0
            if p.vid == 0x1A86:
                return 1
            return 2
        ports.sort(key=rank)
        return [{"device": p.device, "label": p.label, "vid": p.vid,
                 "protocol": p.vid == 0x303A,
                 # ★ 烧录通道只认板载 CH343（0x1A86）★
                 #   别的 USB 串口设备（比如某个 vid=2d79 的串口）
                 #   根本不是这块板子的烧录口，列出来只会让人选错。
                 "flash": p.vid == 0x1A86} for p in ports]

    @staticmethod
    def _port_entry(port: dict) -> dict:
        """端口条目：补一个一行能显示完的 text。"""
        entry = dict(port)
        kind = "协议通道" if port.get("protocol") else "烧录通道"
        entry["text"] = "%s — %s" % (port.get("device", ""), kind)
        return entry

    @Property("QVariantList", notify=portCandidatesChanged)
    def protocolPorts(self) -> list:
        """只列**协议通道**（VID 0x303A 的原生 USB）。

        ★ 用户的要求 ★
          「这个位置也只用选择协议通道，因为烧录通道在使用时不用插上」
        """
        return [self._port_entry(p) for p in self.portList
                if p.get("protocol")]

    @Property("QVariantList", notify=portCandidatesChanged)
    def flashPorts(self) -> list:
        """只列**烧录通道**（VID 0x1A86 的板载 CH343）。"""
        return [self._port_entry(p) for p in self.portList
                if p.get("flash")]

    @Property("QVariantList", notify=portCandidatesChanged)
    def portList(self) -> list:
        """
        所有串口（带协议/烧录标注），设备页的下拉用。

        ★ 必须缓存 ★
          availablePorts() 在 Windows 上走 WMI 枚举，单次 10~200 ms，
          而 QML 的绑定会反复读这个属性 —— 每帧枚举几次会把界面卡死。
        """
        if self._port_cache is None:
            self._port_cache = self.availablePorts()
        return self._port_cache

    @Slot(str, result=bool)
    def connectDevice(self, device: str) -> bool:
        if self._port is None:
            return False
        # ★ 已经连着同一个口就别重开 ★
        #   重开会把旧读线程正在读的句柄关掉 —— 那个线程落到失效句柄上，
        #   直接 access violation（v0.15.0 实测：换串口时程序当场消失）。
        if self._connected and self._device_port == device:
            return True
        # ★ 先把上一个连接彻底收干净 ★
        #   旧 ProtocolClient 的读线程不停的话，它会和新客户端抢同一个串口，
        #   应答被它吃掉 → 所有请求超时 → 表现为「刷新没反应」。
        if self._connected or self._proto is not None:
            self.disconnectDevice()
        try:
            self._port.open(device)
        except Exception as exc:
            # ★ 端口被别的程序占着（多半是另一个客户端还在后台）★
            #   说清楚「怎么办」，别只丢一句「打开失败」；
            #   也别每 30 秒刷一条 ERROR（用户实测刷了十几条）。
            busy = isinstance(exc, PermissionError)
            if busy and self._port_busy:
                self.log.info("串口仍被占用：%s", device)
            else:
                self.log.error("串口打开失败 %s: %s", device, exc)
            if busy:
                self._port_busy = True
                self._set_status(f"{device} 被别的程序占着"
                                 "（多半是另一个客户端还在后台）—— 关掉它再点「连接」")
            else:
                self._set_status(f"打开 {device} 失败：{exc}")
            return False
        self._port_busy = False

        self._proto = ProtocolClient(self._port)
        self._proto.on_event(self._on_event)
        self._proto.on_data(self._on_data)
        self._proto.on_log(self._on_board_log)
        # ★ 协议原文全部落到 device.log ★
        #   现场出问题时，这份日志能还原「PC 发了什么、设备回了什么」。
        self._proto.on_raw(self._on_raw_line)
        self._proto.on_sent(self._on_sent_line)
        self._proto.start()
        self.log.info("串口已打开：%s", device)
        if self._port_timer is not None:
            self._port_timer.start()

        self._connected = True
        self.connectedChanged.emit()
        self._set_status(f"已连接 {device}")
        self.log.info("串口已连接：%s", device)

        # ★ 连接配置工具后也允许查看状态；自动运行由固件上电自行启动 ★
        #   之前客户端从来没发过 AUTO，固件的 s_auto 一直是 false，
        #   保留 AUTO 命令兼容旧固件；当前固件上电即自主运行，关闭客户端
        #   不会关闭映射与遥控器连接。
        self._proto.send("AUTO", "1")
        # ★ 语音通道的开关状态也要推给板子 ★
        #   板子那边只存在 RAM 里（现在也持久化了），
        #   PC 这边记的是 config.json 的 voice_enabled ——
        #   两边曾经不一致过就再也对不上：
        #   界面显示「语音通道已开启」，板子其实不按热键 → 输入法没反应。
        self._proto.send("VOICE", 1 if self._cfg.get("voice_enabled", True) else 0)
        self._sync_voice_hotkey()

        # ★ 连上就问固件版本 ★
        #   设备页的「固件版本」是从 INFO 的 fw= 来的，
        #   不主动问的话页面永远显示「未知」。
        self.refreshDeviceInfo()
        self.refreshRemotes()
        return True

    @Slot()
    def disconnectDevice(self) -> None:
        self._auto_connect = False     # 用户主动断开，不再自动重连
        if self._port_timer is not None:
            self._port_timer.stop()
        if self._proto is not None:
            # 兼容旧固件：新固件断开配置串口后仍应自主运行。
            try:
                self._proto.send("AUTO", "1")
            except Exception:
                pass
            self._proto.stop()
            self._proto = None
        if self._port is not None:
            self._port.close()
        self._connected = False
        self._pairing = False
        self.pairingChanged.emit()
        self.connectedChanged.emit()
        # ★ 断开就等于不知道板子的信息了 ★
        #   以前断开后还显示着「固件 0.17.0 / 1 台已连接」——
        #   那是上一轮缓存的，看着像还连着，很不真实。
        self._fw_version = ""
        self._device_info = {}
        self.remoteModel.set_items([])
        self.deviceInfoChanged.emit()
        self.remotesChanged.emit()
        self._set_status("已断开")

    @Slot()
    def shutdown(self) -> None:
        """
        彻底收尾：停定时器 → 停协议线程 → 关串口。

        ★ 退出前必须调 ★
          解释器退出时 QApplication 先死，BridgeController（含它的 QTimer）
          后死 —— QTimer 析构时会去访问已经销毁的事件分发器 →
          access violation。v0.15.0 打包版退出时偶发崩就是这么来的。
        """
        try:
            if self._port_timer is not None:
                self._port_timer.stop()
            if self._log_timer is not None:
                self._log_timer.stop()
        except Exception:
            pass
        proto = self._proto
        self._proto = None
        if proto is not None:
            try:
                proto.stop()
            except Exception:
                pass
        try:
            if self._port is not None:
                self._port.close()
        except Exception:
            pass
        self._connected = False

    # ------------------------------------------------------------ 配对窗口
    @Slot(int)
    def startPairWindow(self, seconds: int = 120) -> None:
        """
        让开发板开一个配对窗口。

        ★ 窗口内板子会自己收新遥控器 ★
          以前必须 PC 发 PAIR 才会写进配对表（pl_put 的唯一入口），
          所以「两边都进配对模式」时板子虽然连上了，PC 端却看着像没反应。
        """
        if self._proto is None:
            self._set_status("未连接开发板")
            return
        self._proto.send("PAIRWINDOW", max(0, int(seconds)))
        self._pair_window_until = time.monotonic() + max(0, int(seconds))
        self.pairWindowChanged.emit()
        self._set_status(f"已开配对窗口（{seconds} 秒）："
                         "请把遥控器长按 Home 进配对模式，板子会自动配上")

    @Slot()
    def stopPairWindow(self) -> None:
        if self._proto is None:
            return
        self._proto.send("PAIRWINDOW", 0)
        self._pair_window_until = 0.0
        self.pairWindowChanged.emit()
        self._set_status("已关闭配对窗口")

    @Property(bool, notify=pairWindowChanged)
    def pairWindowAuto(self) -> bool:
        """板子是不是在「自动等人来配对」。

        ★ 一个遥控器都没配时，固件会自己一直开着配对窗口 ★
          （蓝灯闪着就是在等人）。这时界面要说清楚，
          别让用户以为还得再点一次「进入配对模式」。
        """
        return self._pair_window_auto

    @Property(int, notify=pairWindowChanged)
    def pairWindowRemaining(self) -> int:
        """配对窗口还剩多少秒。"""
        if not self._pair_window_until:
            return 0
        left = int(self._pair_window_until - time.monotonic())
        if left <= 0:
            self._pair_window_until = 0.0
            self.pairWindowChanged.emit()
            return 0
        return left

    # ------------------------------------------------------------ 扫描配对
    def _on_pair_window_state(self, result) -> None:
        """板子报上来的配对窗口状态。"""
        auto = result.get_int("auto") > 0
        if auto != self._pair_window_auto:
            self._pair_window_auto = auto
            self.pairWindowChanged.emit()

    @Slot()
    def startScan(self) -> None:
        if self._proto is None:
            self._set_status("未连接设备")
            return
        self._proto.send("SCAN", 2)
        self._scanning = True
        self.scanChanged.emit()
        self._set_status("正在扫描遥控器…")

    @Slot()
    def stopScan(self) -> None:
        if self._proto is None:
            return
        self._proto.send("SCAN", 0)
        self._scanning = False
        self.scanChanged.emit()
        self._set_status("已停止扫描")

    @Slot(str, int, str, result=bool)
    def pairCandidate(self, addr: str, addr_type: int, name: str) -> bool:
        """
        发起配对：固件连上目标后 NimBLE 自动完成配对并存 bond。

        ★ 地址类型必须一起传过去 ★
          bond 表里记的类型不一定准（遥控器可能用随机地址）。
        ★ 名字要转义 ★
          「Chromecast Remote」有空格，协议是空格分隔的，
          不转义的话固件只会收到 "Chromecast"。
        """
        if self._proto is None:
            self._set_status("未连接设备")
            return False
        self._proto.send("PAIR", addr, int(addr_type), escape(name or ""))
        self._pairing = True
        self._pairing_target = name or addr
        self.pairingChanged.emit()
        self._set_status(
            f"正在配对 {name or addr}…（遥控器可能在睡觉，会持续尝试 2 分钟）")
        return True

    @Slot(str, int, str, result=bool)
    def pairManual(self, addr: str, addr_type: int, name: str) -> bool:
        return self.pairCandidate(addr, addr_type, name)

    @Slot(int, result=bool)
    def removeRemote(self, idx: int) -> bool:
        """
        从配对里删除这一台 —— 列表上只留这一个动作。

        固件那边删的是自己的 NVS 配对表条目（顺带清 bond，如果真有），
        删完就不再自动重连它。想再加回来：重新扫描 + 配对。
        """
        if self._proto is None:
            return False
        item = self.remoteModel.get(idx) if idx < self.remoteModel.rowCount() else None
        name = item.get("name", "") if item else ""
        addr = str(item.get("addr", "")) if item else ""
        # ★ 等板子真的删掉了再改本地 ★
        #   以前先删本地再发命令，板端若失败（比如 idx 对不上），
        #   下一次刷新它又回来了 —— 用户看着像「删不掉」。
        # ★ 必须传板子的配对表槽位号，不是界面行号 ★
        #   板子把参数当成它自己 NVS 配对表的下标，
        #   界面行号跟它对不上就删错台 —— 用户看着像「删不掉」。
        pl_idx = int(item.get("pl_idx", -1)) if item else -1
        if pl_idx < 0:
            pl_idx = idx
        self._ask("UNPAIR", pl_idx, timeout=3.0,
                  on_ok=lambda r: self._after_unpair(idx, addr, name))
        self._set_status(f"正在删除 {name or idx} …")
        return True

    def _after_unpair(self, idx: int, addr: str, name: str) -> None:
        """UNPAIR 成功后的收尾。"""
        remotes_all = [r for r in self._cfg.get("remotes", [])
                       if isinstance(r, dict)]
        removed = [r for r in remotes_all if r.get("addr") == addr]
        remotes = [r for r in remotes_all if r.get("addr") != addr]
        self._cfg["remotes"] = remotes
        # ★ 归档也要删 ★
        #   归档是按 identity 存的 —— 不删的话，同一个遥控器重新配对后
        #   _archived_bindings() 会命中旧归档，把旧映射推回板子，
        #   出厂默认永远轮不上（「重配」就不该是「老熟人」）。
        idents = {str(r.get("identity", "") or "") for r in removed}
        archive = dict(self._cfg.get("mapping_archive", {}) or {})
        for key in list(archive.keys()):
            entry = archive.get(key)
            if not isinstance(entry, dict):
                continue
            same_addr = str(entry.get("addr", "") or "") == addr
            same_ident = (str(entry.get("identity", "") or "") in idents
                          and str(entry.get("identity", "") or ""))
            if same_addr or same_ident:
                archive.pop(key, None)
        self._cfg["mapping_archive"] = archive
        cfgmod.save_config(self._cfg)
        self._rebuild_remote_model()
        if self._proto is not None:
            self._proto.send("SCAN", 0)
        self._set_status(f"已从配对中删除 {name or idx}")
        self.refreshRemotes()

    @Slot()
    def refreshRemotes(self) -> None:
        """
        重新拉遥控器列表。

        ★ 用 request 等应答 ★
          以前用 send()，板子不回应时界面毫无变化，用户完全无法分辨
          「没反应」和「真的没变」。
        """
        if self._proto is None:
            self.remoteModel.set_items([])
            self._set_status("未连接开发板")
            return
        self._board_addrs.clear()
        self._ask("REMOTES", timeout=3.0, on_ok=self._on_remotes_listed)

    def _on_remotes_listed(self, result) -> None:
        self._set_status(f"已刷新遥控器列表（{result.get_int('count')} 台）")
        # 新配对：本机一条映射都没有、但这个型号存过默认 → 自动套用
        self._apply_model_defaults()

    @Slot()
    def refreshEverything(self) -> None:
        """「刷新」= 固件版本 + 遥控器列表 + 按键映射，三件一起拉。"""
        if self._proto is None:
            self._set_status("未连接开发板")
            return
        self.refreshDeviceInfo()
        self.refreshRemotes()
        self.loadMappingFromBoard()

    @Slot()
    def loadMappingFromBoard(self) -> None:
        """
        把开发板上生效的映射读回来。

        ★ 以前只有「写」没有「读」★
          按键页显示的一直是 PC 本地那份配置 ——
          板子上明明存着 Home→Ctrl+L，界面却全是「未设置」。

        ★ 还要先有一台「当前遥控器」★
          _current 一直是 -1 → currentRemoteAddr(-1) 返回空 →
          MAPGET 根本没发出去（板子明明存着 3 条绑定）。
        """
        if self._proto is None:
            return
        self._ensure_current_remote()
        addr = self.currentRemoteAddr(self._current)
        if not addr:
            return
        self._collecting_map = True
        self._ask("MAPGET", addr, timeout=4.0,
                  on_ok=lambda r: self._apply_board_mapping(r, addr))

    def _ensure_current_remote(self) -> None:
        """
        保证有一台「当前遥控器」。

        以前 _current 启动后一直是 -1，谁也没去设它，
        于是 MAPGET 取不到地址 → 板子上的映射一条也读不回来。
        """
        count = self.remoteModel.rowCount()
        if count == 0:
            return
        if 0 <= self._current < count:
            return
        # 优先选当前连着的那台
        idx = -1
        for i in range(count):
            if self.remoteModel.get(i).get("connected"):
                idx = i
                break
        if idx < 0:
            idx = 0
        self._current = idx
        self.remotesChanged.emit()

    def _apply_board_mapping(self, result, addr: str) -> None:
        """读回开发板上的映射。

        ★ PC 是唯一真值，板子是镜像 ★
          改映射就整份推给板子，不需要再比一遍两边。
          这里只在「本机还是空的」时采纳一次板子上的内容
          （刚配对 / 刚刷完固件），之后永远以本机为准。
        """
        if not self._collecting_map:
            return
        self._collecting_map = False
        count = result.get_int("count")
        board = dict(self._board_mapping)
        self._board_mapping.clear()

        local = self._remote_bindings(self._current)
        if local:
            # 本机有配置 → 以本机为准；条数不一样就把板子同步成镜像
            if count != len(firmware_bindings(local)):
                # ★ 一定要用 local 推，不能用 self._bindings ★
                #   self._bindings 是内存里「当前选中遥控器」的那份，
                #   刚连上还没选遥控器时它是空的 ——
                #   发出去就成了「MAPBEGIN + MAPCOMMIT、零条 MAPSET」，
                #   **把板子上原有的映射整个清空**。
                self._bindings = local
                self._push_bindings_to_firmware()
            return
        # 本机是空的 → 板子上有就采纳板子的，没有就铺出厂默认
        if count > 0:
            self._bindings = self._bindings_from_board(board)
            self._save_bindings(write_archive=True, push=False)
            self._load_mappings()
            self._set_status(f"已从开发板读回 {count} 条映射")
        else:
            self._bindings = self._default_bindings_for_current()
            self._save_bindings(write_archive=True, push=True)
            self._load_mappings()
            self._set_status("已下发内置默认映射")

    @Slot(int, result=bool)
    def syncMappingToBoard(self, index: int) -> bool:
        """把第 index 台遥控器的**本机**映射推给开发板。

        设备页每一行有个「同步」按钮 —— 本机与板子不一致时，
        用它把本机这份覆盖过去。
        """
        if self._proto is None:
            self._set_status("未连接开发板")
            return False
        remotes = self._cfg.get("remotes", [])
        if not (0 <= index < len(remotes)) or not isinstance(remotes[index], dict):
            self._set_status("这台遥控器不在名单里")
            return False
        addr = str(remotes[index].get("addr", "") or "")
        if not addr:
            self._set_status("这台遥控器没有地址")
            return False
        bindings = remotes[index].get("bindings", {})
        if not isinstance(bindings, dict):
            bindings = {}
        count = len(firmware_bindings(bindings))
        if count <= 0:
            self._set_status("本机没有可下发的映射")
            return False
        # 借当前遥控器的通道下发：切到那一台 → 推 → 切回来
        # ★ _current 和 _bindings 都要恢复 ★
        #   只恢复 _current 的话，按键页会留着别的遥控器的映射。
        old = self._current
        old_bindings = self._bindings
        try:
            self._current = index
            self._bindings = bindings
            self._push_bindings_to_firmware()
        finally:
            self._current = old
            self._bindings = old_bindings
        # ★ 界面上的「板子 N 条」要立刻变 ★
        #   否则用户点了同步看着什么都没发生。
        self.remoteModel.update_item(index, binds_board=count)
        self.refreshRemotes()          # 再问板子要一次真实条数
        self._set_status(f"已把本机 {count} 条映射同步到开发板")
        return True

    # ---- 映射比对（给「映射比对」弹窗用）--------------------------
    @Property("QVariantList", notify=mappingsChanged)
    @Property(str, notify=mappingsChanged)
    @Slot(str, result=bool)
    def _token_text(self, tokens) -> str:
        """键序列 → 人话。"""
        if not tokens:
            return ""
        action = self._action_from_tokens(tuple(tokens))
        if action is not None and action.kind != ActionKind.KEY_COMBO:
            return kind_to_label(action.kind)
        return "+".join(str(t) for t in tokens)

    # ---- 映射比对的小工具 --------------------------------------------
    @staticmethod
    def _action_from_tokens(tokens: tuple[str, ...]) -> Optional[ButtonAction]:
        """板子上的一串 token → 本机的动作。"""
        if not tokens:
            return None
        if len(tokens) == 1:
            for kind, toks in SEMANTIC_KEY_TOKENS.items():
                if tuple(toks) == tokens:
                    return ButtonAction(kind)
        return ButtonAction(ActionKind.KEY_COMBO, keys=tuple(tokens))

    def _bindings_from_board(self, board: dict) -> dict:
        """板子上读回的映射 → 本机的 bindings。"""
        bindings: dict = {}
        for (key_id, trig), tokens in board.items():
            action = self._action_from_tokens(tuple(tokens))
            trigger = INDEX_TRIGGER.get(trig)
            if action is None or trigger is None:
                continue
            bindings = set_button_action(bindings, key_id, trigger, action)
        return bindings

    def _merge_board_into_local(self, local: dict, board: dict) -> dict:
        """把开发板上的映射并进本机。

        ★ 规则（"用板子的"就是这套）★
          · 板子上有的键 → 以板子为准
          · 本机有、板子上没有、而且**固件能下发**的 → 删掉
            （留着的话两边永远对不上，冲突弹窗会一直反复弹）
          · 本机独有、**固件下发不了**的（PC 才能做的动作，
            比如「Netflix → 打开终端」）→ 保留
          · 备注、双击/长按里固件下不下来的部分 → 保留
        """
        merged: dict = {}
        if isinstance(local, dict):
            for k, v in local.items():
                merged[k] = dict(v) if isinstance(v, dict) else v

        board_view: dict = {}
        for (key_id, trig), tokens in board.items():
            board_view[(key_id, trig)] = tuple(tokens)
            action = self._action_from_tokens(tuple(tokens))
            trigger = INDEX_TRIGGER.get(trig)
            if action is None or trigger is None:
                continue
            merged = set_button_action(merged, key_id, trigger, action)

        # 本机有、板子没有、固件又能下发的 → 删掉（否则两边对不上）
        for key_id, trig, _tokens in list(firmware_bindings(merged)):
            if (key_id, trig) in board_view:
                continue
            trigger = INDEX_TRIGGER.get(trig)
            if trigger is None:
                continue
            merged = clear_button_action(merged, key_id, trigger)
        return merged

    @staticmethod
    def _same_as_board(bindings: dict, board: dict) -> bool:
        """本机的映射和板子上的是不是一回事（按固件能看到的键序列比）。"""
        local = {}
        for key_id, trig, tokens in firmware_bindings(bindings):
            local[(key_id, trig)] = tuple(tokens)
        remote = {}
        for (key_id, trig), tokens in board.items():
            remote[(key_id, trig)] = tuple(tokens)
        return local == remote

    def _default_bindings_for_current(self) -> dict:
        """当前遥控器这个型号的出厂默认（按型号分档）。"""
        # ★ 存过型号默认就用它 ★
        template = self._model_default_bindings()
        if template:
            self._bindings = template
            self._save_bindings()
            self._load_mappings()
            self._set_status("已恢复本机保存的型号默认映射")
            return True
        actions = remote_layout.DEFAULT_ACTIONS
        long_actions = remote_layout.DEFAULT_LONG_ACTIONS
        prof = remote_layout.profile_for_name(self._current_remote_name())
        if prof is not None:
            actions = prof["actions"]          # type: ignore[assignment]
            long_actions = prof.get("long_actions") or long_actions
        bindings: dict = {}
        for button_id, label in actions.items():
            key_id = remote_layout.button_key_id(button_id)
            if not key_id:
                continue          # 语音键没有 HID usage
            kind = label_to_kind(label)
            action = ButtonAction(kind if kind is not None else ActionKind.DISABLED)
            bindings = set_button_action(bindings, key_id,
                                         ButtonTrigger.SINGLE_CLICK, action)
        # ★ 长按也要铺 ★
        #   返回键长按 = 退格（连着删）。固件的出厂默认里也有这一条，
        #   两边必须一致，否则条数对不上、又会弹冲突窗。
        for button_id, label in (long_actions or {}).items():
            key_id = remote_layout.button_key_id(button_id)
            if not key_id:
                continue
            kind = label_to_kind(label)
            action = ButtonAction(kind if kind is not None else ActionKind.DISABLED)
            bindings = set_button_action(bindings, key_id,
                                         ButtonTrigger.LONG_PRESS, action)
        return bindings

    def _current_remote_name(self) -> str:
        if 0 <= self._current < self.remoteModel.rowCount():
            return str(self.remoteModel.get(self._current).get("name", "") or "")
        return ""

    @Slot()
    def selfTest(self) -> None:
        """
        ★ 硬件自检 ★

        让板子造一个假的按键事件：RGB 灯会闪，PC 侧也会真的学到一个键。
        用来把「按键 → 灯 → PC」这条链路，和「遥控器到底发不发通知」
        这件事分开验证 —— 自检能过就说明我们的链路没问题。
        """
        if self._proto is None:
            self._set_status("未连接设备")
            return
        self._ask("TEST", timeout=2.0, on_ok=lambda _r: self._append_log(
            human_log.stamp() + " 按键自检：板子已执行，灯闪一次，"
            "PC 应收到一个 a 键"))
        self._set_status("已发送自检：板子的灯应该闪一下")

    @Slot(int, result=bool)
    def selectRemote(self, idx: int) -> bool:
        if idx < 0:
            return False
        self._current = idx
        self.remotesChanged.emit()
        self._rebuild_remote_model()
        self._load_mappings()
        # ★ 切换遥控器时读板子，不是写板子 ★
        #   以前这里直接把本地那份下发过去 —— 本地要是空的，
        #   板子上已经配好的键就被清空了。板子上有就以板子为准。
        self.loadMappingFromBoard()
        return True

    @Slot(int, result=str)
    def currentRemoteAddr(self, idx: int) -> str:
        return str(self.remoteModel.get(idx).get("addr", ""))

    @Slot(int, str, result=bool)
    def setRemoteName(self, idx: int, name: str) -> bool:
        """
        给已配对的遥控器起一个用户认得的名字（配对页列表用）。

        ★ 为什么单独存 custom_name ★
            固件每次上报遥控器都带广播名，而 Chromecast 那一堆设备
            广播名全是 "HID_Device" —— 直接写 name 会被下一次上报冲掉。
            多台同型号遥控器本来就只能靠用户自己给的名字区分。
        """
        remotes = list(self._cfg.get("remotes", []))
        if not (0 <= idx < len(remotes)) or not isinstance(remotes[idx], dict):
            return False
        clean = name.strip()[:40]
        remotes[idx]["custom_name"] = clean
        if clean:
            remotes[idx]["name"] = clean
        self._cfg["remotes"] = remotes
        cfgmod.save_config(self._cfg)
        self._rebuild_remote_model()
        self._set_status(f"已重命名为 {clean}" if clean
                         else "已清除自定义名字，下次上报恢复广播名")
        return True

    # ------------------------------------------------------------ 学习
    @Slot()
    def startLearn(self) -> None:
        if self._proto is None:
            # ★ 别静默失败 ★
            #   以前直接 return，点一下什么都没有 —— 用户以为按钮坏了。
            self._set_status("未连接开发板，无法检测真实按键")
            return
        self._learn_active = True
        self._learn_target = ""
        self._learn_result = ""
        # 通用学习：不绑到某个标记上（否则会把上一次没学完的标记顶掉）
        self.learnChanged.emit()
        self._proto.send("LEARN", self._current, 1)
        self._set_status("按键学习中：按下遥控器上的一个键…")

    @Slot()
    def stopLearn(self) -> None:
        self._learn_active = False
        self._learn_target = ""
        self.learnChanged.emit()
        if self._proto is not None:
            self._proto.send("LEARN", self._current, 0)
        self._set_status("已退出学习")

    @Property(str, notify=learnChanged)
    def learnResultText(self) -> str:
        """最近一次检测到的键（中文键名）。"""
        return self._learn_result

    # ------------------------------------------------------------ 映射
    def _load_mappings(self) -> None:
        """
        按固定档案生成按键页的行。

        每一行 = 一个物理键，三格分别是单击 / 双击 / 长按的动作。
        """
        self._bindings = self._remote_bindings(self._current)
        items = []
        for button_id in remote_layout.BUTTON_ORDER:
            key_id = remote_layout.button_key_id(button_id)
            is_voice = remote_layout.is_voice_button(button_id)
            side = "right" if button_id in remote_layout.RIGHT_BUTTON_IDS else "left"
            if is_voice:
                # 语音键不走 HID，它由 ATVV 控制通道驱动，映射在语音页配
                single = self._voice_key_label()
                double = ""
                long = ""
            else:
                single = self._action_text(key_id, ButtonTrigger.SINGLE_CLICK)
                double = self._action_text(key_id, ButtonTrigger.DOUBLE_CLICK)
                long = self._action_text(key_id, ButtonTrigger.LONG_PRESS)
            items.append({
                "button_id": button_id,
                "button_name": remote_layout.display_name(button_id),
                "key_id": key_id,
                "single": single,
                "double": double,
                "long": long,
                "single_note": display_note(self._bindings, key_id,
                                             ButtonTrigger.SINGLE_CLICK),
                "double_note": display_note(self._bindings, key_id,
                                            ButtonTrigger.DOUBLE_CLICK),
                "long_note": display_note(self._bindings, key_id,
                                          ButtonTrigger.LONG_PRESS),
                "short_name": remote_layout.short_name(button_id),
                "is_voice": is_voice,
                "visual_row": remote_layout.visual_row(button_id),
                "side": side,
            })
        self._button_items = items
        self.mappingModel.set_items(items)
        self.mappingsChanged.emit()
    def _remote_bindings(self, idx: int) -> dict:
        remotes = self._cfg.get("remotes", [])
        if 0 <= idx < len(remotes) and isinstance(remotes[idx], dict):
            b = remotes[idx].get("bindings", {})
            return b if isinstance(b, dict) else {}
        return {}

    def _remote_keys(self) -> dict:
        """
        遥控器的物理键表：key_id -> 显示名。

        ★ 来源是**固定档案**（core/remote_layout.py）★
          谷歌 Chromecast 遥控器有哪几个键、各自的 HID usage 是多少，
          都是这只遥控器固有的 —— 不该让用户自己标注。
          打开按键页就有完整的一列键可配。
        """
        out: dict[str, str] = {}
        for button_id in remote_layout.BUTTON_ORDER:
            key_id = remote_layout.button_key_id(button_id)
            if not key_id:
                continue          # 语音键没有 HID usage
            out[key_id] = remote_layout.display_name(button_id)
        return out
    def _action_text(self, key_id: str, trigger: ButtonTrigger) -> str:
        action = button_action_for(self._bindings, key_id, trigger)
        if action.kind == ActionKind.DISABLED:
            return ""
        if action.kind == ActionKind.OPEN_APP:
            for entry in self._custom_apps():
                if entry["path"] == action.uri:
                    return entry["label"]
            return "打开应用：%s" % action.uri
        if action.kind == ActionKind.KEY_COMBO:
            return "+".join(action.keys)
        return kind_to_label(action.kind)

    @Slot(str, str, str, result=bool)
    def setMapping(self, key_id: str, trigger: str, text: str) -> bool:
        """把一段文字（动作名或 "+" 分隔的组合键）写进映射。"""
        try:
            t = ButtonTrigger(trigger)
        except ValueError:
            return False
        action = self._parse_action(text)
        if action is None:
            return False
        self._bindings = set_button_action(self._bindings, key_id, t, action)
        self._save_bindings()
        self._load_mappings()
        return True

    @Slot(str, str, str, result=bool)
    def setMappingNote(self, key_id: str, trigger: str, note: str) -> bool:
        """
        写某个手势的备注名。

        ★ 以前只有 (key_id, note)，备注只能挂在单击上 ★
          参考项目的编辑器是三行三列 —— 单击/双击/长按各有一列备注。
        """
        try:
            t = ButtonTrigger(trigger)
        except ValueError:
            return False
        self._bindings = set_display_note(self._bindings, key_id, t, note)
        self._save_bindings()
        self._load_mappings()
        return True

    @Slot(str, str, result=bool)
    def setMappingNoteLegacy(self, key_id: str, note: str) -> bool:
        self._bindings = set_display_note(self._bindings, key_id,
                                          ButtonTrigger.SINGLE_CLICK, note)
        self._save_bindings()
        self._load_mappings()
        return True

    @Slot(str, str, result=bool)
    def removeMapping(self, key_id: str, trigger: str) -> bool:
        try:
            t = ButtonTrigger(trigger)
        except ValueError:
            return False
        self._bindings = clear_button_action(self._bindings, key_id, t)
        self._save_bindings()
        self._load_mappings()
        return True

    def _parse_action(self, text: str) -> Optional[ButtonAction]:
        """
        把用户输入/选中的文本解析成动作。

        优先按**显示名**匹配（"显示桌面"、"按住说话"…），
        认不出来就当自定义组合键（"ctrl+shift+p"）。
        """
        text = (text or "").strip()
        if not text:
            return ButtonAction(ActionKind.DISABLED)

        kind = label_to_kind(text)
        if kind is not None:
            return ButtonAction(kind)

        # 用户自定义的启动项：按显示名找到路径
        for entry in self._custom_apps():
            if entry["label"] == text:
                return ButtonAction(ActionKind.OPEN_APP, uri=entry["path"])

        try:
            return ButtonAction(ActionKind(text))
        except ValueError:
            pass

        keys = tuple(k.strip().lower() for k in text.split("+") if k.strip())
        if keys:
            return ButtonAction(ActionKind.KEY_COMBO, keys)
        return None

    def _archive_key(self, entry: dict) -> str:
        """映射归档的键：identity（固定 MAC）优先，没有就用地址。"""
        return (str(entry.get("identity", "") or "")
                or str(entry.get("addr", "") or ""))

    def _archived_bindings(self, addr: str) -> dict:
        """按地址或 identity 在归档里找那台遥控器的映射。"""
        archive = self._cfg.get("mapping_archive", {})
        if not isinstance(archive, dict):
            return {}
        for saved in archive.values():
            if isinstance(saved, dict) and str(saved.get("addr", "")) == addr:
                b = saved.get("bindings")
                if isinstance(b, dict) and b:
                    return b
        ident = ""
        for i in range(self.remoteModel.rowCount()):
            r = self.remoteModel.get(i)
            if str(r.get("addr", "") or "") == addr:
                ident = str(r.get("identity", "") or "")
                break
        if ident:
            saved = archive.get(ident)
            if isinstance(saved, dict):
                b = saved.get("bindings")
                if isinstance(b, dict) and b:
                    return b
        return {}

    def _save_bindings(self, write_archive: bool = True, push: bool = True) -> None:
        """
        保存本机映射。

        ★ write_archive=False ★
          「从开发板读回来」这种场合**不能**覆盖归档 ——
          归档是用户配置的备份，用板子读回的内容盖它，
          等于把备份也变成「板子上那份」，
          「板子空 → 按归档恢复」这条路当场失效
          （以前就是这么把用户 15 条配置弄丢的）。

        ★ push=False ★
          不动开发板 —— 板子上那份才是准的，
          或者还在等用户在弹窗里做选择。
        """
        remotes = list(self._cfg.get("remotes", []))
        if 0 <= self._current < len(remotes) and isinstance(remotes[self._current], dict):
            remotes[self._current]["bindings"] = self._bindings
            self._cfg["remotes"] = remotes
            # ★ 归档一份（按 identity / 固定 MAC）★
            #   全新刷写会清掉板子 NVS 里的映射；
            #   遥控器重新配对后靠 identity 认出是同一台，
            #   这份归档就能原样推回去，用户不用重新配一遍。
            if write_archive:
                key = self._archive_key(remotes[self._current])
                if key:
                    archive = dict(self._cfg.get("mapping_archive", {}))
                    entry = remotes[self._current]
                    archive[key] = {
                        "addr": str(entry.get("addr", "") or ""),
                        "identity": str(entry.get("identity", "") or ""),
                        "name": str(entry.get("custom_name", "")
                                    or entry.get("name", "") or ""),
                        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "bindings": self._bindings,
                    }
                    self._cfg["mapping_archive"] = archive
            cfgmod.save_config(self._cfg)
            if push:
                self._push_bindings_to_firmware()

    def _push_bindings_to_firmware(self) -> None:
        """把能下发到固件的那部分映射推给设备（PC 不在场时也能用）。"""
        if self._proto is None:
            return
        remote = self.remoteModel.get(self._current)
        addr = str((remote or {}).get("addr", ""))
        if not addr:
            return
        # ★ 一条都没有就别下发 ★
        #   MAPBEGIN 会清空固件的暂存区，MAPCOMMIT 再把暂存区写进去 ——
        #   零条 MAPSET + 一次 MAPCOMMIT = **把板子上的映射清空**。
        #   「清空映射」只该由解绑（UNPAIR）来做。
        pending = list(firmware_bindings(self._bindings))
        if not pending:
            self.log.warning("本机没有可下发的映射，跳过下发（避免清空开发板）")
            return
        # ★ 一条一条等 OK，不能连发 ★
        #   实测：MAPBEGIN + 15×MAPSET 一口气写下去，最后的 MAPCOMMIT 会丢
        #   —— 板子暂存区满了却没提交，表现就是「本机改了、板子上还是旧映射」
        #   （用户实测：Netflix 设成 Alt+A，按下去还是 Win+E）。
        #   逐条等应答时 100% 成功。
        def _send_wait(verb: str, *args) -> bool:
            result = self._proto.request(verb, *args, timeout=1.0)
            if not result.ok:
                self.log.warning("下发 %s 未确认（%s）", verb, result.reason)
                return False
            return True

        if not _send_wait("MAPBEGIN", addr):
            return
        for row in build_map_rows(self._bindings):
            if not _send_wait("MAPSET", *row):
                self.log.error("下发中断，板子保持原映射")
                return
        _send_wait("MAPCOMMIT")

    @staticmethod
    def _local_path(url: str) -> Path:
        """
        把 QML FileDialog 给的 URL 变成本地路径。

        ★ 不能只用 replace("file://", "") ★
            Qt6 给的是 file:///C:/Users/... ，去掉 "file://" 剩下一开头的斜杠，
            Windows 会把它当成「当前盘的根目录下的 C:/...」，
            is_file() 永远 False —— 表现就是「选了图片但什么都没发生」。
            url2pathname 才是标准做法，顺带处理百分号转义（中文/空格路径）。
        """
        from urllib.parse import unquote, urlsplit
        from urllib.request import url2pathname

        s = url.strip()
        if not s:
            return Path()
        if s.startswith("file:") or s.startswith("file:/"):
            return Path(url2pathname(unquote(urlsplit(s).path)))
        return Path(s)

    @Slot(int, result=str)
    def remoteImageSource(self, idx: int) -> str:
        """遥控器实物图的 file:// URL。

        ★ 用随包的那张固定档案图 ★
          上传平面图 + 标标记的老做法已经去掉（参考项目也没有这套），
          实物图与热点坐标是配套的，必须用同一张。
        """
        from core.resources import assets_dir
        p = assets_dir() / "remotes" / "Chromecast-remote-photo.png"
        if not p.is_file():
            return ""
        return p.as_uri()
    # ------------------------------------------------------------ 语音
    @Slot(bool, result=bool)
    def setVoiceEnabled(self, on: bool) -> bool:
        self._cfg["voice_enabled"] = bool(on)
        cfgmod.save_config(self._cfg)
        if self._proto is not None:
            self._proto.send("VOICE", 1 if on else 0)
        self._voice_state = "语音通道已开启" if on else "语音通道已关闭"
        self.voiceStateChanged.emit()
        return True

    @Slot(bool, result=bool)
    def setAsrEnabled(self, on: bool) -> bool:
        self._cfg["asr_enabled"] = bool(on)
        cfgmod.save_config(self._cfg)
        self.settingsChanged.emit()
        return True

    @Slot(bool, result=bool)
    def setOutputEnabled(self, on: bool) -> bool:
        self._cfg["output_enabled"] = bool(on)
        cfgmod.save_config(self._cfg)
        if self._proto is not None:
            self._proto.send("OUTPUT", 1 if on else 0)
        self.settingsChanged.emit()
        return True

    @Slot(str, result=bool)
    def setVoiceModelPath(self, path: str) -> bool:
        self._cfg["voice_model_path"] = path
        cfgmod.save_config(self._cfg)
        return True

    @Slot(str, result=bool)
    def setVoiceSink(self, sink: str) -> bool:
        self._cfg["voice_sink"] = sink
        cfgmod.save_config(self._cfg)
        self.settingsChanged.emit()
        return True

    @Slot(str, result=bool)
    def setImeProvider(self, provider: str) -> bool:
        if provider not in {"none", "sogou", "wechat", "doubao", "custom"}:
            return False
        self._cfg["ime_provider"] = provider
        cfgmod.save_config(self._cfg)
        self.settingsChanged.emit()
        return True

    @Slot(str, result=bool)
    def setImeHotkey(self, hotkey: str) -> bool:
        tokens = [part.strip().lower() for part in hotkey.split("+") if part.strip()]
        if not tokens:
            return False
        if len(tokens) > 8 or any(hid_usage_for_token(token) is None for token in tokens):
            return False
        self._cfg["ime_hotkey"] = "+".join(tokens)
        # ★ 标记「用户改过」★
        #   改过之后，连上板子才允许覆盖板子上的语音键；
        #   没改过就只读不写，板子上生效的值原样保留。
        self._cfg["ime_hotkey_custom"] = True
        cfgmod.save_config(self._cfg)
        self._push_voice_hotkey_to_firmware()
        self.settingsChanged.emit()
        return True

    def _sync_voice_hotkey(self) -> None:
        """
        连上板子时的语音键同步。

        ★ 没在软件里改过 → 只读，绝不覆盖 ★
          板子的默认值就是左Ctrl+左Win，用户在板子上改过的也要保留 ——
          PC 端配置里的旧值不该把它冲掉。
          用户在软件里改过（ime_hotkey_custom=True）才下发。
        """
        if self._proto is None:
            return
        if not self._cfg.get("ime_hotkey_custom", False):
            self.log.info("语音快捷键未在软件里改过，只读板子当前值，不覆盖")
            self._ask("VOICEKEY", timeout=3.0, on_ok=self._on_voice_key)
            return
        self._push_voice_hotkey_to_firmware()

    def _push_voice_hotkey_to_firmware(self) -> None:
        if self._proto is None:
            return
        hotkey = str(self._cfg.get("ime_hotkey", "lctrl+lwin"))
        mods = 0
        keys = []
        for token in (part.strip().lower() for part in hotkey.split("+") if part.strip()):
            usage = hid_usage_for_token(token)
            if usage is None:
                self.log.warning("Skip invalid voice hotkey token %r", token)
                return
            if 0xE0 <= usage <= 0xE7:
                mods |= 1 << (usage - 0xE0)
            else:
                keys.append(usage)
        # 先更新本地显示，再发板子 —— 回读有延迟，
        # 中间会短暂显示「未设置」，用户以为没改成
        self._voice_key_mods = mods
        self._voice_key_keys = list(keys)
        self.voiceKeyChanged.emit()
        self._proto.send("VOICEKEY", mods, len(keys), *keys)
        # 回读确认（板子存的和发的一致）
        self._ask("VOICEKEY", timeout=2.0, on_ok=self._on_voice_key)

    @Slot(result="QVariantList")
    def availableAudioEndpoints(self) -> list:
        if audio_player is None:
            return []
        return [{"index": e.index, "name": e.name, "cable": e.is_cable}
                for e in audio_player.list_outputs()]

    # ------------------------------------------------------------ 设置
    @Slot(str, result=bool)
    def setLogLevel(self, level: str) -> bool:
        level = level.upper()
        if level not in logging_setup.LOG_LEVELS:
            return False
        self._cfg["log_level"] = level
        cfgmod.save_config(self._cfg)
        logging_setup.manager().set_level(level)
        self.logLevelChanged.emit()
        return True

    @Slot(bool, result=bool)
    def setLaunchAtLogin(self, on: bool) -> bool:
        state = startup.set_enabled(bool(on))
        self._cfg["launch_at_login"] = state.enabled
        cfgmod.save_config(self._cfg)
        self.launchAtLoginChanged.emit()
        return state.enabled

    @Slot(result=str)
    def exportLogs(self) -> str:
        from core import log_export
        try:
            path = log_export.export_logs()
            self._set_status(f"日志已导出：{path}")
            return str(path)
        except Exception as exc:
            self.log.error("导出日志失败: %s", exc)
            self._set_status(f"导出日志失败：{exc}")
            return ""

    @Slot(result=bool)
    def openLogFolder(self) -> bool:
        from core import log_export
        ok = log_export.open_logs_folder()
        if ok:
            self._set_status("已打开日志目录")
        else:
            self._set_status("日志目录还不存在（先点「实时日志」或跑一次操作）")
        return ok

    # ------------------------------------------------------------ 映射导入导出
    @Slot(result=str)
    def exportMappingFile(self) -> str:
        """把当前遥控器的映射导出成 JSON（落到数据目录）。"""
        return self.exportMappingFileTo("")

    @Slot(str, result=str)
    def exportMappingFileTo(self, path: str) -> str:
        """导出映射到指定路径。

        格式对齐参考项目（RemoteMic.key-mapping）：
            {"format":"RemoteMic.key-mapping","schema_version":1,
             "name":…,"profile":…,"mapping":{bindings,secondary_bindings,display_notes}}
        旧格式（version/remote/bindings）也一起写上，别的工具能读。
        """
        import json
        from core.resources import data_root
        remotes = self._cfg.get("remotes", [])
        name = ""
        if 0 <= self._current < len(remotes) and isinstance(remotes[self._current], dict):
            name = str(remotes[self._current].get("name", "") or "")
        profile = remote_layout.profile_for_name(name)
        payload = {
            "format": "RemoteMic.key-mapping",
            "schema_version": 1,
            "name": name,
            "profile": str(profile.get("id", "")) if profile else "",
            "mapping": self._bindings,
            "version": 1,
            "remote": name,
            "bindings": self._bindings,
        }
        target = (Path(str(path)) if path
                  else data_root() / f"mapping_{int(time.time())}.json")
        try:
            if target.parent and str(target.parent):
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except OSError as exc:
            self._set_status(f"导出映射失败：{exc}")
            return ""
        self._set_status(f"已导出映射：{target}")
        return str(target)

    @Slot(str, result=bool)
    def importMappingFile(self, path: str) -> bool:
        """从 JSON 导入映射，并立刻写回开发板。"""
        import json
        from urllib.parse import unquote, urlsplit
        from urllib.request import url2pathname
        p = Path(str(path))
        if not p.is_file():
            try:
                p = Path(url2pathname(unquote(urlsplit(str(path)).path)))
            except Exception:
                p = Path(str(path))
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            self._set_status(f"导入映射失败：{exc}")
            return False
        bindings = None
        if isinstance(data, dict):
            # 参考项目格式：mapping:{bindings,secondary_bindings,display_notes}
            mapping = data.get("mapping")
            if isinstance(mapping, dict):
                bindings = mapping
            else:
                # 旧格式：bindings
                bindings = data.get("bindings")
        if not isinstance(bindings, dict):
            self._set_status("导入失败：文件里没有映射")
            return False
        self._bindings = bindings
        self._save_bindings()          # 内部推给板子
        self._load_mappings()
        self._set_status("映射已导入并写回开发板")
        return True

    @Slot(result=str)
    def bundledFirmwareDir(self) -> str:
        from core.resources import firmware_dir
        return str(firmware_dir())

    # ------------------------------------------------------------ 固件烧录
    @Slot(result="QVariantList")
    def refreshDevices(self) -> list:
        """列出可用于烧录的串口。"""
        try:
            from core.flasher import FirmwareFlasher
            return FirmwareFlasher.list_ports()
        except Exception as exc:
            self.log.warning("枚举烧录设备失败: %s", exc)
            return []

    @Slot("QVariant", result="QVariantList")
    def pickFirmwareFiles(self, paths) -> list:
        """登记待烧录文件，返回 [{path, offset}] 供 UI 显示。"""
        from core.flasher import FirmwareFlasher
        # ★ 三种形态都要吃 ★
        #   pickFirmwareFiles() 给界面的是 [{path, offset}] 字典；
        #   QML FileDialog 给的是 file:// URL；直接调用给字符串。
        #   以前一律 str(p) —— 字典会变成 "{'path': ...}"，报「文件不存在」。
        raw = [as_local_path(p) for p in (paths or [])]
        files = [Path(x) for x in raw if x]
        if not files:
            files = self._bundled_firmware_files()
        if not files:
            return []
        try:
            targets = FirmwareFlasher.classify_firmware(files)
        except Exception as exc:
            self.log.error("固件分类失败: %s", exc)
            return []
        self._firmware_files = [str(t.path) for t in targets]
        return [{"path": str(t.path), "offset": t.offset} for t in targets]

    def _bundled_firmware_files(self) -> list:
        from core.flasher import FirmwareFlasher
        try:
            files = FirmwareFlasher.find_bundle_files(
                Path(self.bundledFirmwareDir()))
        except Exception:
            files = []
        if not files:
            try:
                p = FirmwareFlasher.find_bundled_firmware()
                files = [p] if p else []
            except Exception:
                files = []
        return files

    @Slot("QVariant", result=bool)
    def flashFirmware(self, paths) -> bool:
        """完整刷写：bootloader + 分区表 + 应用（不再整片擦除）。"""
        return self._flash(paths, mode="full")

    @Slot("QVariant", result=bool)
    def flashFirmwareAppOnly(self, paths) -> bool:
        """仅更新应用区（保留 NVS，配对表和映射都还在）。"""
        return self._flash(paths, mode="app")

    @Slot(str)
    def setFlashPort(self, port: str) -> None:
        """选烧录通道（设备页的下拉）。"""
        if port == self._flash_port:
            return
        self._flash_port = port
        self.flashStateChanged.emit()

    @Property(str, notify=flashStateChanged)
    def flashPort(self) -> str:
        """当前选的烧录通道（空串 = 没选 / 没检测到）。"""
        return self._flash_port

    @Property(str, notify=flashStateChanged)
    def flashPortHint(self) -> str:
        """没检测到烧录通道时给用户的提示。"""
        if self.flashPorts:
            return "烧录通道：%s" % self._flash_port
        return ("没检测到烧录通道 —— 请插上开发板的 USB 线"
                "（板载 CH343 串口）；板子跑着固件时，"
                "需要让它复位进下载模式才能刷")

    def _flash(self, paths, mode: str = "full") -> bool:
        """开始烧录（异步）。"""
        from core.flasher import FirmwareFlasher, FlashTarget

        if self._flash_busy:
            return False

        # ★ 三种形态都要吃 ★
        #   pickFirmwareFiles() 给界面的是 [{path, offset}] 字典；
        #   QML FileDialog 给的是 file:// URL；直接调用给字符串。
        #   以前一律 str(p) —— 字典会变成 "{'path': ...}"，报「文件不存在」。
        raw = [as_local_path(p) for p in (paths or [])]
        files = [Path(x) for x in raw if x]
        if not files:
            files = self._bundled_firmware_files()
        if not files:
            self._flash_log.append("没有找到固件文件")
            self.flashStateChanged.emit()
            return False

        try:
            targets = FirmwareFlasher.classify_firmware(files)
        except Exception as exc:
            self._flash_log.append(f"固件分类失败：{exc}")
            self.flashStateChanged.emit()
            return False

        if mode == "app":
            # 只留应用区（0x10000），NVS 不动
            app_targets = [t for t in targets if t.offset == 0x10000]
            if not app_targets:
                self._flash_log.append("没找到应用固件（0x10000）")
                self.flashStateChanged.emit()
                return False
            targets = app_targets

        # ★ 先用用户在设备页选的烧录通道 ★
        port = self._flash_port
        if not port:
            # 没选就自己找：优先 CH343（板载 UART，最稳）
            try:
                for p in FirmwareFlasher.list_ports():
                    if p.get("vid") == 0x1A86:
                        port = p.get("device", "")
                        break
                if not port:
                    for p in FirmwareFlasher.list_ports():
                        if p.get("vid") == 0x303A:
                            port = p.get("device", "")
                            break
            except Exception:
                pass
        if not port:
            self._flash_log.append(
                "没检测到烧录通道 —— 请插上开发板的 USB 线（板载 CH343 串口）；"
                "板子跑着固件时，需要让它复位进下载模式才能刷")
            self.flashStateChanged.emit()
            return False

        self._flash_busy = True
        self._flash_progress = 0.0
        header = ["完整刷写：bootloader + 分区表 + 应用" if mode == "full"
                  else "仅更新应用区（保留配对与按键映射）"]
        self._flash_log = [f"开始烧录 {port}"] + header + [
            f"  0x{t.offset:X}  {t.path.name}" for t in targets]
        self.flashStateChanged.emit()

        def on_progress(line: str) -> None:
            self._flash_log.append(line)
            # 同时写进 app.log —— 界面只看最后几十行，出问题要能翻文件
            self.log.info("[flash] %s", line.strip())
            self.flashStateChanged.emit()

        flasher = FirmwareFlasher(port)

        def run() -> None:
            # ★ 刷写线程必须兜住异常 ★
            #   以前这里没有 try：任何一步抛异常线程就静默死掉，
            #   界面永远停在「烧写中 0%」，用户以为还在刷。
            try:
                # 烧录要独占 CH343，协议通道先让开
                try:
                    self.disconnectDevice()
                except Exception:
                    pass

                flasher.reset_into_download_mode()

                # ★ 不再整片擦除 ★
                #   以前 full 模式先 erase 再写；写入一失败板子就是空白
                #   （用户实测：擦完就没有下文，COM12 消失）。
                #   write-flash 本身会擦要写的扇区，足够了。
                self._flash_progress = 50.0
                self.flashStateChanged.emit()
                result = flasher.flash(targets, progress=on_progress)
            except Exception as exc:
                self.log.error("刷写异常", exc_info=True)
                self._flash_busy = False
                self._flash_log.append(f"ERR: 刷写异常：{exc}")
                self.flashStateChanged.emit()
                return

            self._flash_busy = False
            self._flash_progress = 100.0 if result.ok else self._flash_progress
            self._flash_log.append(("OK: " if result.ok else "ERR: ")
                                   + result.message)
            self.flashStateChanged.emit()

            if not result.ok:
                # 失败也要恢复自动重连 —— 板子被放回应用模式后会自动出现
                self._auto_connect = True
            if result.ok:
                # ★ 刷完自动重连，并把归档的映射推回去 ★
                #   全新刷写把 NVS（配对表 + 按键映射）都清了，
                #   板子起来是个空板子。遥控器重新配对后靠 identity
                #   （固定 MAC）认出是同一台，映射就自动恢复。
                self._flash_log.append(
                    "刷写完成，3 秒后自动重连并按 MAC 归档恢复按键映射")
                self.flashStateChanged.emit()
                self._auto_connect = True
                time.sleep(3.0)
                self._reconnect_after_flash()

        threading.Thread(target=run, name="flasher", daemon=True).start()
        return True

    def _reconnect_after_flash(self) -> None:
        """刷完固件后重新连协议通道。"""
        try:
            self.connectDefaultPort()
        except Exception as exc:
            self.log.warning("刷写后重连失败：%s", exc)

    # ============================================================ 设备页

    @Property(str, notify=deviceInfoChanged)
    def firmwareVersion(self) -> str:
        """板端固件版本（读 INFO 的 fw=）。没连上时是空串。"""
        return self._fw_version

    @Property(bool, notify=deviceInfoChanged)
    def devicePresent(self) -> bool:
        """开发板是否插着。"""
        return self._device_present

    @Property(str, notify=deviceInfoChanged)
    def devicePort(self) -> str:
        """开发板所在的串口，如 "COM12"。"""
        return self._device_port

    @Property("QVariantList", notify=portCandidatesChanged)
    def portCandidates(self) -> list:
        """所有可用的协议通道口（VID 0x303A）。多个时让用户选。"""
        return list(self._port_candidates)

    @Property("QVariantList", notify=mappingsChanged)
    def buttonList(self) -> list:
        """
        按键页的行（纯列表）。

        ★ 为什么是纯列表而不是 QAbstractListModel ★
          页面上要把这些行**按左右两列拆开**渲染（参考项目的布局），
          QAbstractListModel 的 Repeater 没法过滤，JS 数组可以直接过滤。
        """
        return list(self._button_items)

    @Property(str, notify=mappingsChanged)
    def selectedButtonId(self) -> str:
        """当前选中的物理键（QML 用）。"""
        return self._selected_button_id

    @Slot(str)
    def setSelectedButtonId(self, button_id: str) -> None:
        if button_id == self._selected_button_id:
            return
        self._selected_button_id = button_id
        self.mappingsChanged.emit()

    @Slot()
    def openImeSettings(self) -> None:
        """
        打开当前输入法的设置（改快捷键要在这里改）。

        ★ 参考项目的做法 ★
          微信输入法直接起它的 wetype_update.exe -showsetting；
          其它家没有统一入口，退回到 Windows 的输入法设置页。
        """
        provider = str(self._cfg.get("ime_provider", "none"))
        if provider == "wechat":
            for base in (r"C:\Program Files\Tencent\WeType",
                         r"C:\Program Files (x86)\Tencent\WeType",
                         os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                      "Programs", "WeType"),
                         os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                      "Tencent", "WeType")):
                if not base or not os.path.isdir(base):
                    continue
                candidates = sorted(Path(base).glob("*/wetype_update.exe"),
                                    reverse=True)
                if candidates:
                    launch_command([str(candidates[0]), "-showsetting"])
                    self._set_status("已打开微信输入法设置")
                    return
            self._set_status("没找到微信输入法设置程序，已打开系统输入法设置")
        else:
            self._set_status("已打开系统输入法设置（快捷键在对应输入法自己的设置里改）")
        open_uri("ms-settings:regionlanguage")

    @Slot(str, result=str)
    def localPath(self, url: str) -> str:
        """QML FileDialog 给的是 file:// URL，转成本地路径。"""
        try:
            return str(self._local_path(url))
        except Exception:
            return url

    @Slot(str, result=str)
    def baseName(self, url: str) -> str:
        """从路径取文件名（不含扩展名），给自定义启动项当默认名字。"""
        try:
            name = self._local_path(url).stem
            return name or ""
        except Exception:
            return ""

    # ------------------------------------------------ 自定义启动项（数量不限）

    def _custom_apps(self) -> list[dict]:
        raw = self._cfg.get("custom_apps", [])
        out: list[dict] = []
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("label", "")).strip()
                path = str(entry.get("path", "")).strip()
                if label and path:
                    out.append({"label": label, "path": path})
        return out

    def _custom_app_labels(self) -> tuple[str, ...]:
        return tuple(e["label"] for e in self._custom_apps())

    @Slot(result="QVariantList")
    def customApps(self) -> list:
        """用户自己加的启动项（管理对话框用）。"""
        return list(self._custom_apps())

    @Slot(str, str, result=bool)
    def addCustomApp(self, label: str, path: str) -> bool:
        label = (label or "").strip()
        path = (path or "").strip().strip('"')
        if not label or not path:
            return False
        if label_to_kind(label) is not None:
            return False          # 跟内置动作重名会串味
        apps = self._custom_apps()
        if any(a["label"] == label for a in apps):
            return False
        apps.append({"label": label, "path": path})
        self._cfg["custom_apps"] = apps
        cfgmod.save_config(self._cfg)
        self.settingsChanged.emit()
        return True

    @Slot(str, result=bool)
    def removeCustomApp(self, label: str) -> bool:
        apps = self._custom_apps()
        kept = [a for a in apps if a["label"] != label]
        if len(kept) == len(apps):
            return False
        self._cfg["custom_apps"] = kept
        cfgmod.save_config(self._cfg)
        self.settingsChanged.emit()
        return True

    @Slot(str, result=str)
    def actionOptionGroupTitle(self, option: str) -> str:
        """某个预设动作属于哪一组（下拉里用来插组标题）。"""
        for title, options in action_option_groups(self._custom_app_labels()):
            if option in options:
                return title
        return ""

    @Slot(str, result="QVariantList")
    def primaryActionOptionsFor(self, button_id: str) -> list:
        """
        单击的预设清单。

        ★ 照抄参考项目 qt_settings_app.py:6310-6371 的分档 ★
          - 普通键：全部预设（含「禁用」，不含「未设置」）
          - 语音键：多一项「按住说话」放在最前
        """
        options: list[str] = []
        for _title, group in action_option_groups(self._custom_app_labels()):
            options.extend(group)
        # 「按住说话」已从下拉移除：语音键统一在「语音」页配置
        return options

    @Slot(str, result="QVariantList")
    def secondaryActionOptionsFor(self, button_id: str) -> list:
        """
        双击的预设清单。

        比单击多一项「未设置」（双击可以不配）、
        少一项「禁用」（不配就是未设置，不用「禁用」）。
        """
        options: list[str] = ["未设置"]
        for _title, group in action_option_groups(self._custom_app_labels()):
            for opt in group:
                if opt != "禁用":
                    options.append(opt)
        return options

    @Slot(str, result="QVariantList")
    def longPressActionOptionsFor(self, button_id: str) -> list:
        """
        长按的预设清单。

        = 双击的清单 + 4 个「鼠标 Xn 长按」
        （照抄参考项目 settings_ui.LONG_PRESS_ACTION_LABELS）。
        """
        options = self.secondaryActionOptionsFor(button_id)
        options.extend(["鼠标左键长按", "鼠标右键长按",
                         "鼠标 X1 长按", "鼠标 X2 长按"])
        return options

    @Property("QVariantList", notify=settingsChanged)
    def actionLabels(self) -> list:
        """预设动作的扁平清单（动作编辑器的下拉用）。"""
        out: list[str] = []
        for _title, options in action_option_groups(self._custom_app_labels()):
            for opt in options:
                out.append(opt)
        return out

    # ------------------------------------------------ 型号默认模板

    def _model_key(self) -> str:
        name = self._current_remote_name()
        return name.strip() or "unknown"

    @Slot(result=bool)
    def saveModelDefault(self) -> bool:
        """
        把当前映射保存成**这个型号**的默认。

        以后「恢复型号默认」和重新配对都套用它 ——
        用户不用每配一次就重新改一遍。
        """
        if self._current < 0 or not isinstance(self._bindings, dict):
            self._set_status("还没选遥控器")
            return False
        defaults = self._cfg.setdefault("model_defaults", {})
        defaults[self._model_key()] = {"bindings": self._bindings}
        cfgmod.save_config(self._cfg)
        self._set_status(f"已保存「{self._model_key()}」的默认映射"
                         "（恢复默认 / 重新配对都会套用）")
        return True

    def _model_default_bindings(self) -> Optional[dict]:
        defaults = self._cfg.get("model_defaults", {})
        if not isinstance(defaults, dict):
            return None
        entry = defaults.get(self._model_key())
        if isinstance(entry, dict) and isinstance(entry.get("bindings"), dict):
            return entry["bindings"]
        return None

    def _apply_model_defaults(self) -> bool:
        """本机映射为空、但这个型号存过默认 → 直接套用并下发。"""
        if self._current < 0:
            return False
        if self._remote_bindings(self._current):
            return False
        template = self._model_default_bindings()
        if not template:
            return False
        self._bindings = template
        self._save_bindings()
        self._load_mappings()
        self._set_status("已套用本机保存的型号默认映射")
        self.log.info("新配对：套用型号默认 %r", self._model_key())
        return True

    @Slot(result=bool)
    def restoreMappingDefaults(self) -> bool:
        """把当前遥控器的单击映射恢复为内置默认，并立即写回开发板。

        ★ 只覆盖「已知键位的单击项」 ★
          备注、双击/长按、以及本机独有（默认表里没有）的键位都保留 ——
          以前这里是整份替换，用户写的备注和 PC 独有动作全被抹掉。
        ★ 按型号取默认 ★
          现在只有谷歌遥控器一档；以后加了别的型号，
          这里会自动按遥控器名挑对应的那一档。
        """
        if self._current < 0:
            self._set_status("还没选遥控器")
            return False
        # ★ 存过型号默认就用它 ★
        template = self._model_default_bindings()
        if template:
            self._bindings = template
            self._save_bindings()
            self._load_mappings()
            self._set_status("已恢复本机保存的型号默认映射")
            return True
        actions = remote_layout.DEFAULT_ACTIONS
        long_actions = remote_layout.DEFAULT_LONG_ACTIONS
        prof = remote_layout.profile_for_name(self._current_remote_name())
        if prof is not None:
            actions = prof["actions"]          # type: ignore[assignment]
            long_actions = prof.get("long_actions") or long_actions
        bindings: dict = {}
        if isinstance(self._bindings, dict):
            for k, v in self._bindings.items():
                bindings[k] = dict(v) if isinstance(v, dict) else v
        for button_id, label in actions.items():
            key_id = remote_layout.button_key_id(button_id)
            if not key_id:
                continue          # 语音键没有 HID usage
            kind = label_to_kind(label)
            action = ButtonAction(kind if kind is not None else ActionKind.DISABLED)
            bindings = set_button_action(bindings, key_id,
                                         ButtonTrigger.SINGLE_CLICK, action)
        # ★ 长按也恢复（返回键长按 = 退格）★
        for button_id, label in (long_actions or {}).items():
            key_id = remote_layout.button_key_id(button_id)
            if not key_id:
                continue
            kind = label_to_kind(label)
            action = ButtonAction(kind if kind is not None else ActionKind.DISABLED)
            bindings = set_button_action(bindings, key_id,
                                         ButtonTrigger.LONG_PRESS, action)
        self._bindings = bindings
        self._save_bindings()          # 内部会推给板子
        self._load_mappings()
        self._set_status("已恢复型号默认并写回开发板（保留备注与双击/长按）")
        return True

    @Property("QVariantList", notify=mappingsChanged)
    def hotspots(self) -> list:
        """
        实物图上 15 个按键热点的归一化坐标（0..1）。

        坐标是照抄参考项目的，跟随包那张固定档案图 —— 图和坐标必须配套。
        """
        return [{"button_id": h.button_id, "x": h.x, "y": h.y,
                 "width": h.width, "height": h.height,
                 "is_voice": h.is_voice}
                for h in remote_layout.CHROMECAST_HOTSPOTS]

    @Property(str, notify=closeBehaviorChanged)
    def closeBehavior(self) -> str:
        """hide = 关闭窗口时隐藏到任务栏；quit = 完全退出。"""
        return self._close_behavior

    @Property("QVariantList", notify=logsChanged)
    def logLines(self) -> list:
        """诊断日志面板的内容（板端日志 + 协议行）。"""
        return list(self._log_lines)

    @Property(bool, notify=logsChanged)
    def logLive(self) -> bool:
        """是否正在接收板端实时日志。"""
        return self._log_live

    @Property(bool, notify=voiceKeyChanged)
    def imeHotkeyCustom(self) -> bool:
        """用户有没有在软件里改过语音快捷键。

        没改过 → 连上板子时只读不写，板子上生效的值原样保留。
        """
        return bool(self._cfg.get("ime_hotkey_custom", False))

    @Property(str, notify=voiceKeyChanged)
    def voiceKeyText(self) -> str:
        """板端当前生效的语音快捷键，可读形式。"""
        return self._voice_key_label()

    @Property(int, notify=voiceKeyChanged)
    def voiceKeyMods(self) -> int:
        return self._voice_key_mods

    @Property("QVariantList", notify=voiceKeyChanged)
    def voiceKeyKeys(self) -> list:
        return list(self._voice_key_keys)

    # ------------------------------------------------------------ 设备信息

    @Slot()
    def refreshDeviceInfo(self) -> None:
        """轮询串口 + 读板端固件版本。"""
        self._poll_ports()
        self._ask("INFO", timeout=2.5,
                  on_ok=lambda r: self._on_info(r))

    def _poll_ports(self) -> None:
        """
        开发板插没插 —— 看 ESP32-S3 原生 USB（VID 0x303A）在不在。

        ★ 这里只判断「口在不在」★
          口存在**不等于**协议通道通了 —— 通了还要 connectDevice()。
          v0.14.x 就是把这两件事混为一谈，结果页面显示「已插入」
          但一条命令都没发出去（根因 1）。
        """
        ports: list[str] = []
        signature = ""
        try:
            import serial.tools.list_ports as lp
            names: list[str] = []
            for p in lp.comports():
                names.append(f"{p.device}:{p.vid:04X}")
                if p.vid == 0x303A:
                    ports.append(p.device)
            signature = "|".join(sorted(names))
        except Exception as exc:
            self.log.debug("枚举串口失败: %s", exc)

        present = bool(ports)
        port = self._device_port if self._device_port in ports else (
            ports[0] if ports else "")
        changed = present != self._device_present or port != self._device_port
        self._device_present = present
        self._device_port = port
        if list(self._port_candidates) != ports:
            self._port_candidates = ports
            self.portCandidatesChanged.emit()
        # 串口清单也会变（插了别的设备），下拉框要跟着刷新
        if signature != self._port_signature:
            self._port_signature = signature
            self._port_cache = None      # 缓存作废，下次读重新枚举
            self.portCandidatesChanged.emit()

        if changed:
            self.deviceInfoChanged.emit()
            if not present:
                self._fw_version = ""
                self.deviceInfoChanged.emit()

        # ---- 热插拔：板子拔了要断，插上要自动连 ----
        if self._connected and self._device_port not in ports:
            self.log.info("开发板被拔出，断开串口")
            self.disconnectDevice()
            self._auto_connect = True      # 拔了不算用户主动断开，插回来要自动连
            self._set_status("开发板已拔出")
            return
        # 自动重连要退避：端口被别的程序占着时，
        # 每 2 秒重试一次会把日志刷爆（v0.15.0 实测刷了几十条 ERROR）。
        if not self._connected and len(ports) == 1 and self._auto_connect:
            now = time.monotonic()
            if self._last_connect_try and now - self._last_connect_try < self._reconnect_backoff:
                return
            self._last_connect_try = now
            self.log.info("发现开发板 %s，自动连接", ports[0])
            if self.connectDevice(ports[0]):
                self._reconnect_backoff = 0.0
            else:
                self._reconnect_backoff = min(
                    30.0, max(2.0, self._reconnect_backoff * 2 or 2.0))

    @Slot(result=str)
    def connectDefaultPort(self) -> str:
        """
        启动时自动连开发板。

        返回值：
            ""        —— 没有开发板，不连
            "<端口>"  —— 已经连上了
            "__ask__" —— 插了多个开发板，**不猜**，让 UI 问用户
        """
        if not self._auto_connect:
            # 自动连接被禁用（测试用）：连都不连
            return ""
        # ★ 已经连上了就别再连一次 ★
        #   __init__ 里的 _poll_ports() 已经自动连过，
        #   再连一次就会多出一条读线程抢同一个串口（应答被吃掉）。
        if self._connected:
            return self._device_port
        ports = list(self._port_candidates)
        if not ports:
            try:
                self._poll_ports()
                ports = list(self._port_candidates)
            except Exception:
                ports = []
        if not ports:
            self._set_status("未找到开发板")
            return ""
        if len(ports) > 1:
            self._set_status(f"发现 {len(ports)} 个开发板，请选择")
            return "__ask__"
        self.connectDevice(ports[0])
        return ports[0]

    @Slot(str, result=bool)
    def connectPort(self, device: str) -> bool:
        """用户手动指定连哪个口。"""
        return self.connectDevice(device)

    def _on_info(self, result) -> None:
        version = result.get_str("fw")
        if version != self._fw_version:
            self._fw_version = version
            self.deviceInfoChanged.emit()

    def _ask(self, verb: str, *args, timeout: float = 4.0, on_ok=None) -> None:
        """
        异步发一条命令。

        ★ 不能在 UI 线程里直接 request() ★
          request() 会阻塞等应答，开发板没插上时要等满 timeout，
          界面就卡住了。这里丢到后台线程，回调再通过信号回主线程。
        """
        proto = self._proto
        if proto is None:
            return

        def run() -> None:
            try:
                result = proto.request(verb, *args, timeout=timeout)
            except Exception as exc:
                self.log.debug("%s 失败: %s", verb, exc)
                return
            if result.ok and on_ok is not None:
                # 回调也要回 GUI 线程：它会改属性、发信号
                self._gui_call(on_ok, result)

        threading.Thread(target=run, name=f"ask-{verb.lower()}",
                         daemon=True).start()

    # ------------------------------------------------------------ 关闭行为

    @Slot(str)
    def setCloseBehavior(self, behavior: str) -> None:
        behavior = "quit" if str(behavior) == "quit" else "hide"
        if behavior == self._close_behavior:
            return
        self._close_behavior = behavior
        self._cfg["close_behavior"] = behavior
        cfgmod.save_config(self._cfg)
        self.closeBehaviorChanged.emit()

    # ------------------------------------------------------------ 诊断日志

    def _append_log(self, line: str) -> None:
        """
        往日志面板追加一行。

        ★ 合并 emit，不逐行 emit ★
          以前每条协议行都 emit 一次 logsChanged，
          QML 的 ListView 用的是 JS 数组模型 —— 一变就**整体重建**，
          800 行 × 每秒几十行 = 每秒上万次 delegate 重建，全在 GUI 线程，
          界面直接被打死（v0.15.0 实测：用一会儿就卡死/崩溃）。
        """
        self._log_lines.append(line)
        # 面板只留最近 800 行，文件里另有完整记录
        if len(self._log_lines) > 800:
            del self._log_lines[:200]
        self._log_dirty = True

    @Slot()
    def startLogStream(self) -> None:
        """打开板端实时日志。"""
        if self._log_live:
            return
        self._log_live = True
        self._cfg["log_live"] = True
        cfgmod.save_config(self._cfg)
        self.logsChanged.emit()
        self._ask("LOGLIVE", 1, timeout=3.0)

    @Slot()
    def stopLogStream(self) -> None:
        if not self._log_live:
            return
        self._log_live = False
        self._cfg["log_live"] = False
        cfgmod.save_config(self._cfg)
        self.logsChanged.emit()
        self._ask("LOGLIVE", 0, timeout=3.0)

    @Slot()
    def readLogHistory(self) -> None:
        """把板端滚动缓冲里的历史日志拉回来。"""
        if self._proto is None:
            return
        self._collecting_logs = True
        self._ask("LOGREAD", timeout=6.0,
                  on_ok=lambda r: self._collecting_logs.__setattr__(False) or None)

    def _check_capture_deadline(self) -> None:
        """录制超时了自动停 —— 钩子不能一直挂着。"""
        if not self._capture_deadline:
            return
        if time.monotonic() < self._capture_deadline:
            return
        self._capture_deadline = 0.0
        self.log.info("快捷键录制超时，自动停止")
        self.stopHotkeyCapture()
        self._set_status("录制超时，已自动停止")

    @Slot()
    def flushLogs(self) -> None:
        """把攒下来的日志一次性通知界面（合并 emit）。"""
        if not self._log_dirty:
            return
        self._log_dirty = False
        self.logsChanged.emit()

    @Slot()
    def clearLogs(self) -> None:
        self._log_lines.clear()
        self.logsChanged.emit()

    # ------------------------------------------------------------ 语音键

    @Slot()
    def refreshVoiceKey(self) -> None:
        """读板端当前生效的语音快捷键。"""
        self._ask("VOICEKEY", timeout=3.0, on_ok=self._on_voice_key)

    def _on_voice_key(self, result) -> None:
        mods = result.get_int("mods")
        count = result.get_int("count")
        keys: list[int] = []
        raw = result.kv.get("keys", "")
        if raw:
            for byte_str in raw.split():
                try:
                    keys.append(int(byte_str, 16))
                except ValueError:
                    pass
        keys = keys[:count] if count else []
        if mods != self._voice_key_mods or keys != self._voice_key_keys:
            self._voice_key_mods = mods
            self._voice_key_keys = keys
            self.voiceKeyChanged.emit()

    def _voice_key_label(self) -> str:
        # ★ 显示键名，不显示 0x 键值 ★
        #   0x14 这种只有开发者看得懂；用户要看到 lctrl+lalt+q。
        parts = list(tokens_from_firmware(self._voice_key_mods,
                                          tuple(self._voice_key_keys)))
        return "+".join(parts) if parts else "（未设置）"

    # ------------------------------------------------------------ 内部

    #: 「信号强」的门槛（dBm）。比这弱的默认不显示。
    NEARBY_RSSI = -78

    def _is_nearby(self, item: dict) -> bool:
        """默认视图：必须有名字，而且信号不弱。"""
        return bool(item.get("name")) and item.get("rssi", -100) >= self.NEARBY_RSSI

    def _count_nearby(self) -> int:
        return sum(1 for it in self._scan_items if self._is_nearby(it))

    def _rebuild_scan_model(self) -> None:
        """
        重建显示列表。

        规则（学 Windows 的「添加设备」）：
          · 默认：只显示「有名字 + 信号强」的，按信号从强到弱排
          · 「显示所有设备」：全部显示，仍然按信号排序
        """
        items = list(self._scan_items)
        if not self._show_all:
            items = [it for it in items if self._is_nearby(it)]
        items.sort(key=lambda it: -it.get("rssi", -100))
        self.scanModel.set_items(items)
        self.scanChanged.emit()

    @Slot(bool)
    def setShowAllDevices(self, on: bool) -> None:
        self._show_all = bool(on)
        self._rebuild_scan_model()

    @Slot()
    def clearScanList(self) -> None:
        self._scan_items = []
        self._rebuild_scan_model()

    def _set_status(self, text: str) -> None:
        self._status = text
        self.statusChanged.emit()

    def _apply_raw_line(self, line: str) -> None:
        """
        协议原文（进出都记）—— 排障时这就是完整的现场记录。

        ★ 只进文件，不进面板 ★
          面板是给人看的：什么时间、哪台遥控器、按了什么键、
          变成了什么键。协议原文塞在面板里只会把真正要看的内容淹掉。
          工程细节要查就打开日志文件（设备页有「打开日志位置」「导出日志」）。
        """
        self.device_log.info("%s", line)

    def _apply_board_log(self, level: str, msg: str) -> None:
        """
        板端日志（固件的 LG 行）。

        固件的 ESP_LOG 平时只走 UART0，PC 用 CDC 连上时一行都看不到；
        v0.14.0 起板端自己留了滚动缓冲，PC 可以拉历史 + 收实时。
        """
        text = human_log.board_log(level, "", msg)
        self.logLine.emit(text)
        self._append_log(text)

    def _on_sent_line(self, line: str) -> None:
        """发出的命令（排障时和收到的对照着看）。"""
        self.device_log.info(">> %s", line)

    def _on_event(self, name: str, kv: dict) -> None:
        """
        协议读线程的入口。

        ★ 这里只 emit 信号 ★
          真正的处理在 GUI 线程（_apply_event）。
          读线程直接 beginResetModel() 会破坏 Qt 的模型状态 → 随机崩。
        """
        self.protocolEventReceived.emit(name, kv)

    def _on_data(self, kind: str, kv: dict) -> None:
        """协议读线程的入口（数据行）—— 同样只 emit，处理回 GUI 线程。"""
        self.protocolDataReceived.emit(kind, kv)

    def _on_board_log(self, level: str, msg: str) -> None:
        """板端日志 —— 读线程入口。"""
        self.protocolLogReceived.emit(level, msg)

    def _on_raw_line(self, line: str) -> None:
        """协议原文 —— 读线程入口。"""
        self.protocolRawReceived.emit(line)

    def _gui_call(self, fn, *args) -> None:
        """把一个回调丢到 GUI 线程执行。"""
        self.guiCallRequested.emit(fn, args)

    @Slot(object, object)
    def _run_gui_call(self, fn, args) -> None:
        try:
            fn(*args)
        except Exception as exc:
            self.log.debug("GUI 回调失败: %s", exc)

    def _apply_event(self, name: str, kv: dict) -> None:
        if name == "STATE":
            # 固件每 5 秒报一次状态，顺便问一句配对窗口
            if self._proto is not None:
                self._ask("GETPAIRWINDOW", timeout=3.0,
                          on_ok=self._on_pair_window_state)
        if name == "SCANSTART":
            # 扫描**会话**开始 —— 清一次（会话内各轮不清）
            self._scan_items = []
            self._rebuild_scan_model()
        elif name == "SCANSTOP":
            pass
        if name == "SCAN":
            # 固件跨扫描轮保留候选，同一个地址会反复上报 -> 按地址去重
            addr = kv.get("addr", "")
            item = {"addr": addr, "name": kv.get("name", ""),
                    "rssi": int(kv.get("rssi", 0) or 0),
                    "score": int(kv.get("score", 0) or 0),
                    "addr_type": int(kv.get("type", 0) or 0)}
            for old in self._scan_items:
                if old["addr"] == addr:
                    old.update(item)
                    break
            else:
                self._scan_items.append(item)
            self._rebuild_scan_model()
        elif name == "CONN":
            self._set_status(f"已连接 {kv.get('name') or kv.get('addr')}")
            self.refreshRemotes()
        elif name == "RPEND":
            # ★ 以板端为准整体重建 ★
            #   以前只裁尾巴：本地 [A,B,C]、板子 [A,C]（删了中间那台）
            #   裁成 [A,B] → B 顶替了 C，名字和映射全错位。
            reported = set(self._board_addrs)
            if reported:
                remotes = [r for r in self._cfg.get("remotes", [])
                           if isinstance(r, dict) and r.get("addr") in reported]
                self._cfg["remotes"] = remotes
                cfgmod.save_config(self._cfg)
            self._board_addrs.clear()
            self._rebuild_remote_model()
            # 列表齐了才有意义：选一台当前遥控器，把板子上的映射读回来
            self._ensure_current_remote()
            self.loadMappingFromBoard()
        elif name == "CONNFAIL":
            # 配对窗口里连不上是常态（遥控器睡着就不广播），
            # 不能说"失败"就完事 —— 要说清还在等什么、为什么没成。
            why = unescape(kv.get("why", "")) or kv.get("status", "?")
            if self._pairing:
                self._set_status(f"还没连上 {self._pairing_target}，"
                                 f"继续等它广播（{why}）")
            else:
                self._set_status(f"连接失败（{why}）")
        elif name == "DISC":
            self._set_status("遥控器已断开")
            self.refreshRemotes()
        elif name == "LEARN":
            usage = int(kv.get("usage", "0") or 0)
            src = int(kv.get("src", "0") or 0)
            self._detect_key(src, usage)

        elif name == "KEY":
            self._on_key(kv)
            self._log_key_event(kv)
        elif name == "MAPOUT":
            self._log_mapout(kv)
        elif name == "MAPMISS":
            self._log_mapmiss(kv)
        elif name == "VOICE":
            self._log_voice(kv)
        elif name == "PAIR":
            ok = kv.get("ok", "0") == "1"
            self._pairing = False
            self.pairingChanged.emit()
            if ok:
                self._set_status("配对成功")
                # ★ 配对成功就退出扫描 ★
                #   用户定的模型：只有「还没有任何配对信息」时才扫描；
                #   配上了就该安静下来等遥控器自己醒。
                #   持续扫描既费电又和重连抢控制器。
                if self._proto is not None:
                    self._proto.send("SCAN", 0)
                self._scanning = False
                self.scanChanged.emit()
            else:
                self._set_status("配对失败：" + self._explain_pair_fail(
                    unescape(kv.get("why", "")) or ""))
            self.refreshRemotes()
        elif name == "PAIR" and kv.get("ok", "0") == "1":
            self._set_status(f"配对成功：{kv.get('name') or kv.get('addr')}")
            self.refreshRemotes()
        elif name == "ATVVREADY":
            self._set_status("ATVV 语音通道就绪")

    @staticmethod
    def _explain_pair_fail(why: str) -> str:
        """
        把固件报的原因翻译成人能照着做的下一步。

        固件给的原因有两种：
          never-seen-in-NNs-window  —— 整个窗口里**一次都没扫到**这个地址
          BLE_HS=13(ETIMEOUT)|HCI=…  —— 扫到了、发了连接请求、对方没应答
        """
        if not why:
            return "原因未知，请把日志导出给我"
        if why.startswith("never-seen"):
            return ("这段时间一次都没扫到它。"
                    "遥控器空闲时完全不广播（省电），"
                    "请长按 Home 3 秒进配对模式（LED 闪烁）后重新点配对。")
        return (f"{why}。扫到了但连不上：多半是它刚醒就睡了，"
                "保持配对模式（LED 闪烁）再点一次。")

    def _apply_data(self, kind: str, kv: dict) -> None:
        if kind == "RM":
            self._on_remote_row(kv)
        elif kind == "MAP":
            self._on_map_row(kv)
        elif kind == "AU":
            self._on_voice_frame("AU", kv)
        elif kind == "CTL":
            self._on_voice_frame("CTL", kv)
        elif kind == "LOG":
            # 板端滚动日志缓冲里的一行（LOGREAD 回传）
            level = kv.get("level", "I")
            tag = kv.get("tag", "")
            msg = unescape(kv.get("msg", ""))
            text = human_log.board_log(level, tag, msg)
            self.logLine.emit(text)
            self._append_log(text)

    def _rebuild_remote_model(self) -> None:
        """按当前配置重建遥控器列表。"""
        remotes = [r for r in self._cfg.get("remotes", [])
                   if isinstance(r, dict) and r.get("addr")]
        self.remoteModel.set_items(remotes)
        self.remotesChanged.emit()

    def _on_remote_row(self, kv: dict) -> None:
        """
        收到固件上报的一台遥控器。

        ★ 按**地址**合并，不按下标 ★
          以前按下标 merge，还会往配置里塞 {} 占位。
          结果 UI 的行号跟配置的下标对不上 —— 删中间那台会删错，
          而且后面那台会顶替它（名字和映射全错位）。
          地址才是唯一真身。
        """
        addr = str(kv.get("addr", "") or "")
        if not addr:
            return
        self._board_addrs.add(addr)
        # ★ identity（固定 MAC）★
        #   广播地址会轮换，identity 不会 —— 认人靠它。
        #   用户有多台谷歌遥控器，全靠广播地址认的话，
        #   地址一变就变成「多了一台新遥控器」，旧的还在列表里。
        ident = str(kv.get("id", "") or "")
        remotes_all = [r for r in self._cfg.get("remotes", [])
                       if isinstance(r, dict)]
        old: dict = {}
        pos = -1
        for i, r in enumerate(remotes_all):
            if ident and str(r.get("identity", "") or "") == ident:
                old = r
                pos = i
                break
            if str(r.get("addr", "") or "") == addr and pos < 0:
                old = r
                pos = i
        entry = {**old,
                 "addr": addr,
                 "identity": ident or str(old.get("identity", "") or ""),
                 "pl_idx": int(kv.get("idx", -1) or -1),
                 "name": str(kv.get("name", "") or ""),
                 "addr_type": int(kv.get("type", 0) or 0),
                 "connected": kv.get("conn", "0") == "1",
                 "voice": kv.get("voice", ""),
                 # 本机这份的条数
                 # ★ 要数**真正能下发到板子的映射条数** ★
                 #   以前数的是外层 dict 的键数（bindings/secondary_bindings/…），
                 #   显示出来永远是个位数，跟板子上真实条数对不上，
                 #   「同步」按钮也就永远亮着。
                 #   firmware_bindings() 的单位和固件 br_mapping_count() 一致。
                 "binds": len(firmware_bindings(old.get("bindings") or {})),
                 # ★ 板子上**真实**的条数 ★
                 #   固件 v0.17.0 起 DT RM 的 binds 是 br_mapping_count()
                 #   的真实值。两个数摆在一起，用户一眼能看出
                 #   「板子上还没同步」还是「已经同步好了」。
                 "binds_board": int(kv.get("binds", 0) or 0)}
        custom = str(old.get("custom_name", "") or "")
        if custom:
            # 用户自己起的名字优先，广播名（一堆 HID_Device）只当兜底
            entry["name"] = custom

        # ★ 原地更新，顺序不变 ★
        #   板子每次上报的顺序是它自己的，跟本地不一定一致；
        #   换序会让用户刚改的名字跑到别的行去。
        if pos >= 0:
            remotes_all[pos] = entry
        else:
            remotes_all.append(entry)
        remotes = remotes_all
        self._cfg["remotes"] = remotes
        cfgmod.save_config(self._cfg)
        self._rebuild_remote_model()

    def _on_map_row(self, kv: dict) -> None:
        """
        板子上的一条映射（DT MAP key=.. trig=.. out=.. mods=.. keys=..）。

        key 是十进制的 (source<<12)|usage；
        mods 是修饰键位掩码；keys 是普通键的 usage 字节（十六进制）。
        """
        if not self._collecting_map:
            return
        try:
            key = int(kv.get("key", "0") or 0)
            trig = int(kv.get("trig", "0") or 0)
            out = int(kv.get("out", "1") or 1)
            mods = int(kv.get("mods", "0") or 0)
        except (TypeError, ValueError):
            return
        raw_keys = str(kv.get("keys", "") or "").split()
        usages: list[int] = []
        for item in raw_keys:
            try:
                usages.append(int(item, 16))
            except ValueError:
                pass

        src = key >> 12
        usage = key & 0x0FFF
        key_id = f"{src}:{usage}"

        trigger = {0: ButtonTrigger.SINGLE_CLICK,
                   1: ButtonTrigger.DOUBLE_CLICK,
                   2: ButtonTrigger.LONG_PRESS}.get(trig)
        if trigger is None:
            return

        if out == 2:
            # ★ 消费者页（音量+ / 音量− / 静音）★
            #   tokens_from_firmware 只认键盘页的 usage，
            #   消费者页的 0xE9/0xEA/0xE2 会被算成空 → 整条映射丢掉。
            #   所以按 out 分流：消费者页按 usage 反查 token。
            token = CONSUMER_TOKEN_BY_USAGE.get(usages[0] if usages else -1)
            tokens = (token,) if token else ()
        else:
            tokens = tuple(tokens_from_firmware(mods, usages))
        if not tokens:
            return
        # ★ 必须按 **(键, 触发)** 存，不能只按键 ★
        #   同一个键可以同时有单击和长按（返回键：单击退格、长按连删）。
        #   只按 key_id 存 → 长按把单击覆盖掉 →
        #   快照比板子少一条 → 合并时又把这条例外当成「板子上没有」删掉 →
        #   按键页显示「未设置」，还老弹「不一致」。
        self._board_mapping[(key_id, TRIGGER_INDEX[trigger])] = tuple(tokens)

    # ------------------------------------------------- 人话日志
    def _remote_label(self, addr: str = "") -> str:
        """遥控器的人话名字（名字 + 地址）。"""
        name = ""
        for i in range(self.remoteModel.rowCount()):
            r = self.remoteModel.get(i)
            if not addr or str(r.get("addr", "") or "") == addr:
                name = str(r.get("name", "") or "")
                if not addr:
                    addr = str(r.get("addr", "") or "")
                break
        return human_log.who(name, addr)

    def _key_name_of(self, src: str, usage: str, idx: str = "") -> str:
        """usage → 键名（主页键…）。"""
        try:
            src_i = int(src)
            usage_i = int(usage)
        except (TypeError, ValueError):
            return "未知键"
        # 板子自检（TEST）造的假按键：idx=-1，说人话
        try:
            if idx != "" and int(idx) < 0:
                return "测试按键"
        except (TypeError, ValueError):
            pass
        key_id = f"{src_i}:{usage_i}"
        # usage_to_button_id() 是 {usage: 键 id}，别把两个搞反了
        for usage_i2, button_id in remote_layout.usage_to_button_id().items():
            if key_id == f"{remote_layout.BR_SRC_CONSUMER}:{usage_i2}":
                return remote_layout.display_name(button_id)
        if src_i == 1:          # 键盘页才用键盘键名
            name = token_for_keyboard_usage(usage_i)
            if name:
                return name
        return f"键 {usage}"

    def _log_key_event(self, kv: dict) -> None:
        addr = str(kv.get("addr", "") or "")
        name = self._key_name_of(kv.get("src", "0"), kv.get("usage", "0"),
                                 kv.get("idx", ""))
        self._append_log(human_log.key_event(kv, self._remote_label(addr), name))

    def _log_mapout(self, kv: dict) -> None:
        addr = str(kv.get("addr", "") or "")
        try:
            key = int(kv.get("key", "0") or 0)
        except (TypeError, ValueError):
            return
        src = key >> 12
        usage = key & 0x0FFF
        name = self._key_name_of(str(src), str(usage))
        out_text = ""
        try:
            mods = int(kv.get("mods", "0") or 0)
            usages = [int(x, 16) for x in
                      str(kv.get("keys", "") or "").split() if x]
        except (TypeError, ValueError):
            mods, usages = 0, []
        if usages:
            out_text = " + ".join(tokens_from_firmware(mods, usages))
        if not out_text and int(kv.get("out", "1") or 1) == 2 and usages:
            out_text = "消费者键 0x%02X" % usages[0]
        self._append_log(human_log.mapout(kv, self._remote_label(addr),
                                          name, out_text))

    def _log_mapmiss(self, kv: dict) -> None:
        try:
            key = int(kv.get("key", "0") or 0)
        except (TypeError, ValueError):
            return
        name = self._key_name_of(str(key >> 12), str(key & 0x0FFF))
        self._append_log(human_log.mapmiss(kv, self._remote_label(), name))

    @Property(str, notify=remotesChanged)
    def remoteOnlineText(self) -> str:
        """每台遥控器的在线情况。

        ★ 用户有多台谷歌遥控器 ★
          遥控器只在醒着的时候广播，没连上的那台按语音键当然没反应，
          这一行让人一眼看出该按哪台。
        """
        parts: list[str] = []
        for i in range(self.remoteModel.rowCount()):
            r = self.remoteModel.get(i)
            name = str(r.get("custom_name", "") or r.get("name", "")
                       or r.get("addr", ""))
            parts.append(("● " if r.get("connected") else "○ ") + name)
        return "　".join(parts)

    @Property(int, notify=remotesChanged)
    def remoteOnlineCount(self) -> int:
        """有几台遥控器在线。"""
        count = 0
        for i in range(self.remoteModel.rowCount()):
            if self.remoteModel.get(i).get("connected"):
                count += 1
        return count

    @Property(str, notify=voiceKeyChanged)
    def lastVoiceEvent(self) -> str:
        """最近一次语音触发（什么时候、哪台遥控器、板子按出了什么键）。

        ★ 用户要多台谷歌遥控器 ★
          按了没反应的时候，这一行能立刻分清是「板子没收到」
          还是「输入法没接住」。
        """
        return self._last_voice_text

    def _log_voice(self, kv: dict) -> None:
        addr = str(kv.get("addr", "") or "")
        try:
            mods = int(kv.get("mods", "0") or 0)
            usages = [int(x, 16) for x in
                      str(kv.get("keys", "") or "").split() if x]
        except (TypeError, ValueError):
            mods, usages = 0, []
        # 用**这次实际发出去**的键，而不是板子上的配置值 —— 日志要如实
        pressed = list(tokens_from_firmware(mods, tuple(usages)))
        hotkey = "+".join(pressed) if pressed else "（无按键）"
        remote = self._remote_label(addr)
        self._last_voice_text = "%s · %s · %s" % (
            human_log.stamp().strip("[]"), remote, hotkey)
        self.voiceKeyChanged.emit()
        self._append_log(human_log.voice_event(kv, remote, hotkey))

    def _detect_key(self, src: int, usage: int) -> None:
        """检测到真实按键：翻成中文键名 + 选中对应的那一行 + 第一次就停。

        ★ 为什么要这样 ★
          以前只显示 usage=0x3A —— 用户看不懂，
          也不知道自己按下的是哪个键、对应哪张卡片，
          而且学完不会自动停（按钮一直显示「停止检测」）。
          参考项目就是「第一次按下即停 + 显示中文键名 + 选中对应行」。
        """
        self._learn_target = f"0x{usage:02X}"
        button_id = ""
        if src == remote_layout.BR_SRC_CONSUMER:
            button_id = remote_layout.usage_to_button_id().get(usage, "")
        if button_id:
            key_name = remote_layout.display_name(button_id)
            self.setSelectedButtonId(button_id)
        else:
            key_name = f"未知键（usage=0x{usage:02X}）"
        # ★ 保持检测状态 ★
        #   参考项目是「按一次就退出」，但连续测试每个键时很烦 ——
        #   现在一直保持，界面跟着跳；再点一次按钮才退出。
        self._learn_result = key_name
        self.learnChanged.emit()
        self._set_status(f"已检测：{key_name}；继续按其它键，再点按钮退出")

    # ------------------------------------------------- PC 专属动作（手势判定）

    # 与固件 br_gesture.c 的阈值一致
    PC_LONG_MS = 550
    PC_DOUBLE_MS = 300

    def _pc_bindings_of(self, addr: str) -> dict:
        """按地址（或 identity）找那台遥控器的绑定。"""
        for entry in self._cfg.get("remotes", []):
            if not isinstance(entry, dict):
                continue
            if addr and addr in (str(entry.get("addr", "")),
                                 str(entry.get("identity", ""))):
                b = entry.get("bindings", {})
                return b if isinstance(b, dict) else {}
        return {}

    def _pc_key_event(self, kv: dict) -> None:
        """
        EV KEY：给「PC 专属动作」做手势判定。

        ★ 为什么在 PC 侧再判一次手势 ★
          打开应用 / 鼠标动作没法翻译成 HID，板子根本不下发；
          但板子对每个物理键都发 EV KEY，所以 PC 能自己判单击/双击/长按，
          这样按键页三列下拉对这类动作才有意义。
        """
        try:
            src = int(kv.get("src", "0") or 0)
            usage = int(kv.get("usage", "0") or 0)
        except (TypeError, ValueError):
            return
        if src != remote_layout.BR_SRC_CONSUMER:
            return
        button_id = remote_layout.usage_to_button_id().get(usage, "")
        if not button_id:
            return
        # ★ 绑定字典的键是 key_id（"2:120"），不是 "netflix" ★
        #   按按钮 id 去查永远查不到 —— 这就是「下拉里选了、按了没反应」的根因。
        key_id = remote_layout.button_key_id(button_id)
        if not key_id:
            return
        addr = str(kv.get("addr", "") or "")
        bindings = self._pc_bindings_of(addr) if addr else self._bindings
        if not isinstance(bindings, dict) or not bindings:
            return
        actions = {t: button_action_for(bindings, key_id, t)
                   for t in ButtonTrigger}
        if not any(is_pc_only(a) for a in actions.values()):
            return

        now = time.monotonic()
        key = (addr, key_id)
        state = self._pc_gesture.get(key)
        is_down = str(kv.get("state", "down") or "down") == "down"

        if is_down:
            if state is not None and state.get("await_double"):
                state.update(pressed_at=now, long_fired=False,
                             second=True, await_double=False)
            else:
                self._pc_gesture[key] = {
                    "pressed_at": now, "long_fired": False, "second": False,
                    "await_double": False, "actions": actions}
            return

        if state is None:
            return
        if state.get("long_fired"):
            run_pc_action(actions[ButtonTrigger.LONG_PRESS], "release")
            self._pc_gesture.pop(key, None)
        elif state.get("second"):
            run_pc_action(actions[ButtonTrigger.DOUBLE_CLICK])
            self._pc_gesture.pop(key, None)
        else:
            state["await_double"] = True
            state["released_at"] = now

    def _pc_gesture_tick(self) -> None:
        if not self._pc_gesture:
            return
        now = time.monotonic()
        for key, state in list(self._pc_gesture.items()):
            if state.get("await_double"):
                if now - state["released_at"] >= self.PC_DOUBLE_MS / 1000.0:
                    run_pc_action(state["actions"][ButtonTrigger.SINGLE_CLICK])
                    self._pc_gesture.pop(key, None)
                continue
            if state.get("long_fired"):
                continue
            if now - state["pressed_at"] < self.PC_LONG_MS / 1000.0:
                continue
            long_action = state["actions"][ButtonTrigger.LONG_PRESS]
            if is_pc_only(long_action):
                state["long_fired"] = True
                run_pc_action(long_action, "press")
            else:
                # 这个键没有「长按」专属动作：按住等同单击（固件的快路径）
                single = state["actions"][ButtonTrigger.SINGLE_CLICK]
                if is_pc_only(single):
                    run_pc_action(single)
                self._pc_gesture.pop(key, None)

    def _on_key(self, kv: dict) -> None:
        """收到按键事件。"""
        if self._learn_active:
            try:
                src = int(kv.get("src", "0") or 0)
                usage = int(kv.get("usage", "0") or 0)
            except (TypeError, ValueError):
                return
            if str(kv.get("state", "down") or "down") == "down":
                self._detect_key(src, usage)
            return
        self._pc_key_event(kv)
        self._learn_target = kv.get("usage", "")
        self.learnChanged.emit()

    def _on_voice_frame(self, kind: str, kv: dict) -> None:
        self._voice_state = f"{kind} {kv.get('n', 0)} 字节"
        self.voiceStateChanged.emit()
