"""
v1.1.0 契约闸门：手机端（临时遥控器）新增的跨语言契约必须两边一致。

★ 为什么单独写一个文件、且不依赖 PySide6 ★
    1.1.0 的教训：契约漂移（固件改了、PC 端没改）只有在「两边常量不一致」时
    才会暴露，而这类测试往往因为依赖太重跑不起来。
    这个文件只用标准库读源码 —— 任何环境都能跑，装不装 PySide6 都拦不住它。

检查项：
    1. BR_SRC_PHONE / BR_OUT_MOUSE 两边取值一致（键位空间隔离的前提）
    2. 手机伪地址两边一致（映射表按地址存，两边不一样就存不进去）
    3. 固件上报的 EV 名，PC 端确实有处理分支（不然事件到了没人接）
    4. PC 端要发的命令，固件确实认（不然点了没反应）

运行：
    python dev/client/tests/test_v110_contract.py
    python -m pytest dev/client/tests/test_v110_contract.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FW_MAIN = ROOT / "dev" / "firmware" / "bridge" / "main"
CLIENT_SRC = ROOT / "dev" / "client" / "src"

FW_TYPES = FW_MAIN / "br_types.h"
FW_PHONE = FW_MAIN / "br_phone.c"
FW_APP = FW_MAIN / "app_main.c"
FW_WEB = FW_MAIN / "br_web.c"
PC_LAYOUT = CLIENT_SRC / "core" / "remote_layout.py"
PC_CONTROLLER = CLIENT_SRC / "core" / "controller.py"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def c_defines(text: str) -> dict[str, str]:
    """抓 C 头文件里的 #define NAME 值"""
    out: dict[str, str] = {}
    for m in re.finditer(r"#define\s+(\w+)\s+([^\r\n/]+)", text):
        out[m.group(1)] = m.group(2).strip()
    return out


def py_assigns(text: str, name: str):
    """抓 Python 模块级常量 NAME = ..."""
    m = re.search(rf"^{re.escape(name)}\s*=\s*(.+)$", text, re.M)
    if not m:
        return None
    return m.group(1).strip()


def _load_layout():
    """直接按路径载入 remote_layout.py —— 它只依赖标准库，载入不牵扯 PySide6。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("remote_layout", PC_LAYOUT)
    mod = importlib.util.module_from_spec(spec)
    # ★ dataclass 在装饰时会回头查 sys.modules[cls.__module__] ★
    #   不先登记就会 AttributeError —— 载入失败不是代码的问题，是姿势的问题。
    sys.modules["remote_layout"] = mod
    spec.loader.exec_module(mod)
    return mod


def _adpcm_tables_match(js_text: str, c_text: str) -> bool:
    """页面的 ADPCM 步长表/索引表必须和板子解码器的一致。

    两边不是同一份代码（一边 JS 一边 C），表错一位音频就全是噪声。
    """
    m = re.search(r"ADPCM_STEP\s*=\s*\[(.*?)\];", js_text, re.S)
    n = re.search(r"ADPCM_INDEX\s*=\s*\[(.*?)\];", js_text, re.S)
    c1 = re.search(r"s_step_table\[89\]\s*=\s*\{([^}]*)\}", c_text, re.S)
    c2 = re.search(r"s_index_table\[8\]\s*=\s*\{([^}]*)\}", c_text, re.S)
    if not (m and n and c1 and c2):
        return False
    js_step = [int(x) for x in re.findall(r"\d+", m.group(1))]
    js_index = [int(x) for x in re.findall(r"-?\d+", n.group(1))]
    c_step = [int(x) for x in re.findall(r"-?\d+", c1.group(1))]
    c_index = [int(x) for x in re.findall(r"-?\d+", c2.group(1))]
    return js_step == c_step and js_index == c_index


def main() -> int:
    fw = c_defines(FW_TYPES.read_text(encoding="utf-8"))
    layout = PC_LAYOUT.read_text(encoding="utf-8")
    ctrl = PC_CONTROLLER.read_text(encoding="utf-8")
    app = FW_APP.read_text(encoding="utf-8")
    https = FW_WEB.read_text(encoding="utf-8")

    print("=" * 66)
    print("v1.1.0 契约闸门")
    print("=" * 66)

    # 1. source 空间
    fw_src = fw.get("BR_SRC_PHONE", "")
    pc_src = py_assigns(layout, "BR_SRC_PHONE") or ""
    check("固件 BR_SRC_PHONE 存在", bool(fw_src), fw_src)
    check("PC 端 BR_SRC_PHONE 存在", bool(pc_src), pc_src)
    check("BR_SRC_PHONE 两边一致",
          fw_src.replace("u", "") == pc_src.replace("u", ""),
          f"固件={fw_src} PC={pc_src}")
    # 与既有 source 不得撞车（撞了就无法隔离映射）
    for other in ("BR_SRC_KEYBOARD", "BR_SRC_CONSUMER", "BR_SRC_DATA",
                  "BR_SRC_VENDOR", "BR_SRC_ATVV"):
        v = fw.get(other, "")
        if v and v.replace("u", "") == fw_src.replace("u", ""):
            check(f"BR_SRC_PHONE 不与 {other} 冲突", False, f"两边都是 {v}")

    # 2. 输出类型
    fw_out = fw.get("BR_OUT_MOUSE", "")
    check("固件 BR_OUT_MOUSE 存在", bool(fw_out), fw_out)

    # 3. 手机伪地址：固件是字节数组，PC 是字符串
    m = re.search(r"#define\s+BR_PHONE_ADDR\s*\{([^}]*)\}", fw_types_text := FW_TYPES.read_text(encoding="utf-8"))
    fw_addr = ""
    if m:
        fw_addr = ":".join(f"{int(x.strip(), 0):02X}"
                            for x in m.group(1).split(",") if x.strip())
    pc_addr = py_assigns(layout, "BR_PHONE_ADDR") or ""
    pc_addr = pc_addr.strip('"').upper()
    check("手机伪地址两边一致", bool(fw_addr) and fw_addr == pc_addr,
          f"固件={fw_addr} PC={pc_addr}")

    # 4. 固件上报的 EV，PC 端有处理分支
    for ev in ("TEXT", "WIFI", "SYSACT"):
        check(f"PC 端处理 EV {ev}",
              re.search(rf'name\s*==\s*"{ev}"', ctrl) is not None)

    # 5. PC 端要发的命令，固件认
    for cmd in ("WIFI", "WIFIINFO", "WIFISCAN"):
        check(f"固件认命令 {cmd}",
              re.search(rf'"{cmd}"', app) is not None)

    # 6. 板端路由齐全（手机页面 + 事件收发）
    for route in ('"/io"', '"/io/poll"', '"/*"'):
        check(f"板端注册路由 {route}", route in https)

    # 7. 通配路由必须显式指定匹配函数（httpd 默认全等比较，不写就是全 404）
    check("httpd 显式启用通配匹配",
          "httpd_uri_match_wildcard" in https)

    # 8. 版本号两边同步
    fw_ver = re.search(r'#define\s+FW_VERSION\s+"([^"]+)"', app)
    ver_py = py_assigns((CLIENT_SRC / "core" / "version.py").read_text(encoding="utf-8"),
                         "VERSION") or ""
    ver_py = ver_py.strip('"')
    check("固件与 PC 端版本号一致",
          bool(fw_ver) and fw_ver.group(1) == ver_py,
          f"固件={fw_ver.group(1) if fw_ver else '?'} PC={ver_py}")

    # 9. 手机要作为一台遥控器上报（否则 PC 端列表里根本没有它）
    fw_ble = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("固件把手机报进遥控器列表",
          'BR_PHONE_ADDR' in fw_ble and '"kind", "phone"' in fw_ble)
    check("PC 端认 kind=phone",
          'kv.get("kind"' in ctrl)

    # 10. PC 端不能把手机的键（src=6）当噪声丢掉
    check("PC 端 PC 动作认 src=6",
          "BR_SRC_PHONE" in ctrl and
          ("src == remote_layout.BR_SRC_PHONE" in ctrl or
           "src != remote_layout.BR_SRC_PHONE" in ctrl))

    # 11. 两套布局的键位空间必须隔离
    layout = _load_layout()
    phone_ids = {layout.button_key_id_for(layout.LAYOUT_PHONE, b)
                 for b in layout.PHONE_BUTTON_ORDER}
    cc_ids = {layout.button_key_id_for(layout.LAYOUT_CHROMECAST, b)
              for b in layout.BUTTON_ORDER}
    check("手机与遥控器 source 不同",
          layout.BR_SRC_PHONE != layout.BR_SRC_CONSUMER,
          f"{layout.BR_SRC_PHONE} vs {layout.BR_SRC_CONSUMER}")
    check("手机键位不与遥控器键位重合",
          not (phone_ids & cc_ids),
          f"重合={sorted(phone_ids & cc_ids)}")
    check("手机每个键都有 key_id",
          all(layout.button_key_id_for(layout.LAYOUT_PHONE, b)
              for b in layout.PHONE_BUTTON_ORDER))

    # 12. 多 WiFi 记忆：命令、数据行、界面三处都要对齐
    #     换地方不用重新配网 —— 少任何一环，用户就得把板子插回电脑再配一次
    fw_wifi = (FW_MAIN / "br_wifi.c").read_text(encoding="utf-8")
    for cmd in ("WIFILIST", "WIFIDEL"):
        check(f"固件认命令 {cmd}", re.search(rf'"{cmd}"', app) is not None)
        check(f"PC 端会发 {cmd}", f'"{cmd}"' in ctrl)
    check("固件上报 DT WIFINET", '"WIFINET"' in fw_wifi)
    check("PC 端处理 DT WIFINET", 'kind == "WIFINET"' in ctrl)
    check("固件记住多个 WiFi",
          "WIFI_MAX_NETS" in fw_wifi and "load_nets" in fw_wifi)
    check("固件连不上会换下一个记住的网络", "net_begin" in fw_wifi)
    check("PC 端有记住的 WiFi 列表属性", "def wifiNets" in ctrl)
    qml = (CLIENT_SRC / "ui" / "qml" / "DevicePage.qml").read_text(encoding="utf-8")
    phone_qml = (CLIENT_SRC / "ui" / "qml" / "PhonePage.qml").read_text(encoding="utf-8")
    check("PC 界面有配网入口",
          "wifiConnectButton" in phone_qml and "Bridge.setWifi" in phone_qml)
    check("PC 界面能删记住的 WiFi", "Bridge.deleteWifiNet" in phone_qml)
    # 这两个是「配网功能看不见」的真凶：拿到 IP 的事件没人接、状态变化不上报
    check("IP_EVENT 交给 on_wifi_event",
          re.search(r"IP_EVENT,\s*ESP_EVENT_ANY_ID,\s*on_wifi_event",
                    fw_wifi) is not None)
    check("WiFi 状态变化主动上报 PC",
          re.search(r"static void report_state\(void\)\s*\{\s*br_wifi_report\(\);",
                    fw_wifi) is not None)

    # 13. 明文 HTTP：局域网内不做证书那套，用户不该被要求装 CA
    check("板端 TLS 默认关闭",
          re.search(r"#define\s+BR_WEB_ENABLE_TLS\s+0", https) is not None)
    check("PC 端给手机的地址是 http", 'f"http://{ip}:5888"' in ctrl)
    check("界面不再出现 Movo 字样", "Movo" not in qml)
    web_html = (ROOT / "dev" / "firmware" / "bridge" / "web"
                / "touchpad.html").read_text(encoding="utf-8")
    check("陀螺仪走原生桥（不依赖安全上下文）", "__movoGyro" in web_html)

    # 14. shim 的 move 合并不能重入：flush 必须直走 _post。
    #     教训：flushMove 调 self.emit('move') 会被 emit 的 move 拦截
    #     再攒回 _queueMove —— 合并→flush→再合并死循环，
    #     触摸板/陀螺仪的 move 永远发不出去（光标完全不动）。
    shim = (ROOT / "dev" / "firmware" / "bridge" / "web" / "static" / "js"
            / "socket.io.min.js").read_text(encoding="utf-8")
    check("shim 有独立发送入口 _post", "Shim.prototype._post" in shim)
    check("shim 非 move 事件走 _post",
          re.search(r"return this\._post\(ev, data, ack", shim) is not None)
    m = re.search(r"function flushMove\(self\) \{([\s\S]*?)\n    \}", shim)
    flush_body = m.group(1) if m else ""
    check("flushMove 直走 _post", "self._post('move'" in flush_body)
    check("flushMove 不再回 emit（重入死循环）",
          "self.emit('move'" not in flush_body)

    # 15. 语音就绪握手：页面在 ready 前不发 voice_start，板子必须应答 voice_status。
    #     教训：板子以前只在收到 voice_start 后才回 voice_state ready:true，
    #     而页面（voice.js）没就绪就只弹「识别引擎还在加载」并再问一次
    #     voice_status，绝不主动发 voice_start → 「等就绪 → 才发 start →
    #     才回就绪」死锁，语音永远按不下去（用户实测 + COM6 零语音日志）。
    phone_c = (FW_MAIN / "br_phone.c").read_text(encoding="utf-8")
    voice_js = (ROOT / "dev" / "firmware" / "bridge" / "web" / "static" / "js"
                / "voice.js").read_text(encoding="utf-8")
    check("页面就绪前只问 voice_status（契约前提）",
          "if (!ready)" in voice_js and "socket.emit('voice_status')" in voice_js)
    m = re.search(r'strcmp\(name,\s*"voice_status"\)\s*==\s*0\)\s*\{([\s\S]*?)\n    \}',
                  phone_c)
    status_body = m.group(1) if m else ""
    check("板子有 voice_status 处理分支", bool(status_body), "缺 voice_status 分支")
    check("voice_status 应答 voice_state ready:true",
          'br_web_send_event("voice_state"' in status_body
          and '\\"ready\\":true' in status_body)

    # 16. /io/poll 必须始终返回 JSON 数组：shim 按 list.length 遍历。
    #     教训：evq_pop_all 以前只拼 {...},{...} —— 单条是裸对象（length
    #     undefined 被丢弃）、多条是非法 JSON（r.json 抛错被判断线），
    #     voice_state/partial/final 一条都到不了手机，语音永远不就绪。
    m = re.search(r"static int evq_pop_all\(char \*out, size_t cap\)\s*\{([\s\S]*?)\n\}",
                  https)
    pop_body = m.group(1) if m else ""
    check("evq_pop_all 存在", bool(pop_body))
    check("poll 输出以 [ 开头", "out[0] = '[';" in pop_body)
    check("poll 输出以 ] 收尾", "out[used] = ']';" in pop_body)
    check("shim 按数组遍历 poll 结果", "list.length" in shim)

    # 17. 语音 PCM 包（100ms@16k mono int16 = 3200 字节）不能走 JSON 的 1024 上限。
    #     教训：http_io 先按 sizeof(body)=1024 判 413，voice_audio 每包都被
    #     "too large" 丢掉 → 麦克风按得下去但电脑收不到声音。
    #     voice_audio 必须在 1024 闸门**之前**按 content_len 单独收满。
    m = re.search(r"static esp_err_t http_io\(httpd_req_t \*req\)\s*\{([\s\S]*?)"
                  r"if \(req->content_len >= \(int\)sizeof\(body\)\)", https)
    io_head = m.group(1) if m else ""
    check("http_io 存在 1024 JSON 闸门", bool(io_head))
    check("voice_audio 在 1024 闸门之前单独处理",
          "voice_audio" in io_head and "malloc" in io_head
          and "content_len" in io_head)
    # 17b. ★ 手机语音必须走 ADPCM（和遥控器同一条通道）★
    #      教训：16kHz 裸 PCM 是 32KB/s，而手机→板子这条 WiFi 实测
    #      只有 ~15KB/s（一次请求固定开销 ~90ms）—— 音频只能一半一半
    #      挤过去，电脑那边听到的是断断续续的碎片。
    #      ADPCM 压 4 倍（8KB/s）才有实时余量。
    check("页面 PCM 包走 ADPCM",
          "var V_ADPCM_CHUNK = 1600;" in voice_js
          and "voice_adpcm" in voice_js)
    check("板子有手机 ADPCM 解码入口",
          "br_voice_push_phone_adpcm" in (FW_MAIN / "br_voice.c").read_text(
              encoding="utf-8")
          and "br_phone_push_adpcm" in (FW_MAIN / "br_phone.c").read_text(
              encoding="utf-8")
          and "voice_adpcm" in https)
    check("ADPCM 步长表页面与板子一致",
          _adpcm_tables_match(voice_js, (FW_MAIN / "br_voice.c").read_text(
              encoding="utf-8")))
    # 17b-2. ★ 请求体不能超过板子的 TCP 窗口 ★
    #      教训：9600 字节的包超过 lwIP 的 TCP_WND(5760)，
    #      传输停在半路（板子只收到 4320 字节），
    #      httpd 被拖死而且**不会自己恢复** —— 板子的网页从此全挂。
    check("板子对 PCM 包长有上限",
          "BR_WEB_MAX_PCM_BODY" in https
          and re.search(r"#define\s+BR_WEB_MAX_PCM_BODY\s+4096", https)
          is not None)
    check("页面一包不超过 4096 字节",
          re.search(r"var V_ADPCM_CHUNK\s*=\s*(\d+);", voice_js) is not None
          and int(re.search(r"var V_ADPCM_CHUNK\s*=\s*(\d+);",
                            voice_js).group(1)) <= 4096)
    check("单个 PCM 请求有超时（不会把队列堵死）",
          "V_PCM_TIMEOUT" in voice_js and "timeout" in shim)

    # 17c. start() 必须先看连接、再看就绪。
    #      顺序反了的时候，连接睡着就弹「识别引擎还在加载」，
    #      voice_status 被丢掉，什么也不会发生。
    m = re.search(r"function start\(\) \{(.*?)\n        \}", voice_js, re.S)
    start_body = m.group(1) if m else ""
    check("start() 先判连接再判就绪",
          start_body.find("if (!socket.connected)") != -1
          and start_body.find("if (!socket.connected)")
          < start_body.find("if (!ready)"))

    # 18. PC 端给用户的手机地址必须是「IP:端口」，不能带 http://。
    #     教训：用户在路由器里看不到 ESP32，只能照着 PC 端显示的地址填；
    #     而手机 App 的 normalize() 只认 IP:端口 —— 粘 "http://1.2.3.4:5888"
    #     会被 split('/')[0] 截成 "http:" 然后判「地址格式不对」。
    #     所以复制按钮复制的必须是 phoneAddr（不带 scheme）。
    ctrl = PC_CONTROLLER.read_text(encoding="utf-8")
    qml = (CLIENT_SRC / "ui" / "qml" / "DevicePage.qml").read_text(encoding="utf-8")
    phone_qml = (CLIENT_SRC / "ui" / "qml" / "PhonePage.qml").read_text(encoding="utf-8")
    check("客户端提供 phoneAddr（IP:端口）",
          re.search(r"def phoneAddr\(self\)", ctrl) is not None
          and 'f"{ip}:5888"' in ctrl)
    check("界面用 phoneAddr 显示地址", "Bridge.phoneAddr.length > 0" in phone_qml)
    check("复制按钮复制的是 phoneAddr",
          "copyToClipboard(Bridge.phoneAddr)" in phone_qml)
    check("「手机地址」行在手机端区第一项",
          phone_qml.index('objectName: "phoneUrlRow"')
          < phone_qml.index('objectName: "wifiRow"'))

    # 18b. ★ 手机改走蓝牙后，独立「手机页」已取消（v1.2.9）★
    #      那页最初是为了 WiFi 配网；手机现在就是一台蓝牙设备，
    #      真实设备名/地址显示在设备页的「手机（…）」那一行。
    main_qml = (CLIENT_SRC / "ui" / "qml" / "main.qml").read_text(encoding="utf-8")
    check("手机页已从导航移除", "{ title: \"手机\"" not in main_qml)
    check("手机页已从 StackLayout 移除", "PhonePage { tokens: th }" not in main_qml)
    check("设备页有手机真实信息行", 'objectName: "phoneInfoRow"' in qml)
    check("设备页不再有手机分区", 'objectName: "phoneSection"' not in qml)
    check("蓝牙配对列表不显示手机",
          'visible: row.kind !== "phone"' in qml)
    check("遥控器计数不含手机",
          "RemoteListModel.bleConnectedCount" in qml
          and "RemoteListModel.bleCount" in qml)
    check("固件按心跳报手机在线（不写死已连接）",
          "br_phone_recently_active() ? 1 : 0" in fw_ble)
    check("客户端暴露手机在线状态",
          "def phoneOnline(self)" in ctrl
          and 'r.get("kind") == "phone"' in ctrl)

    # 19. 板子要在路由器 DHCP 列表里有个能认出来的名字。
    #     教训：用户照路由器设备列表找 ESP32，找不到（默认是芯片派生名/一串 MAC），
    #     只能一个个 IP 试 → 手机端填错地址 → 「连不上电脑」。
    wifi_c = (FW_MAIN / "br_wifi.c").read_text(encoding="utf-8")
    check("固件设置了主机名",
          "esp_netif_set_hostname" in wifi_c and "ESP32-Remote" in wifi_c)
    check("取 netif 用的是字符串 key（传枚举会 LoadProhibited 崩板）",
          'esp_netif_get_handle_from_ifkey("WIFI_STA_DEF")' in wifi_c
          and "esp_netif_get_handle_from_ifkey(WIFI_IF_STA)" not in wifi_c)

    # 20. 三处文案必须一致：板子页面、APK 补丁脚本、APK 里的 i18n。
    #     服务跑在开发板上，文案还说「电脑」→ 用户会把 PC 的 IP 填进去。
    i18n_js = (ROOT / "dev" / "firmware" / "bridge" / "web" / "static" / "js"
               / "i18n.js").read_text(encoding="utf-8")
    apk_patch = (ROOT / "dev" / "client" / "build" / "patch_movo_apk.py").read_text(
        encoding="utf-8")
    check("板端文案说「开发板」而不是「电脑」",
          "'set.addr': '开发板地址'" in i18n_js
          and "连不上开发板" in i18n_js)
    check("APK 补丁脚本用同一套文案",
          "'set.addr': '开发板地址'" in apk_patch
          and "连不上开发板" in apk_patch)

    # 21. 手机键盘页最上面那排功能键（音量/静音/终端/关机）走 sys_action，
    #     PC 端必须真的实现 —— 以前只认 open_url，那排键按了毫无反应。
    kb = (ROOT / "dev" / "firmware" / "bridge" / "web" / "keyboard.html").read_text(
        encoding="utf-8")
    check("键盘页功能键走 sys_action",
          'data-sys="vol_up"' in kb and 'data-sys="terminal"' in kb
          and 'data-sys="shutdown"' in kb)
    check("PC 端实现了音量/静音", "send_media_key" in ctrl)
    check("PC 端实现了终端与关机",
          '"terminal"' in ctrl and '"shutdown"' in ctrl)

    # 22. 陀螺仪必须有位移时唤醒/续命连接。
    #     教训：link.js 闲置 61 秒就断连，而晃手机不产生触摸事件 →
    #     连接睡着 → move 全被丢掉 → 飞鼠完全没反应。
    pad_html = (ROOT / "dev" / "firmware" / "bridge" / "web"
                / "touchpad.html").read_text(encoding="utf-8")
    check("陀螺仪有位移时唤醒连接",
          "link.isSleeping()) link.wake();" in pad_html
          and "link.touch();" in pad_html)
    check("陀螺仪俯仰用绕 X 轴的角速度（不是偏航）",
          "feedGyro(-gy * DEG, -gx * DEG)" in pad_html
          and "gz * DEG" not in pad_html)

    # 23. 板子不能把陀螺仪的小数位移直接取整扔掉。
    check("板端 move 保留小数余量",
          "rem_x" in (FW_MAIN / "br_phone.c").read_text(encoding="utf-8"))

    # 24. /controller 页面不存在，那个按钮必须删掉（死控件）。
    check("页面里不再有指向 /controller 的入口",
          "location.replace('/controller')" not in pad_html)

    # 25. SendInput 的 INPUT 联合体必须放全三种成员。
    #     教训：只放 KEYBDINPUT 时 64 位下 sizeof(INPUT)=32，系统要求 40，
    #     SendInput 直接返回 0（错误 87）—— 媒体键发不出去，
    #     而且**文字注入**（语音识别结果上屏）也是这么静默失败的。
    win_py = (CLIENT_SRC / "platforms" / "windows.py").read_text(encoding="utf-8")
    check("INPUT 联合体含 MOUSE/HARDWARE 成员",
          '"mi", _MOUSEINPUT' in win_py and '"hi", _HARDWAREINPUT' in win_py)
    check("文字注入与媒体键共用同一套结构",
          "def send_vk(" in win_py and "send_vk(vk, extended, up)" in win_py)
    try:
        import ctypes  # noqa: PLC0415
        import sys as _sys  # noqa: PLC0415
        _sys.path.insert(0, str(CLIENT_SRC))
        from platforms.windows import _INPUT as _INP  # noqa: PLC0415
        check("sizeof(INPUT) == 40",
              ctypes.sizeof(_INP) == 40, f"实际 {ctypes.sizeof(_INP)}")
    except Exception as exc:  # pragma: no cover - 环境缺依赖时跳过
        print(f"  SKIP 运行时检查 INPUT 大小（{exc}）")

    # 26. ★ 协议任务必须真的创建出来 ★
    #     教训：板子只有 512KB 内部 RAM，BLE+WiFi+USB 一起用非常紧。
    #     一次改版多吃掉 1.8KB，cdc 任务（8KB 栈）就**静默地创建失败**了 ——
    #     板子照常活着、网页照常能开，但 PC 发什么命令都不回
    #     （表现：读不到固件版本、遥控器列表空）。
    #     任务创建失败以前没人看返回值，现场一点线索都没有。
    fw_uart = (FW_MAIN / "br_uart.c").read_text(encoding="utf-8")
    fw_wifi = (FW_MAIN / "br_wifi.c").read_text(encoding="utf-8")
    fw_voice = (FW_MAIN / "br_voice.c").read_text(encoding="utf-8")
    check("固件检查 cdc 任务创建结果",
          "cdc task create failed" in app
          and "esp_get_free_heap_size" in app)
    check("固件检查 uart 任务创建结果",
          "uart task create failed" in fw_uart)
    # 预卷缓冲是 .bss（吃内部 RAM），不能再涨回 16KB
    m = re.search(r"#define\s+PHONE_PREROLL_BYTES\s+(\d+)", fw_voice)
    check("手机预卷缓冲不超过 4KB（内部 RAM 紧张）",
          m is not None and int(m.group(1)) <= 4096,
          f"PHONE_PREROLL_BYTES={m.group(1) if m else '?'}")

    # 27. ★ 页面脚本加载失败要能自愈 ★
    #     教训：手机在 5G、板子在 2.4G，链路一差浏览器会丢掉某一个脚本的请求 ——
    #     页面看着加载完了，语音按钮却是死的（MovoVoice 根本没定义）。
    #     现在缺关键对象就自动重载一次（最多两次）。
    check("触控板页有脚本自愈",
          "movo_script_reload" in pad_html
          and "window.MovoVoice && window.AirLink" in pad_html)
    kb_html = (ROOT / "dev" / "firmware" / "bridge" / "web"
               / "keyboard.html").read_text(encoding="utf-8")
    check("键盘页有脚本自愈", "movo_script_reload" in kb_html)

    # 28. ★ 陀螺仪横向方向不能反 ★
    #     教训：feedGyro 里那个负号是按浏览器 devicemotion.gamma 定的，
    #     而原生桥给的是 Android 陀螺仪的**角速度**，
    #     同一次「往右倾」在两边符号相反 → 手机往右倾鼠标却往左走。
    #     证据：注入 gy=+0.6 板子记 dx=-20（往左）；
    #     修后注入 gy=+0.6 记 dx=+61（往右）、gy=-0.6 记 dx=-61（往左）、
    #     gx=+0.6 记 dy=+60（前后方向也正了）。
    check("原生陀螺仪两轴符号都翻正",
          "feedGyro(-gy * DEG, -gx * DEG)" in pad_html,
          "原生桥必须把 gy、gx 都再取一次负")
    check("浏览器 devicemotion 路径的符号没被动过",
          "feedGyro(rot.gamma, rot.alpha)" in pad_html)

    # 29. ★ 拔插板子后必须能自动重连 ★
    #     教训：热插拔的断开路径调了 disconnectDevice()，
    #     而那个函数会把**轮询定时器也停掉** ——
    #     定时器一停，就再没人去发现板子插回来了，
    #     客户端永远停在「未连接」（实测：拔一次板子就得重启客户端）。
    check("热插拔断开不停止轮询定时器",
          "def _disconnect(self, user_initiated: bool = True)" in ctrl
          and "if user_initiated:" in ctrl)
    check("自动重连不要求只有一个串口",
          re.search(r"if not self\._connected and ports and self\._auto_connect",
                    ctrl) is not None)

# 30. ★ WiFi 连不上要有原因、要能扫 ★
    #     教训：用户把 "CMCC-7K5j" 打成 "CMCC-7k5j"（大小写敏感！），
    #     板子一直 reason=201 找不到，界面只写「未连接」，毫无线索。
    _scan_cfg = re.findall(r"wifi_scan_config_t sc\s*=\s*\{[^}]*\}", fw_wifi,
                           re.S)
    check("板子扫描 WiFi 不用自定义时长（蓝牙开着时 IDF 会直接报错）",
          bool(_scan_cfg) and all("scan_time" not in c for c in _scan_cfg),
          f"{len(_scan_cfg)} 处扫描配置")
    check("板子能把 SSID 大小写自愈",
          "fix_ssid_case" in fw_wifi and "strcasecmp" in fw_wifi)
    check("板子上报 WiFi 失败原因",
          "wifi_fail_hint" in fw_wifi and 'br_kv_add(&kv, "why"' in fw_wifi)
    check("客户端有 WiFi 扫描与选择",
          "def scanWifi(self)" in ctrl and "def useWifiAp(self" in ctrl
          and "wifiApRow" in phone_qml)

# 31. ★ 候选遥控器要能点选配对 ★
    #     教训：配对窗口是板子自动抓「第一个像遥控器的广播」，
    #     周围有多台谷歌遥控器时抓谁全看运气 ——
    #     用户看着就是「只能配上一台，换一台死活配不上」。
    check("客户端有扫描候选与点选配对",
          "def pairCandidate(self" in ctrl
          and "scanCandidateRow" in qml and "Bridge.pairCandidate" in qml)

# 32. ★ CDC 命令任务：栈必须够大，而且要在 WiFi 之前建 ★
    #     教训：客户端连上就发 REMOTES（列遥控器），cdc 任务处理它时
    #     栈溢出 → panic 重启 → USB 设备消失 → PC 端永远收不到数据，
    #     表现为「手机打字到不了电脑」「用着用着就不响应」。
    #     闸门盯死两件事：栈 ≥ 16KB、创建位置在 br_wifi_init 之前。
    fw_app_txt = FW_APP.read_text(encoding="utf-8")
    m = re.search(r'xTaskCreate\(cdc_task, "cdc", (\d+)', fw_app_txt)
    check("cdc 任务栈 ≥ 16KB", bool(m) and int(m.group(1)) >= 16384,
          m.group(1) if m else "未找到 xTaskCreate(cdc_task, ...)")
    i_cdc = fw_app_txt.find('xTaskCreate(cdc_task, "cdc"')
    i_wifi = fw_app_txt.find("br_wifi_init()")
    check("cdc 任务在 WiFi 之前创建（内部 RAM 够用）",
          i_cdc > 0 and i_wifi > 0 and i_cdc < i_wifi,
          f"cdc@{i_cdc} wifi@{i_wifi}")
    check("cdc 任务行缓冲是 static（不压栈）",
          "static char line[BR_LINE_MAX]" in fw_app_txt)

    # 33. ★ 剪贴板句柄必须按 64 位声明 ★
    #     教训：GetClipboardData 默认 restype=c_int，64 位句柄被截断，
    #     GlobalLock 拿着残指针读 → access violation → 手机文本注入必崩。
    pc_windows = (CLIENT_SRC / "platforms" / "windows.py").read_text(encoding="utf-8")
    check("GetClipboardData 按 64 位句柄声明",
          "user32.GetClipboardData.restype = ctypes.c_void_p" in pc_windows)
    check("GlobalLock 按 64 位句柄声明",
          "kernel32.GlobalLock.restype = ctypes.c_void_p" in pc_windows)
    check("文本注入有 Unicode 兜底",
          "_type_unicode" in pc_windows and "KEYEVENTF_UNICODE" in pc_windows)

    # 34. ★ APK 侧（BLE 桥）源码与脚本必须在仓库里、且带关键修复 ★
    apk_java = ROOT / "dev" / "apk" / "javasrc" / "com" / "dsnaaa" / "movo" / "BleClient.java"
    apk_build = ROOT / "dev" / "apk" / "build_apk.py"
    check("APK 桥源码在仓库里", apk_java.is_file(), str(apk_java))
    if apk_java.is_file():
        j = apk_java.read_text(encoding="utf-8")
        check("@JavascriptInterface 存在（否则页面看不到 MovoBle）",
              "@android.webkit.JavascriptInterface" in j)
        check("超长 JSON 分帧（type=5）", "byte[]{5, 0," in j or "{5, 0," in j)
        check("断线看门狗", "watchdog" in j)
        check("连续写失败判定断开", "txFail" in j)
    check("APK 可重建脚本存在", apk_build.is_file(), str(apk_build))

    # 35. ★ 手机身份 + 不依赖 PC 软件的 ASCII 输入 ★
    fw_gatt = (FW_MAIN / "br_ble_gatt.c").read_text(encoding="utf-8")
    fw_phone = (FW_MAIN / "br_phone.c").read_text(encoding="utf-8")
    qml_main = (CLIENT_SRC / "ui" / "qml" / "main.qml").read_text(encoding="utf-8")
    qml_dev = (CLIENT_SRC / "ui" / "qml" / "DevicePage.qml").read_text(encoding="utf-8")
    check("固件读取手机设备名（0x2A00）",
          "0x2A00" in fw_gatt and "report_phone" in fw_gatt)
    check("固件上报 EV PHONE", 'br_proto_send_ev("PHONE"' in fw_gatt)
    check("客户端处理 EV PHONE",
          'name == "PHONE"' in ctrl and "phoneAddr" in ctrl)
    check("手机输入会闪灯反馈",
          "bridge_led_phone_activity" in fw_phone
          and "bridge_led_phone_activity" in (FW_MAIN / "br_led.c").read_text(encoding="utf-8"))
    check("文本统一回 PC 注入（不自己 HID 敲）",
          "ascii_char_key" not in fw_phone)
    check("手机页已移除", "PhonePage" not in qml_main)
    check("设备页显示手机真实信息", "phoneInfoRow" in qml_dev)

    # 36. ★ 手机底部栏重构 + 设置页闸门 + 第 6 个手机键（v1.3.0）★
    tp = (ROOT / "dev" / "firmware" / "bridge" / "web" / "touchpad.html").read_text(encoding="utf-8")
    apk_setup = (ROOT / "dev" / "firmware" / "bridge" / "apk" / "setup.html").read_text(encoding="utf-8")
    apk_java = (ROOT / "dev" / "apk" / "javasrc" / "com" / "dsnaaa" / "movo"
                / "BleClient.java").read_text(encoding="utf-8")
    layout = PC_LAYOUT.read_text(encoding="utf-8")
    check("底部：触控板/陀螺仪合并成一个切换键",
          'id="mode-toggle"' in tp and 'id="mode-pad"' not in tp and 'id="mode-gyro"' not in tp)
    check("底部：新增自定义快捷键按钮", 'id="shortcut-btn"' in tp)
    fw_types_txt = FW_TYPES.read_text(encoding="utf-8")
    fw_shortcut = re.search(r"#define BR_PHU_SHORTCUT\s+0x([0-9A-Fa-f]+)", fw_types_txt)
    pc_shortcut = re.search(r"PHONE_LEGACY_SHORTCUT_USAGE\s*=\s*0x([0-9A-Fa-f]+)", layout)
    check("两端 usage 一致（BR_PHU_SHORTCUT == 旧版常量）",
          bool(fw_shortcut) and bool(pc_shortcut)
          and int(fw_shortcut.group(1), 16) == int(pc_shortcut.group(1), 16),
          f"固件={fw_shortcut.group(0) if fw_shortcut else '缺'} "
          f"客户端={pc_shortcut.group(0) if pc_shortcut else '缺'}")
    check("设置页：连上后不自动跳走",
          "isSettings" in apk_setup
          and "if (isSettings)" in apk_setup
          and "B.call('autopause'" not in apk_setup)
    check("设置页：主动同步连接状态（新页面收不到状态变化）",
          "B.call('state'" in apk_setup)
    check("设置页：灵敏度等设置能保存（BLE 模式没有地址也能存）",
          "function applyPrefs" in apk_setup
          and "applyPrefs();" in apk_setup
          and "N.setPref('mouse_sense'" in apk_setup)
    i18n_js = (ROOT / "dev" / "firmware" / "bridge" / "web" / "static" / "js"
               / "i18n.js").read_text(encoding="utf-8")
    check("i18n：底部快捷键有中文键",
          "'pad.mode.shortcut'" in i18n_js and "'pad.mode.gyro'" in i18n_js)
    check("原生：autopause 闸门",
          'case "autopause"' in apk_java and "autoConnectBlocked" in apk_java)
    fw_phone_txt = FW_PHONE.read_text(encoding="utf-8")
    bp = (ROOT / "dev" / "client" / "src" / "ui" / "qml"
          / "ButtonsPage.qml").read_text(encoding="utf-8")
    ensure_body = PC_CONTROLLER.read_text(encoding="utf-8").split("def _ensure_current_remote")[1][:1200]
    check("首屏：选中变化后重建按键行", "_load_mappings()" in ensure_body)
    check("卡片名：自定义键用后端名字",
          "function cardDisplayName" in bp
          and bp.count("buttonName: root.cardDisplayName(modelData)") == 2)
    check("手机布局不显示「实物图缺失」",
          "!root.phoneMode && photoImage.status !== Image.Ready" in bp)
    check("触控板视图保持 flex 尺寸（#pad 不能塌）",
          ".pad-view {" in tp and "display: flex;" in tp.split(".pad-view {")[1][:220]
          and "height: 100%;" in tp.split(".pad-view {")[1][:220])

    fw_ck = (ROOT / "dev" / "firmware" / "bridge" / "main"
             / "br_ckeys.c").read_text(encoding="utf-8")
    check("固件：自定义键清单节流上报（连发会丢）",
          "esp_timer_create" in fw_ck
          and "esp_timer_start_once(s_tx_timer, 10000)" in fw_ck)
    ctl_src = PC_CONTROLLER.read_text(encoding="utf-8")
    check("PC：左右卡片各自分行，避免同行遮挡",
          "left_row = right_row = 0" in ctl_src
          and "row_for_card = left_row" in ctl_src)

    check("固件：手机快捷键上报 EV KEY（PC 专属动作靠它）",
          "report_phone_key" in fw_phone_txt
          and 'br_proto_send_ev("KEY"' in fw_phone_txt
          and "BR_SRC_PHONE" in fw_phone_txt
          and "report_phone_key(usage, down)" in fw_phone_txt)

    # 37. ★ 手机自定义快捷键页（v1.4.0）★
    fw_ckeys = (ROOT / "dev" / "firmware" / "bridge" / "main" / "br_ckeys.c").read_text(encoding="utf-8")
    fw_app_txt = FW_APP.read_text(encoding="utf-8")
    sp = tp  # v1.6：快捷键变成触控板页内的第三种模式，不再有独立页面
    ctl = PC_CONTROLLER.read_text(encoding="utf-8")
    i18n_txt = (ROOT / "dev" / "firmware" / "bridge" / "web" / "static" / "js"
                / "i18n.js").read_text(encoding="utf-8")
    check("自定义键：上限与 usage 公式三处一致",
          "BR_PHU_CUSTOM_MAX  64u" in fw_types_txt
          and "BR_PHU_CUSTOM_BASE 0x100u" in fw_types_txt
          and "CUSTOM_KEY_MAX = 64" in layout
          and "CUSTOM_KEY_USAGE_BASE = 0x100" in layout)
    check("固件：自定义键 NVS 与上报",
          "ckeys" in fw_ckeys and "ckdone" in fw_ckeys
          and "br_ckeys_report" in fw_ckeys)
    check("固件：三条 CDC 命令 + ckget",
          all(x in fw_app_txt for x in ("CKBEGIN", "CKADD", "CKCOMMIT"))
          and "ckget" in fw_phone_txt)
    check("手机：快捷键是页内第三种模式（保留输入框与 6 键）",
          'id="ck-view"' in sp and 'id="ck-grid"' in sp
          and "socket.on('ck'" in sp and "ckdone" in sp
          and "m !== 'gyro' && m !== 'ck'" in sp)
    ctl_txt = PC_CONTROLLER.read_text(encoding="utf-8")
    check("PC：自定义键坐标 + 热点 + 拖动换位",
          "CUSTOM_AREA" in layout and "def moveCustomKey" in ctl_txt
          and "def hotspots" in ctl_txt and "phoneShotSource" in ctl_txt)
    check("手机：页面主动拉清单",
          "socket.emit('ckget'" in sp)
    check("手机：底部快捷键切换模式而不是跳页",
          "shortcut.html" not in tp and "setMode(mode === 'ck' ? 'pad' : 'ck')" in tp)
    check("手机：快捷键页支持手指拖动滚动（不是只能脚本滚）",
          "function ckScroll" in tp and "view.scrollTop = startTop - dy" in tp
          and "touch-action: pan-y" in tp)
    check("手机：按钮用 click 触发，不拦滚动手势",
          "b.addEventListener('click'" in tp)
    check("图标：最右快捷键是浏览器线稿（不是播放三角）",
          'aria-label="打开网站（长按编辑）"' in tp
          and 'circle cx="12" cy="14.1" r="3.2"' in tp
          and "10.3 11.7" not in tp)
    led_c = (ROOT / "dev" / "firmware" / "bridge" / "main" / "br_led.c").read_text(encoding="utf-8")
    gatt_c = (ROOT / "dev" / "firmware" / "bridge" / "main" / "br_ble_gatt.c").read_text(encoding="utf-8")
    core_c = (ROOT / "dev" / "firmware" / "bridge" / "main" / "br_ble_core.c").read_text(encoding="utf-8")
    check("灯：手机在线也算有链路，不常亮蓝呼吸",
          "s_phone_linked" in led_c and "!s_connected && !s_phone_linked" in led_c
          and "bridge_led_set_phone_linked(true)" in gatt_c
          and "bridge_led_set_phone_linked(false)" in gatt_c)
    setup_html = (ROOT / "dev" / "firmware" / "bridge" / "apk" / "setup.html").read_text(encoding="utf-8")
    check("设置页：不再有 WiFi 地址回退入口",
          "改用 WiFi 地址" not in setup_html)
    gatt_src = (ROOT / "dev" / "firmware" / "bridge" / "main" / "br_ble_gatt.c").read_text(encoding="utf-8")
    check("名称：广播名是 remote-bridge",
          'PHONE_DEV_NAME "remote-bridge"' in gatt_src
          and '"remote-bridge".equals(name)' in apk_java)
    check("配对：删除手机后拒绝连接，配对窗口可恢复",
          "s_phone_blocked" in gatt_src and "br_ble_gatt_forget_phone" in gatt_src
          and "PHONEFORGET" in FW_APP.read_text(encoding="utf-8"))
    check("配对：手机遇到 denied 会退避重试",
          r'\"ev\":\"denied\"' in apk_java and "retryNotBefore" in apk_java)

    check("灯：发起连接不再把状态打回未连接",
          "bridge_led_set_connected(false)" not in core_c)

    check("日志：手机键用真实蓝牙地址（不是 50:50 虚拟地址）",
          'addr == remote_layout.BR_PHONE_ADDR' in PC_CONTROLLER.read_text(encoding="utf-8")
          and "self._phone_id_addr or self._phone_addr" in PC_CONTROLLER.read_text(encoding="utf-8"))
    voice_js = (ROOT / "dev" / "firmware" / "bridge" / "web" / "static" / "js"
                / "voice.js").read_text(encoding="utf-8")
    check("语音：松手立即退出，不等本机识别结果",
          "T('pad.stt.recognizing')" not in voice_js)

    check("手机：完整清单按批次覆盖（删除能同步）",
          "burst = {}" in tp and "items = burst" in tp
          and "Number(d.n) === 0" in tp)
    check("文案：底部按钮叫「快捷键」，且中英齐全",
          "<span data-i18n=\"pad.mode.shortcut\">快捷键</span>" in tp
          and "'pad.mode.shortcut': '快捷键'" in i18n_txt
          and "'pad.mode.shortcut': 'Shortcuts'" in i18n_txt)
    check("翻译：新增 aria/设置页提示都有中英键",
          "'pad.shortcut.aria'" in i18n_txt
          and "Custom shortcuts" in i18n_txt
          and "'set.ble.connected'" in i18n_txt
          and "'set.ble.connecting'" in i18n_txt
          and "data-i18n-aria=\"pad.shortcut.aria\"" in tp)
    check("PC：custom_keys 读写与 CKEYS 推送",
          "custom_keys" in ctl and 'proto.request("CKBEGIN"' in ctl
          and "def addCustomKey" in ctl and "def removeCustomKey" in ctl)
    check("i18n：快捷键页文案",
          "'ck.title'" in i18n_txt and "'ck.empty'" in i18n_txt
          and "'app.title.ck'" in i18n_txt)

    print("=" * 66)
    if FAILURES:
        print(f"v1.1.0 契约闸门：{len(FAILURES)} 项不通过")
        for f in FAILURES:
            print(f"  - {f}")
        print("=" * 66)
        return 1
    print("v1.1.0 契约闸门：全部通过")
    print("=" * 66)
    return 0


def test_contract() -> None:
    """pytest 入口"""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
