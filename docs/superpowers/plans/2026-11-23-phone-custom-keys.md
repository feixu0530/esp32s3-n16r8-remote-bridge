# 手机自定义快捷键页（v1.4.0）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 手机新增独立「快捷键」页面（3 列、最多 12 个自定义键），键的增删改名与动作全部在 PC 端「按键」页配置。

**Architecture:** PC 配置为唯一真相，经 USB CDC（`CKBEGIN/CKADD/CKCOMMIT`）下发到板子 NVS；手机连上后由板子逐条推 `EV` 事件（`ck` / `ckdone`）渲染页面；点击发 `tap_key {key:"ck<id>"}`，键位空间 `6:(0x100+id)` 复用既有映射表与 `EV KEY`，组合键板子执行、打开软件由 PC 执行。

**Tech Stack:** ESP-IDF v6.1 / NimBLE / NVS；PySide6 + QML；Android WebView + 本地 assets；apktool + javac/d8。

**Spec:** `docs/superpowers/specs/2026-11-23-phone-custom-keys-design.md`

## Global Constraints

- 版本：**1.4.0**（功能新增，第二位 +1）；不得覆盖 `releases/1.3.x`。
- 键位公式三处一致：`usage = 0x100 + id`，`id ∈ 1..12`，`key_id = "6:<usage>"`，手机键名 `ck<id>`。
- 不硬编码 MAC；不要求 CA；发布物必须带 `SHA256SUMS.txt`（LF + `sha256sum -c` 自检）。
- 界面文案必须走 i18n（中/英），新增控件不得出现「死控件」。
- 图标只做文字自动图标（不引入图片传输）。
- 固件改动后必须真机验证（板子日志/`INFO`），不能只跑客户端测试。

---

### Task 1: 两端常量与手机键名解析（固件）

**Files:**
- Modify: `dev/firmware/bridge/main/br_types.h`
- Modify: `dev/firmware/bridge/main/br_phone.c`
- Test: `dev/client/tests/test_v110_contract.py`（Task 6 汇总，这里先改源码）

**Interfaces:**
- Produces: `BR_PHU_CUSTOM_BASE 0x100`、`BR_PHU_CUSTOM_MAX 12`（br_types.h）
- Produces: `quickbar_usage("ck3", &usage)` → true，`usage=0x103`

- [ ] **Step 1: 加常量**

```c
/* br_types.h，接在 BR_PHU_SHORTCUT 后 */
#define BR_PHU_CUSTOM_BASE 0x100u  /* 自定义键 usage 起点：0x100 + id */
#define BR_PHU_CUSTOM_MAX  12u
```

- [ ] **Step 2: 解析 ck<id>**

`quickbar_usage()` 内追加（放 `shortcut` 分支后）：

```c
if (name[0] == 'c' && name[1] == 'k') {
    int id = 0;
    const char *p = name + 2;
    while (*p >= '0' && *p <= '9') {
        id = id * 10 + (*p - '0');
        if (id > 999) return false;
        p++;
    }
    if (*p == 0 && id >= 1 && id <= (int)BR_PHU_CUSTOM_MAX) {
        *usage = (uint8_t)(BR_PHU_CUSTOM_BASE + id);
        return true;
    }
    return false;
}
```

> 注意：`usage` 是 `uint8_t`，0x100+id 会溢出。把 `quickbar_usage/phone_mapped_key/quickbar_default` 的 `usage` 改成 `uint16_t`，`BR_KEY_MAKE` 已按 12 位取值，不受影响。全文件所有 `uint8_t usage` 相关处一并改。

- [ ] **Step 3: 编译固件**

Run: `python dev/firmware/bridge/build_fw.py`
Expected: `exit code: 0`，无 warning 关于 usage 截断。

---

### Task 2: 固件 `br_ckeys` 模块（NVS + 上报）

**Files:**
- Create: `dev/firmware/bridge/main/br_ckeys.c`
- Create: `dev/firmware/bridge/main/br_ckeys.h`
- Modify: `dev/firmware/bridge/main/CMakeLists.txt`（source 列表）
- Modify: `dev/firmware/bridge/main/br_ble_gatt.c`（订阅/连上后上报）
- Modify: `dev/firmware/bridge/main/app_main.c`（`CKBEGIN/CKADD/CKCOMMIT/CKGET`）

**Interfaces:**
- Produces: `void br_ckeys_init(void);`
- Produces: `bool br_ckeys_begin(int count);`
- Produces: `bool br_ckeys_add(int id, const char *name);`
- Produces: `bool br_ckeys_commit(void);`
- Produces: `int br_ckeys_count(void);`
- Produces: `void br_ckeys_report(void);`（推 `ck`/`ckdone` 给手机）

- [ ] **Step 1: 数据结构与 NVS**

```c
#define CKEYS_NS    "ckeys"
#define CKEYS_KEY   "list"
#define CKEYS_MAGIC 0x434B0001u
#define CKEYS_VER   1
#define CKEYS_NAME  24

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  version;
    uint8_t  count;
    uint8_t  ids[BR_PHU_CUSTOM_MAX];
    char     names[BR_PHU_CUSTOM_MAX][CKEYS_NAME];
} ckeys_blob_t;
```

`br_ckeys_init()` 读 NVS，校验 magic/version/count≤12，否则清空。

- [ ] **Step 2: CDC 命令**

`app_main.c` 分发：

```c
} else if (strcmp(verb, "CKBEGIN") == 0) {
    long n = 0;
    br_proto_arg_int(&p, 1, &n);
    if (!br_ckeys_begin((int)n)) br_proto_send_err("CKBEGIN", "args", NULL);
    else br_proto_send_ok("CKBEGIN", NULL);
} else if (strcmp(verb, "CKADD") == 0) {
    long id = 0; const char *nm = argc > 2 ? p.argv[2] : "";
    if (!br_proto_arg_int(&p, 1, &id) || !br_ckeys_add((int)id, nm))
        br_proto_send_err("CKADD", "args", NULL);
    else br_proto_send_ok("CKADD", NULL);
} else if (strcmp(verb, "CKCOMMIT") == 0) {
    if (!br_ckeys_commit()) br_proto_send_err("CKCOMMIT", "nvs", NULL);
    else { br_proto_send_ok("CKCOMMIT", NULL); br_ckeys_report(); }
}
```

- [ ] **Step 3: 上报**

```c
void br_ckeys_report(void)
{
    char buf[96];
    for (int i = 0; i < s_count; i++) {
        snprintf(buf, sizeof(buf), "{\"id\":%u,\"name\":\"%s\"}",
                 (unsigned)s_ids[i], s_names[i]);   /* 名字已做 JSON 转义 */
        br_ble_gatt_phone_send("ck", buf);
    }
    snprintf(buf, sizeof(buf), "{\"n\":%d}", s_count);
    br_ble_gatt_phone_send("ckdone", buf);
}
```

在 `br_ble_gatt.c` 的 `BLE_GAP_EVENT_SUBSCRIBE`（`cur_notify` 为真）与 `BLE_GAP_EVENT_CONNECT` 成功分支末尾调用 `br_ckeys_report()`。

- [ ] **Step 4: 编译 + 烧录 + 验证空列表**

Run: `python dev/firmware/bridge/build_fw.py && python dev/firmware/bridge/flash_auto.py`
然后用 CDC 发 `CKBEGIN 0` `CKCOMMIT`，期望 `OK`；手机连上时收到 `{"ev":"ckdone","data":{"n":0}}`（用 CDP 在页面里 `window.__ckDebug=[]; window.addEventListener` 不方便，改为在 `shortcut.html` 完成后验证）。

---

### Task 3: 手机页面 `shortcut.html`

**Files:**
- Create: `dev/firmware/bridge/web/shortcut.html`
- Modify: `dev/firmware/bridge/web/static/js/i18n.js`
- Modify: `dev/firmware/bridge/tools/pack_apk_assets.py`（页面列表）
- Modify: `dev/firmware/bridge/web/touchpad.html`（底部键改导航；删除图标面板）

**Interfaces:**
- Consumes: BLE 事件 `ck`（`{id,name}`）、`ckdone`（`{n}`）
- Produces: `tap_key {key:"ck<id>"}`

- [ ] **Step 1: 页面骨架**

```html
<main id="grid"></main>
<script>
(function(){
  var items = {};
  socket.on('connect', requestSync);
  socket.on('ck', function(d){ if (d && d.id) items[d.id] = d.name || ''; });
  socket.on('ckdone', function(){ render(); });
  function requestSync(){ items = {}; try { socket.emit('ckget', {}); } catch(e){} }
  function render(){ /* 3 列网格；空态提示「到电脑端『按键』页添加」 */ }
  function tap(id){
    try { socket.emit('tap_key', { key: 'ck' + id }); } catch(e){}
    try { haptic('tap'); } catch(e){}
  }
})();
</script>
```

- [ ] **Step 2: 文字图标**

每格：`div.ck-avatar` 显示 `name.slice(0,2)`；背景色按 id 从固定 8 色表取；与现有圆角/线稿风格一致。

- [ ] **Step 3: 底部键改导航**

`touchpad.html`：`#shortcut-btn` 的点击从 `tap_key` 改为 `location.href='shortcut.html'`；移除长按图标面板与 `shortcut_icon` 相关代码和样式。保留按钮文字「快捷」与键盘图标。

- [ ] **Step 4: 打包资源**

`pack_apk_assets.py` 的页面循环加入 `shortcut.html`（它不需要 fix_html 的路径改写，但统一走同一函数无副作用）。

Run: `python dev/firmware/bridge/tools/pack_apk_assets.py`
Expected: 输出包含 `页面 shortcut.html … 字节`。

---

### Task 4: PC 控制器（数据模型 + 推送 + PC 动作解析）

**Files:**
- Modify: `dev/client/src/core/remote_layout.py`
- Modify: `dev/client/src/core/controller.py`
- Test: `dev/client/tests/test_gates.py`（Task 6）

**Interfaces:**
- Produces: `CUSTOM_KEY_MAX=12`、`custom_key_id(i)->"6:257"`、`custom_button_id(i)->"ck1"`（remote_layout）
- Produces: `Bridge.customKeys`（QVariantList: id/name/key_id/button_id）
- Produces: `Bridge.addCustomKey()`、`Bridge.removeCustomKey(button_id)`、`Bridge.renameCustomKey(button_id, name)`
- Consumes: 现有 `setMapping(key_id, trigger, text)`（动作编辑走它）

- [ ] **Step 1: 常量与辅助**

```python
CUSTOM_KEY_MAX = 12
CUSTOM_KEY_USAGE_BASE = 0x100

def custom_button_id(kid: int) -> str:
    return "ck%d" % kid

def custom_key_id(kid: int) -> str:
    return "%d:%d" % (BR_SRC_PHONE, CUSTOM_KEY_USAGE_BASE + kid)
```

- [ ] **Step 2: 模型读写**

手机条目 `custom_keys`：`[{"id":1,"name":"截图"}]`。
`_load_custom_keys()`（在 `selectRemote`/`_bindings` 更新处调用）把列表规范化（去重、id 1..12、名字截断 24 字节）。
`addCustomKey()` 取最小空闲 id，默认名 `快捷键 N`，保存配置后 `_push_custom_keys()`。

- [ ] **Step 3: 推送 CKEYS**

```python
def _push_custom_keys(self) -> None:
    if self._proto is None:
        return
    keys = self._custom_keys
    self._proto.send("CKBEGIN %d" % len(keys))
    for k in keys:
        self._proto.send("CKADD %d %s" % (k["id"], k["name"].replace(" ", "_")))
    self._proto.send("CKCOMMIT")
```

> 命令是按空格分词的，名字里的空格先用 `_` 占位，板子侧上报时把 `_` 还原成空格。禁止换行/引号（前端限制输入长度与字符）。

- [ ] **Step 4: buttonList 加入自定义键**

`_remote_keys()` 与 `buttonList` 构造时，若 `kind==phone`，在固定 5 行后追加自定义键行：

```python
{"button_id": "ck%d" % kid, "button_name": name, "key_id": "6:%d" % (0x100+kid),
 "single": self._action_text(key_id, SINGLE), "double": "", "long": "",
 "short_name": name[:4], "is_voice": False, "visual_row": 3 + idx, "side": "right"}
```

- [ ] **Step 5: PC 动作解析**

`_pc_key_event` 里 `usage_to_button_id_for(LAYOUT_PHONE)` 查不到时：

```python
if src == remote_layout.BR_SRC_PHONE and usage >= 0x100:
    button_id = remote_layout.custom_button_id(usage - 0x100)
    key_id = f"6:{usage}"
```

- [ ] **Step 6: 删除时清理映射**

`removeCustomKey`：从配置删项 → `clear_button_action(bindings, key_id, SINGLE)` → `_save_bindings()`（会推 MAPSET/COMMIT）→ `_push_custom_keys()`。

---

### Task 5: PC 端「按键」页 UI（手机布局）

**Files:**
- Modify: `dev/client/src/ui/qml/ButtonsPage.qml`

**Interfaces:**
- Consumes: `Bridge.customKeys`、`Bridge.buttonList`（已含 ck 行）、`Bridge.addCustomKey()` 等

- [ ] **Step 1: 管理条**

手机布局（`Bridge.selectedRemoteKind !== "chromecast"` 或现有 kind 判断）时，在卡片区上方加一行：

```
[＋ 添加快捷键]   名称A ✕   名称B ✕   …
```

`＋` → `Bridge.addCustomKey()`；每项点击名字 → 打开改名对话框（复用 DevicePage 的 `phoneNameDialog` 模式，新写一个 `customKeyNameDialog`）；`✕` → 确认后 `Bridge.removeCustomKey(button_id)`。

- [ ] **Step 2: 卡片渲染**

自定义键已作为 `buttonList` 行，右侧列表按 `visual_row` 自动排（确认 `rightCardRows` 上限 ≥ 3+12，改 `readonly property int rightCardRows: 8` → `20`）。

点击卡片 → 现有 `selectButton(button_id)` → 动作编辑器（组合键/打开软件）直接可用，无需改动编辑器。

- [ ] **Step 3: 空态**

没有自定义键时，卡片区仍显示固定 5 行，管理条只有「＋ 添加快捷键」。

---

### Task 6: 契约闸门 §37 + 测试更新

**Files:**
- Modify: `dev/client/tests/test_v110_contract.py`
- Modify: `dev/client/tests/test_gates.py`
- Modify: `dev/client/tests/test_ui_render.py`（若导航/页面数量断言受影响）

- [ ] **Step 1: 三处一致**

```python
check("自定义键：上限与 usage 公式三处一致",
      "BR_PHU_CUSTOM_MAX  12u" in fw_types_txt
      and "CUSTOM_KEY_MAX = 12" in layout
      and "CUSTOM_KEY_USAGE_BASE = 0x100" in layout)
```

- [ ] **Step 2: 固件/手机/PC 结构**

```python
check("固件：自定义键 NVS 与上报", "ckeys" in fw_ckeys and "ckdone" in fw_ckeys)
check("固件：三条 CDC 命令", all(x in fw_app for x in ("CKBEGIN", "CKADD", "CKCOMMIT")))
check("手机：快捷键页存在且 3 列", "shortcut.html" in pages and "grid-template-columns" in sp)
check("手机：底部键导航", "location.href='shortcut.html'" in tp)
check("PC：custom_keys 读写与推送", "custom_keys" in ctl and "CKBEGIN %d" in ctl)
```

- [ ] **Step 3: 手机行数断言**

`test_gates` 当前断言手机 6 行 → 改为「固定 5 行 + 自定义键」；无自定义键时 5 行。

- [ ] **Step 4: 跑全部脚本测试**

Run（逐个）：`test_foundation / test_connect_lifecycle / test_gates / test_mapping_roundtrip / test_protocol / test_scan_list / test_ui_render / test_v017 / v020..v024 / test_v110_contract`
Expected: 全 rc=0。

---

### Task 7: 版本 1.4.0 + 真机验收 + 发布

**Files:**
- Modify: `dev/client/src/core/version.py`、`dev/firmware/bridge/main/app_main.c`、`test_v017/test_v020` 版本钉
- Modify: `DEV_PROGRESS.md`、`releases/1.4.0/*`

- [ ] **Step 1: 真实场景验收（PC 客户端 + 板子 + 手机）**

1. PC「按键」页手机布局：添加 3 个键，分别配 `Ctrl+Shift+P`、打开记事本、中文名「截图」。
2. 手机打开快捷键页：应出现 3 格，名字与顺序正确，文字图标不同色。
3. 点组合键：板子日志 `HIDTX keyboard mod=03 key0=13`，**关掉 PC 客户端仍有效**。
4. 点打开软件：`Notepad.exe` 启动。
5. 改名/删除：手机页面同步；重启手机 App 后列表仍在。
6. 断线重连（关开手机蓝牙）：列表自动恢复。

- [ ] **Step 2: 发版**

```bash
python dev/firmware/bridge/build_fw.py
python dev/firmware/bridge/flash_auto.py
python dev/firmware/bridge/tools/pack_apk_assets.py
python dev/apk/build_apk.py --out C:/tmp/Movo-1.4.0-ble.apk
python dev/client/build/package.py --version 1.4.0 --notes "…"
MOTO_APK_DIR=C:/tmp/apk140 python dev/client/build/make_release.py --version 1.4.0
# 补 firmware/ firmware-wifi/ bootloader/ partition-table/ 与 -ble 副本
python tools/check_release.py 1.4.0
```

Expected: `check_release.py` 全部通过。

---

## Self-Review 记录

- 规格覆盖：入口/图标/上限/PC 管理/固件存储/手机页/闸门/发版均有对应任务。
- 名称一致性：`ck<id>`、`6:(0x100+id)`、`EV ck`/`ckdone`、`CKBEGIN/CKADD/CKCOMMIT` 在任务 1~7 中一致。
- 已知取舍：名字用 `_` 代替空格（协议按空格分词）；不改旧 `6:6` 映射；文字图标不做图片传输。
