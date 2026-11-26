# 按键映射与同步（简化版）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「插上板子就能用」这件事安全地回来：PC 改映射立刻同步到板子，新配对/删过重配的遥控器自动铺默认按键，同时**板子绝不可能把 PC 的键盘按死**。

**Architecture:** 三层，各管一件事、互不牵扯：
1. **固件安全网**（USB HID 输出层）——任何按下的键/修饰键都有「强制抬起」兜底，板子在任何情况下都不可能让 PC 的键盘卡住。
2. **固件默认档案**（按遥控器型号）——只在**配对成功那一刻**铺一次，之后用户怎么改都是用户的事。
3. **PC 端同步**（单向）——PC 是唯一真值，改一下就整份推给板子。**不做条数比对、不做冲突弹窗、不做比对页**。

**Tech Stack:** ESP-IDF v6.1 + NimBLE + TinyUSB（复合 HID 键盘/消费者 + UAC 麦克风 + CDC）；PySide6/QML；esptool；自研脚本式闸门测试。

**Spec:** 用户本轮口述 + 前几轮实测记录（见本文「需求核对」一节）。参考项目只借鉴它的**按型号分档 + 导入导出**做法，不抄它那套 PC 侧直连设备的检测。

## Global Constraints

- 目录职责（PROJECT_RULES.md）：`dev/` 开发、`reference/` 参考、`releases/` 发布、`temp/` 临时、`tools/` 工具。
- 版本：开发期 `0.x.y`；**大功能递增第二位**（本次 `0.21.1` → `0.22.0`），Bug 修复递增第三位；功能完成并获用户确认后才发 `1.0.0`。
- `releases/releases_node.md` 必须维护（打包脚本已自动追加）。
- 每次打包**换新版本号、禁止覆盖旧产物**、必写 `changes.md`（写清"为什么改"）。
- `SHA256SUMS.txt` 用 **LF** 换行，生成后必须跑 `sha256sum -c SHA256SUMS.txt` 自检通过。
- 验收流水线**必须自带硬闸门**（脚本式测试，退出码非 0 即失败），不能靠人工抽查收尾。
- **不要大改固件的配对遥控器与识别按键链路**；本计划只碰 USB HID 输出层、映射表、手势触发。
- 固件版本号（`app_main.c` 的 `FW_VERSION`）**每次改固件都必须升**，否则板子报的号是假的。
- 本项目**不是 git 仓库**：每个任务的「提交」等价物 = 该任务的闸门测试全绿（见每个 Task 末尾）。

---

## 参考项目怎么做的（调研结论，决定了我们抄什么）

参考项目 `reference/winmic-src/apps/windows/rc003/src/ovb_rc003/` 的做法：

- **PC 端映射为主，设备是纯输入设备** —— 它**根本没有下行通道**，
  所以**压根没有「设备 vs PC」的比对**，也就没有冲突弹窗。
- 它用四个机制替代了"两边比对"：
  1. **单一权威源 + 只读投影**：映射只有一份，别的都是投影；
  2. **整份覆盖 + 原子写**：应用预设 = 三字段整份替换，写盘用临时文件 + `os.replace`；
  3. **失败回退（fail-safe）**：新文件解析失败 → **保留旧映射**，只记 warning；
  4. **idle 时机**：正在按住的键不会被中途换掉，挂起等空闲再应用。
- **profile 贯穿全链**：每个型号有自己的「键位清单 / 显示名 / 默认映射 / usage 表」，
  映射数据里必须带 `profile` 字段，所有入口都校验它。
- **导入导出格式**（`mapping_presets.py`）：
  `{"format":"RemoteMic.key-mapping","schema_version":1,"name","profile",
    "mapping":{"bindings","secondary_bindings","display_notes"}}`
  校验很严：profile 必须匹配、**必须包含该型号的全部按键**、
  多余的键拒绝、备注 ≤256 字、语音键不能同时配双击/长按、导入后**不立刻生效**（要点"应用"）。
- **「恢复内置默认」恢复的是** `remote_layout.default_actions(profile)` —— 按型号取默认表。

⇒ **我们照抄的部分**：
① PC 是主、整份覆盖、**不比对不弹窗**（与你的要求一致）；
② profile 分档（键位清单 + 默认表 + 导入导出的命名空间）；
③ 导入导出格式与校验照它的规格收紧；
④ 固件侧沿用我们已有的 `MAPBEGIN → MAPSET×N → MAPCOMMIT`
（固件的 blob 是一次写入，**PC 推失败时旧版本原样还在** —— 这正是参考项目的 fail-safe）。

⇒ **我们必须自己设计的**：参考项目没有设备，所以「下发」是我们独有的。
但它那套「整份覆盖 + 校验失败保留旧值 + 不做部分应用」正好可以直接搬。

---

## 需求核对（把前几版的要求重新过一遍）

| # | 来自哪一轮 | 要求 | 本计划怎么落 |
|---|---|---|---|
| 1 | v0.21.1 回退 | **板子不能把 PC 的键盘按死** | Task 1（看门狗 + 去掉抢修饰键的语音条目） |
| 2 | 本轮口述 | **每次按键的修改自动同步到开发板**（不用点按钮） | Task 3（PC 改一下就整份下发；设备页不再有「同步」按钮） |
| 3 | 本轮口述 | **新连接的遥控器 / 删除过重新连接的遥控器 → 自动配置默认按键** | Task 2（固件在配对成功那一刻铺默认档案） |
| 4 | 本轮口述 | **别搞复杂的条数比对与再同步** | Task 3（删掉比对页、冲突弹窗、条数比对；只留导入导出） |
| 5 | 本轮口述 | **参考项目的按键映射已完善，去看看它怎么做** | Task 2 的档案结构照它；Task 3 的导入导出格式照它 |
| 6 | v0.19.0 | 新烧录固件插上就能用 | Task 2（默认档案进固件） |
| 7 | v0.20.0 | 默认映射**按遥控器型号分档**（现在是谷歌遥控器一档） | Task 2（`k_default_profiles[]` 按名字匹配型号） |
| 8 | v0.20.0 | 删除配对后重配 = 新遥控器 | Task 2（解绑时清掉该遥控器的映射 → 重配时板子是空的 → 铺默认） |
| 9 | v0.21.0 | 遥控器名字列显示全 | 已在 v0.21.0 完成，Task 4 只做回归确认 |
| 10 | v0.21.0 | 按键页遥控器图片清晰 | 已完成，Task 4 回归确认 |
| 11 | v0.21.0 | 编辑下拉框抄参考项目 | 已完成，Task 4 回归确认 |
| 12 | v0.21.0 | 应用图标 | 已完成，Task 4 回归确认 |
| 13 | v0.21.0 | 返回键长按 = Del（连删） | Task 2 保留（看门狗保证长按不会卡键） |
| 14 | v0.21.0 | 刷写信息框平时折叠 | 已完成，Task 4 回归确认 |
| 15 | v0.21.1 | 映射快照不能丢同一个键的多个手势 | 已完成（快照按 `(键, 触发)` 存），Task 4 回归确认 |
| 16 | v0.21.1 | 诊断日志 MAC 方向正确 | 已完成（固件统一用 `addr_to_str`），Task 4 回归确认 |
| 17 | v0.21.1 | 语音日志不能说谎 | 已完成（事件带 `sent`/`on`），Task 4 回归确认 |
| 18 | v0.17.0 | 按 MAC 归档映射，重刷后自动恢复 | **降级**：归档仍在，但不再参与"两边比对"；只作为导入导出的备份 |
| 19 | v0.17.0 | 语音键默认左Ctrl+左Win、PC 端可配 | 已完成，Task 4 回归确认 |
| 20 | v0.18.0 | 设备页一行一处刷写入口、断开后清空信息 | 已完成，Task 4 回归确认 |
| 21 | **本轮新增** | **按键页的语音键不可配置**，只显示语音页配好的按键组合；板子上语音**只走语音途径**，两者不冲突 | Task 2：默认档案**去掉语音键那条**；按键页语音行保持只读显示（`controller.py` 的 `_load_mappings()` 里语音行走 `self._voice_key_label()`，本来就不进映射表） |
| 22 | **本轮新增** | 默认档案不只按型号分档，要按**遥控器型号 + MAC 地址**分，**记录在板子上** | Task 2：配对表条目（`pl_entry_t`）新增 `profile` 字段，匹配到的默认档案 id 存进 NVS；同一台重配时按 identity 找回原档案 → 用回同一套默认 |
| 23 | **本轮新增** | **返回键长按 = Del** 要真的「按住缓慢连删、抬起停止」，不能「一下全删」 | Task 1：抬起那一半丢了（手势状态找不到时不发抬起 → 键永远按着 → 系统自动重复 → 文字一下全没）。修抬起路径 + 看门狗兜底。**语义本身不用改**（参考项目的长按是"到阈值触发一次"，我们要的是"按住不放"，所以要按我们的语义保留按住，只补抬起） |

**明确砍掉**（用户说太复杂）：映射比对页、冲突二选一弹窗、「板子 N 条 / 本机 M 条」比对、设备页行内「同步」按钮。

---

## 文件结构

| 文件 | 职责 | 本计划怎么动 |
|---|---|---|
| `dev/firmware/bridge/main/br_usb.c` / `.h` | USB HID 输出（键盘/消费者） | **加按键释放看门狗** |
| `dev/firmware/bridge/main/br_voice.c` / `.h` | 语音通道（ATVV 解码 + 语音热键） | **热键加超时释放** |
| `dev/firmware/bridge/main/br_mapping.c` | 映射表（存/取/出厂默认档案） | 默认档案去掉语音键条目 |
| `dev/firmware/bridge/main/br_ble_core.c` | BLE 主机（扫描/连接/配对表） | 主循环里调看门狗；配对成功铺默认 |
| `dev/client/src/core/controller.py` | PC 端总控（协议、映射、UI 数据） | 简化同步：去掉比对/冲突；改映射即下发 |
| `dev/client/src/core/remote_layout.py` | 型号档案（键位清单 + 默认动作） | 默认档案与固件对齐 |
| `dev/client/src/ui/qml/ButtonsPage.qml` | 按键页 | 去掉「映射比对」入口，改为「导入/导出」 |
| `dev/client/src/ui/qml/DevicePage.qml` | 设备页 | 去掉条数比对与「同步」按钮 |
| `dev/client/src/ui/qml/MappingCompareDialog.qml` | 映射比对弹窗 | **删除**（被砍掉） |
| `dev/client/tests/test_v023.py` | 新闸门 | 本计划的核心闸门 |
| `dev/client/build/package.py` | 打包 | 闸门列表加 `test_v023.py` |

---

## Task 1: 安全网 —— 板子绝不能把 PC 的键盘按死 ★最先做★

**为什么这是第一件事**：用户实测「插上板子 PC 的键盘就失效」。根因有两层，都已定位：

1. **修饰键状态是全局共享的**（`br_usb.c:522-591`：`s_kbd_modifier` / `s_kbd_keys[]`）。
   语音热键按下 `lctrl+lwin`（`s_kbd_modifier |= 0x09`），
   而默认映射里的「语音键 → 左Ctrl」那条按下/抬起会 `&= ~0x01`，
   **把热键的左Ctrl位清掉** → 输入法收到的是「只按了左Win」→ 组合键不成立。
   而语音热键的抬起**只等 ATVV 的 AUDIO_STOP 帧**（`br_voice.c` 的 `case 0x00`），
   遥控器不发这一帧时 **左Win 就一直按着** → PC 的键盘整个废掉（每个键都变成 Win+某键）。
2. **任何一次"按下"若丢了对应的"抬起"，键就永远卡在 PC 上**。

**改法**：不猜哪条路会漏，直接兜底 —— **任何按下的键/修饰键，超过 3 秒强制抬起**。
这样即使某个路径漏了抬起事件，板子也不可能把键盘按死超过 3 秒。

**Files:**
- Modify: `dev/firmware/bridge/main/br_usb.c`（加看门狗状态与函数）
- Modify: `dev/firmware/bridge/main/br_usb.h`（声明 `bridge_usb_hid_watchdog()`）
- Modify: `dev/firmware/bridge/main/br_voice.c`（热键超时释放）
- Modify: `dev/firmware/bridge/main/br_ble_core.c`（主循环里调看门狗）
- Test: `dev/client/tests/test_v023.py`

**Interfaces:**
- Produces: `void bridge_usb_hid_watchdog(void)` —— 扫描已按下的键/修饰键，超时的强制抬起并发一个干净的报告。
- Produces: `void br_voice_watchdog(void)` —— 语音热键超过 `BR_VOICE_HOTKEY_MAX_MS` 未释放则强制释放。

- [ ] **Step 1: 写失败的闸门（静态检查 + 逻辑检查）**

新建 `dev/client/tests/test_v023.py`：

```python
"""
v0.23 闸门：USB HID 安全网 —— 板子绝不能把 PC 的键盘按死。

★ 为什么要有这条 ★
    用户实测：插上板子，PC 自己的键盘就失效；遥控器自动连上后按键也乱。
    根因是 USB HID 的修饰键状态是全局共享的：
      · 语音热键按下 左Ctrl+左Win，默认映射里的「语音键 → 左Ctrl」
        又去 &= ~左Ctrl，把组合键拆成只剩 左Win；
      · 语音热键的抬起只等 ATVV 的 AUDIO_STOP 帧，遥控器不发就一直按着。
    所以固件必须有一道兜底：任何按下的键/修饰键，超时强制抬起。
"""
# 断言 1：br_usb.c 里有看门狗函数与超时常量
# 断言 2：br_ble_core.c 的主循环里调用了它
# 断言 3：br_voice.c 里有语音热键的超时释放
# 断言 4：默认档案里**没有**语音键那条（它和语音热键抢同一个 左Ctrl 位）
```

具体断言：

```python
def test_hid_watchdog_exists() -> None:
    usb = (FW_MAIN / "br_usb.c").read_text(encoding="utf-8")
    check("有按键释放看门狗函数",
          "bridge_usb_hid_watchdog" in usb, "")
    check("有按住超时常量",
          "BR_HID_HOLD_MAX_MS" in usb, "")
    check("看门狗会发一个干净的报告（清空状态）",
          "s_kbd_count = 0" in usb and "s_kbd_modifier = 0" in usb, "")


def test_watchdog_is_called() -> None:
    core = (FW_MAIN / "br_ble_core.c").read_text(encoding="utf-8")
    check("主循环里调用了 HID 看门狗",
          "bridge_usb_hid_watchdog()" in core, "")


def test_voice_hotkey_timeout() -> None:
    voice = (FW_MAIN / "br_voice.c").read_text(encoding="utf-8")
    check("语音热键有超时释放", "br_voice_watchdog" in voice, "")
    check("语音热键有超时常量", "BR_VOICE_HOTKEY_MAX_MS" in voice, "")


def test_default_profile_has_no_voice_key() -> None:
    mapping = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    body = mapping.split("k_binds_chromecast[]", 1)[-1].split("};", 1)[0]
    check("默认档案里没有语音键（它和语音热键抢同一个左Ctrl位）",
          "8755u" not in body, body[:200])
```

- [ ] **Step 2: 跑闸门，确认它红**

Run: `python dev/client/tests/test_v023.py`
Expected: FAIL —— 「有按键释放看门狗函数」等 4 条断言失败。

- [ ] **Step 3: 实现 HID 看门狗（br_usb.c）**

在 `br_usb.c` 里，紧跟 `s_kbd_modifier` / `s_kbd_keys` 的定义处加：

```c
/*
 * ★ 按住多久就强制抬起 ★
 *
 *   板子替 PC 按键时，只要有一次「抬起」事件丢了，
 *   那个键就永远按在 PC 上 —— 表现为 PC 自己的键盘突然失灵
 *   （每个键都变成了 Ctrl+某键）。
 *
 *   这里不猜是哪条路会漏，直接兜底：
 *   任何按下的键/修饰键超过 BR_HID_HOLD_MAX_MS 就强制抬起。
 */
#define BR_HID_HOLD_MAX_MS 3000

static uint32_t s_kbd_stamp[6];    /* 每个普通键的按下时刻 */
static uint32_t s_mod_stamp[8];    /* 每个修饰键位的按下时刻 */
```

在 `bridge_usb_press_keyboard()` 里记录时刻（按下普通键/修饰键位时），
在 `bridge_usb_release_keyboard()` 里清掉对应时刻，
再新增：

```c
/**
 * ★ 按键释放看门狗 ★
 *
 *   把按住超时的键/修饰键强制抬起并发一个干净报告。
 *   主循环每 200 ms 调一次 —— 板子绝不可能把 PC 的键盘按死。
 */
void bridge_usb_hid_watchdog(void)
{
    if (!tud_hid_ready()) {
        return;
    }
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    bool changed = false;

    for (int k = 0; k < s_kbd_count; k++) {
        if (s_kbd_stamp[k] && (now - s_kbd_stamp[k]) > BR_HID_HOLD_MAX_MS) {
            s_kbd_stamp[k] = 0;
            changed = true;
        }
    }
    for (int b = 0; b < 8; b++) {
        if (s_mod_stamp[b] && (now - s_mod_stamp[b]) > BR_HID_HOLD_MAX_MS) {
            s_mod_stamp[b] = 0;
            s_kbd_modifier &= (uint8_t)~(1 << b);
            changed = true;
        }
    }
    if (!changed) {
        return;
    }
    /* 把超时的普通键从按着集合里摘掉 */
    int w = 0;
    for (int k = 0; k < s_kbd_count; k++) {
        if (s_kbd_stamp[k]) {
            s_kbd_keys[w] = s_kbd_keys[k];
            s_kbd_stamp[w] = s_kbd_stamp[w];
            w++;
        }
    }
    s_kbd_count = w;
    uint8_t keycode[6] = {0};
    memcpy(keycode, s_kbd_keys, s_kbd_count);
    send_hid_keyboard_checked(s_kbd_modifier, keycode);
    ESP_LOGW(TAG, "HID 看门狗：强制抬起超时的键");
}
```

同时在 `br_usb.h` 加声明：
```c
/** 按键释放看门狗：按住超时的键强制抬起（主循环周期调用） */
void bridge_usb_hid_watchdog(void);
```

- [ ] **Step 4: 主循环里调看门狗（br_ble_core.c）**

在 `br_ble_task()` 的 `for (;;)` 循环里（与 `run_pending_defaults()` 同一层）加：

```c
        /* ★ 按键释放看门狗 ★
           任何按下的键超过 3 秒就强制抬起 ——
           板子绝不可能把 PC 的键盘按死。 */
        static int64_t last_hid_watchdog = 0;
        if (now - last_hid_watchdog > 200000) {
            last_hid_watchdog = now;
            bridge_usb_hid_watchdog();
        }
```

- [ ] **Step 5: 语音热键超时释放（br_voice.c / .h）**

```c
#define BR_VOICE_HOTKEY_MAX_MS 2000

/**
 * 语音热键看门狗：ATVV 的 AUDIO_STOP 帧不一定来，
 * 遥控器不发就一直按着 → PC 的键盘整个废掉。
 * 超时强制释放。
 */
void br_voice_watchdog(void)
{
    bool down;
    uint32_t press_at;
    portENTER_CRITICAL(&s_hotkey_mux);
    down = s_hotkey_down;
    press_at = s_pressed_at;
    portEXIT_CRITICAL(&s_hotkey_mux);
    if (down && (uint32_t)(now_ms() - press_at) > BR_VOICE_HOTKEY_MAX_MS) {
        voice_hotkey_release();
    }
}
```
（`s_pressed_at` 在 `voice_hotkey_press()` 里记；`now_ms()` 已在该文件里。）

在 `br_ble_core.c` 的主循环里与 HID 看门狗一起调 `br_voice_watchdog();`。

- [ ] **Step 6: 默认档案去掉语音键条目（br_mapping.c）**

在 `k_binds_chromecast[]` 里删掉这一行：

```c
    { 8755u, 0, 1, 0x01, 0, { 0 } },     /* 语音键 → 左Ctrl（板子按出语音快捷键） */
```

★ 为什么 ★：语音热键（`lctrl+lwin`）由 ATVV 通道直接按出，**不依赖这条映射**；
而这条映射会跟热键抢同一个「左Ctrl」状态位，把组合键拆掉。0.18.0 没有默认映射时语音是好的，正是这个原因。

- [ ] **Step 7: 补手势的「抬起兜底」（这是"一下全删"的真凶）**

`br_gesture.c` 的 `br_gesture_release()` 现在是：

```c
    br_gesture_state_t *st = find_state(addr, key_id);
    if (st == NULL || !st->is_pressed) {
        ...
        return;              /* ★ 状态没了 → 什么都不发 → 键永远按着 ★ */
    }
```

★ 症状 ★：状态找不到（地址轮换了 / 状态被 LRU 淘汰）时**直接返回**，
不补一次抬起 → 那个键一直按在 PC 上 → 系统自动重复 →
用户看到的是「按下返回键，输入框的文字一下全被删掉」，
而不是「按住缓慢连删、抬起停止」。

改法：状态找不到时，**也要把该键的抬起补上**：

```c
    br_gesture_state_t *st = find_state(addr, key_id);
    if (st == NULL || !st->is_pressed) {
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        /* ★ 状态丢了也要补一次抬起 ★
           否则那个键永远按在 PC 上 —— 系统自动重复，
           用户看到的是「一下全删」。 */
        fire_trigger(addr, key_id, BR_TRIG_SINGLE, false);
        fire_trigger(addr, key_id, BR_TRIG_LONG, false);
        return;
    }
```

（`fire_trigger(..., press=false)` 在没有绑定时是空操作，不会误伤。）

- [ ] **Step 8: 跑闸门，确认它绿**

Run: `python dev/client/tests/test_v023.py`
Expected: 全部通过。

- [ ] **Step 9: 编译 + 刷板子 + 真机验证「键盘不会被按死」**

```bash
"C:/Espressif/python_env/idf6.1_py3.11_env/Scripts/python.exe" dev/firmware/bridge/build_fw.py
"C:/Espressif/python_env/idf6.1_py3.11_env/Scripts/python.exe" -m esptool --chip esp32s3 -p COM6 -b 460800 \
  --before default_reset --after hard_reset write-flash 0x10000 dev/firmware/bridge/build/remote_bridge.bin
```

真机验收：
1. 插上板子 → **立刻用 PC 自己的键盘打字**，必须完全正常（这是用户报的原始症状）。
2. 遥控器自动连上 → 按遥控器的各个键（方向/主页/返回/语音）→ PC 的键盘**必须始终正常**。
3. 按住遥控器的返回键 5 秒（长按连删）→ 松开后 PC 的键盘仍正常。

**这一步没通过就不许往下做**（用户的键盘比什么都重要）。

---

## Task 2: 固件默认档案（按型号分档，新配对才铺）

**Files:**
- Modify: `dev/firmware/bridge/main/br_mapping.c`（`k_binds_chromecast[]` 内容对齐 PC）
- Modify: `dev/firmware/bridge/main/br_ble_core.c`（配对成功铺默认；解绑清映射）
- Modify: `dev/client/src/core/remote_layout.py`（默认档案与固件一致）
- Test: `dev/client/tests/test_v023.py`（追加）

**Interfaces:**
- Consumes: `br_mapping_apply_defaults(const char *addr, const char *name)`（已有）
- Produces: 默认档案两边**逐条一致**（固件 14 条 ↔ PC 14 条可下发 + 1 条 PC 独有）

- [ ] **Step 1: 写失败的闸门：两边默认档案逐条一致**

在 `test_v023.py` 追加：

```python
def test_default_profile_matches_pc() -> None:
    """固件的默认档案必须和 PC 的逐条一致（否则两边对不上）。"""
    fw = _parse_firmware_defaults()      # 复用 test_v020.py 的解析函数
    pc = _pc_default_shape()             # 复用 test_v020.py 的构造
    check("两边条数一致", len(fw) == len(pc), f"固件{len(fw)} PC{len(pc)}")
    diff = [k for k in pc if fw.get(k) != pc[k]]
    check("逐条一致", not diff, str([(k, fw.get(k), pc.get(k)) for k in diff]))
```

（把 `test_v020.py` 里的 `_parse_firmware_defaults()` / `_pc_default_shape()` 抽到一个共用小模块
`dev/client/tests/_default_tables.py`，两个测试文件都 import 它 —— 不要复制两份。）

- [ ] **Step 2: 跑闸门，确认它红**

Run: `python dev/client/tests/test_v023.py`
Expected: FAIL —— 条数或逐条比对不通过。

- [ ] **Step 3: 对齐默认档案**

固件 `k_binds_chromecast[]` 应该是（**去掉语音键**、保留返回键长按）：

```c
static const br_default_bind_t k_binds_chromecast[] = {
    { 8739u, 0, 1, 0x08, 1, { 0x07 } },     /* 主页 → Win+D 显示桌面 */
    { 8258u, 0, 1, 0x00, 1, { 0x52 } },     /* 上 → ↑ */
    { 8260u, 0, 1, 0x00, 1, { 0x50 } },     /* 左 → ← */
    { 8261u, 0, 1, 0x00, 1, { 0x4F } },     /* 右 → → */
    { 8259u, 0, 1, 0x00, 1, { 0x51 } },     /* 下 → ↓ */
    { 8425u, 0, 2, 0x00, 1, { 0xE9 } },     /* 音量+ → 消费者音量+ */
    { 8426u, 0, 2, 0x00, 1, { 0xEA } },     /* 音量- → 消费者音量- */
    { 8418u, 0, 2, 0x00, 1, { 0xE2 } },     /* 静音 → 消费者静音 */
    { 8311u, 0, 1, 0x04, 1, { 0x2B } },     /* YouTube → Alt+Tab 应用切换 */
    { 8606u, 0, 1, 0x00, 1, { 0x29 } },     /* 电源 → Esc */
    { 8585u, 0, 1, 0x00, 1, { 0x28 } },     /* 输入源 → Enter */
    { 8257u, 0, 1, 0x00, 1, { 0x28 } },     /* 确定 → Enter */
    { 8740u, 0, 1, 0x00, 1, { 0x2A } },     /* 返回 → 退格 */
    { 8740u, 2, 1, 0x00, 1, { 0x2A } },     /* 返回 长按 → 退格（连着删） */
};
```

PC 侧 `remote_layout.py`：`DEFAULT_ACTIONS` 里去掉语音相关（本来就没有），
`DEFAULT_LONG_ACTIONS` 保留 `{"back": "退格"}`；**新增** `DEFAULT_FIRMWARE_ACTIONS`
（只含固件能下发的那些，PC 端铺默认时用它，保证与固件逐条一致）。

- [ ] **Step 4: 默认档案按「型号 + MAC」分档，记录在板子上**

用户要求：默认档案不只按型号，还要**按遥控器型号 + MAC 地址**分，
而且**这个归属要记录在板子上**（这样同一台重配时还用回同一套默认）。

改法（`br_ble_core.c` + `br_mapping.c`）：

1. `pl_entry_t`（配对表条目）新增一个字段 `profile`（默认档案 id 的字符串）：
   ```c
   char profile[16];      /* 命中的默认档案 id（如 "chromecast"），空串=没匹配 */
   ```
   配对成功时 `pl_put()` 把它一起落进 NVS（`e<idx>` 那条记录后面加一个字段）。
2. `br_mapping_apply_defaults(addr, name)` 改成三参：
   ```c
   int br_mapping_apply_defaults(const char *addr, const char *name,
                                 const char *profile_in);
   ```
   - `profile_in` 非空 → 直接用它（**同一台重配时用回原来的档案**）；
   - 否则按名字匹配 `k_default_profiles[]`，匹配上就把 id 写回配对表。
3. 重配同一台时：`pl_put()` 按 identity 找回原槽位 → 原槽位里的 `profile` 还在
   → 铺回同一套默认 ✓「同一台 = 同一套默认」。

- [ ] **Step 5: 确认「只在配对成功那一刻铺」**

`br_ble_core.c` 的 CONNECT 分支里 `queue_defaults(idx, p->name)` 已经在；
`br_mapping_apply_defaults()` 里 `br_mapping_count(addr) > 0` 就返回（用户配过就不覆盖）✓。
解绑时 `br_ble_unpair()` 已经调 `br_mapping_clear(addr)` ✓ → 重配时板子是空的 → 自动铺默认 ✓。

- [ ] **Step 5: 跑闸门，确认它绿**

Run: `python dev/client/tests/test_v023.py`
Expected: 全部通过。

---

## Task 3: PC 端同步简化（单向自动同步）

**Files:**
- Modify: `dev/client/src/core/controller.py`
- Modify: `dev/client/src/ui/qml/ButtonsPage.qml`
- Modify: `dev/client/src/ui/qml/DevicePage.qml`
- Delete: `dev/client/src/ui/qml/MappingCompareDialog.qml`
- Modify: `dev/client/src/ui/qml/main.qml`（删掉冲突弹窗）
- Test: `dev/client/tests/test_v023.py`（追加）

**Interfaces:**
- Consumes: `_save_bindings(write_archive=True, push=True)` → `_push_bindings_to_firmware()`（已有）
- Produces: `Bridge.exportMappingFileTo(path)` / `Bridge.importMappingFile(path)`（已有，按键页直接用）

- [ ] **Step 1: 写失败的闸门**

```python
def test_sync_is_simple_and_automatic() -> None:
    """改映射就自动同步；不做比对、不弹冲突。"""
    src = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("没有冲突弹窗了", "mappingConflict" not in src, "")
    check("没有映射比对视图了", "boardMappingView" not in src, "")
    check("改映射仍然立刻下发", "_push_bindings_to_firmware" in src, "")

    btn = (QML / "ButtonsPage.qml").read_text(encoding="utf-8")
    check("按键页没有「映射比对」入口", "映射比对" not in btn, "")
    check("按键页有导入/导出入口",
          "exportMappingFileTo" in btn or "exportMappingFile" in btn, "")

    dev = (QML / "DevicePage.qml").read_text(encoding="utf-8")
    check("设备页不再做条数比对", "板子 " not in dev or "bindsBoard" not in dev, "")
    check("设备页不再有行内「同步」按钮", "syncMappingToBoard" not in dev, "")

    check("映射比对弹窗文件已删",
          not (QML / "MappingCompareDialog.qml").is_file(), "")
```

- [ ] **Step 2: 跑闸门，确认它红**

Expected: FAIL（当前代码里这些都还在）。

- [ ] **Step 3: 改 controller.py**

- 删掉 `mappingConflict` 信号、`_apply_board_mapping()` 里的冲突分支、
  `resolveMappingConflict()`、`boardMappingView()`、`mappingSyncState()`、
  `applyMappingDirection()`、`_board_snapshot` / `_board_snapshot_addr`、
  `_board_extra_entries()`。
- `_apply_board_mapping()` 简化为**只读不覆盖**：

```python
    def _apply_board_mapping(self, result, addr: str) -> None:
        """读回开发板上的映射 —— 只用来显示，**不覆盖本机**。

        ★ 本机是唯一真值 ★
          板子是镜像：PC 改了就整份推过去。
          所以这里读回来的东西只在「本机还是空的」时采纳一次，
          之后永远以本机为准，不再比对、不再弹窗。
        """
        if not self._collecting_map:
            return
        self._collecting_map = False
        count = result.get_int("count")
        if count <= 0:
            # 板子上是空的（刚配对 / 重配 / 刚刷过固件）
            local = self._remote_bindings(self._current)
            if local:
                self._bindings = local
                self._save_bindings(write_archive=True, push=True)
                self._load_mappings()
                self._set_status("开发板上没有映射，已把本机配置下发过去")
            else:
                self._bindings = self._default_bindings_for_current()
                self._save_bindings(write_archive=True, push=True)
                self._load_mappings()
                self._set_status("已下发内置默认映射")
            return
        # 板子上有：本机空 → 采纳一次；本机不空 → **不动**（PC 是主）
        if not self._remote_bindings(self._current):
            self._bindings = self._bindings_from_board(dict(self._board_mapping))
            self._save_bindings(write_archive=True, push=False)
            self._load_mappings()
            self._set_status(f"已从开发板读回 {count} 条映射")
```

- [ ] **Step 4: 改 QML**

- `DevicePage.qml`：遥控器行的描述只留地址（去掉条数比对）；
  删掉「同步」按钮（`syncMappingButton`）与 `binds` / `bindsBoard` 两个 role 的使用；
  `actionColumnWidth` 从 288 改回 184（只剩「重命名」「删除」两个按钮）。
- `ButtonsPage.qml`：底部操作条把「映射比对」换成「导出配置」「导入配置」两个按钮
  （直接调 `Bridge.exportMappingFileTo(url)` / `Bridge.importMappingFile(url)`，
  配 `FileDialog` 选路径）；删掉 `MappingCompareDialog` 的实例化。
- `main.qml`：删掉 `mappingConflictDialog` 与它的 `Connections`。
- 删除 `dev/client/src/ui/qml/MappingCompareDialog.qml`。

- [ ] **Step 5: 导入导出按参考项目的规格收紧**

参考项目 `mapping_presets.py` 的校验规则直接搬（它是现成的、够用）：

```python
# 导出的文档（格式与参考项目一致）
{
  "format": "RemoteMic.key-mapping",
  "schema_version": 1,
  "name": "<配置名>",
  "profile": "<遥控器型号 id>",
  "mapping": {
    "bindings": {...}, "secondary_bindings": {...}, "display_notes": {...}
  }
}
```

导入时的校验（`controller.py` 的 `importMappingFile`）：
1. 根字段集必须正好是 `{format, schema_version, name, profile, mapping}`，
   `format` 必须是 `"RemoteMic.key-mapping"`，`schema_version` 必须是整数 1
   → 否则报「请选择本软件导出的按键配置文件（版本 1）。」；
2. `profile` 必须等于当前遥控器的型号 → 否则报「配置的遥控器型号与当前型号不同，请先切换设备。」；
3. `mapping` 的三个字段集必须正好是 `{bindings, secondary_bindings, display_notes}`；
4. 里面的键必须**都属于当前型号**（`remote_layout.BUTTON_ORDER`）
   → 多余的键报「配置包含不属于此型号的按键。」；
5. **必须包含该型号的全部按键** → 少一个报「配置必须包含此型号的全部按键。」；
6. 每条备注 ≤256 字。

★ 校验失败时**绝不能改动本机配置**（参考项目的 fail-safe）：
报错 + 原样保留 + 状态栏说清哪一条不对。

- [ ] **Step 6: 跑闸门，确认它绿**

Run: `python dev/client/tests/test_v023.py` → 全绿；
再跑 `python dev/client/tests/test_ui_render.py` → 零 QML 警告。

---

## Task 4: 保留 v0.21.1 的其它改进（只做回归确认，不改代码）

这些已经在 v0.21.0/0.21.1 做完并验证过，**回退固件没有动它们的源码**，本任务只确认没被回退带走：

- 遥控器名字列加宽（`DevicePage.qml` 的 `titleWidth: 180`）
- 按键页遥控器图片 `mipmap: true` + `MultiEffect` 投影
- 编辑下拉框抄参考项目（`EditorActionCombo.qml`：分类字号、`ListView.view.width`、显式 popup）
- 应用图标（`assets/icons/app.svg` + `app.ico` + `app.py` / `tray.py` / `package.py --icon`）
- 刷写信息框平时折叠
- 映射快照按 `(键, 触发)` 存（多手势不丢）
- 固件地址字节序统一（`br_addr_to_str`）
- 语音日志说实话（事件带 `sent` / `on`）

- [ ] **Step 1: 写回归闸门（静态检查，逐条点名）**

在 `test_v023.py` 追加：

```python
def test_kept_features_still_there() -> None:
    dev = (QML / "DevicePage.qml").read_text(encoding="utf-8")
    check("名字列仍加宽", "titleWidth: 180" in dev, "")
    btn = (QML / "ButtonsPage.qml").read_text(encoding="utf-8")
    check("图片仍开 mipmap", "mipmap: true" in btn, "")
    check("图片仍有投影", "MultiEffect" in btn, "")
    combo = (QML / "EditorActionCombo.qml").read_text(encoding="utf-8")
    check("下拉框仍钉住 popup", "popup: Popup" in combo, "")
    check("图标还在", (CLIENT / "src" / "assets" / "icons" / "app.ico").is_file(), "")
    check("刷写框仍默认折叠", "flashLogExpanded: false" in dev, "")
    core = (CLIENT / "src" / "core" / "controller.py").read_text(encoding="utf-8")
    check("快照仍按 (键,触发) 存",
          "self._board_mapping[(key_id, TRIGGER_INDEX[trigger])]" in core, "")
    fw = (FW_MAIN / "br_mapping.c").read_text(encoding="utf-8")
    check("固件地址统一用 br_addr_to_str", "br_addr_to_str(addr" in fw, "")
```

- [ ] **Step 2: 跑闸门，确认它绿**（这些应当已经满足）

Expected: 全绿。若某条红，说明回退时丢了东西，按提示补回。

---

## Task 5: 版本、打包与验收

**Files:**
- Modify: `dev/firmware/bridge/main/app_main.c`（`FW_VERSION` → `"0.22.0"`）
- Modify: `dev/client/src/core/version.py`（`VERSION = "0.22.0"`）
- Modify: `dev/client/build/package.py`（闸门列表加 `tests/test_v023.py`）
- Modify: `dev/client/tests/test_v017.py` / `test_v020.py` / `test_v021.py` / `test_v022.py`（版本断言同步到 0.22.0）

- [ ] **Step 1: 升版本号并同步所有版本断言**

```bash
sed -i 's/#define FW_VERSION "0.21.1"/#define FW_VERSION "0.22.0"/' dev/firmware/bridge/main/app_main.c
sed -i 's/VERSION = "0.21.1"/VERSION = "0.22.0"/' dev/client/src/core/version.py
sed -i 's/0\.21\.1/0.22.0/g' dev/client/tests/test_v017.py dev/client/tests/test_v020.py \
                                  dev/client/tests/test_v021.py dev/client/tests/test_v022.py
```

- [ ] **Step 2: 把新闸门加进打包闸门列表**

`dev/client/build/package.py` 的 `run_checks()` 里加 `"tests/test_v023.py"`。

- [ ] **Step 3: 全量闸门**

```bash
cd dev/client/tests
for t in test_*.py; do python "$t" || exit 1; done
```
Expected: 全部退出码 0。（跑之前先关掉正在运行的客户端，它占着 COM12。）

- [ ] **Step 4: 写 changes.md 并打包**

```bash
python dev/client/build/package.py --version 0.22.0 --notes-file build/notes-0.22.0.txt
```

`changes.md` 必须写清这 8 条为什么改（尤其第 1 条：板子按死 PC 键盘的根因与兜底）。

- [ ] **Step 5: 校验和自检**

```bash
cd releases/0.22.0
find ESP32S3RemoteBridge -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS.txt
sha256sum -c SHA256SUMS.txt
```
Expected: 全部 `OK`，退出码 0；文件是纯 LF（`grep -c $'\r' SHA256SUMS.txt` 为 0）。

- [ ] **Step 6: 真机验收（用户配合）**

1. **插上板子，PC 的键盘必须立刻正常**（用户报的原始症状）。
2. 遥控器自动连上后，按各个键 → PC 键盘始终正常。
3. 设备页：一行只显示名字和地址，没有条数比对、没有「同步」按钮。
4. 按键页改一个映射 → **不用点任何按钮**，板子立刻跟着变（拔掉 PC 也能用）。
5. 删掉一台遥控器 → 重新配对 → 板子自动铺 14 条默认（按键页显示得到）。
6. 按键页的「导出配置 / 导入配置」能往返。
7. 语音键：按下去，输入法真的启动；日志显示「板子替电脑按出 左Ctrl+左Win」。

---

## 风险与回退

| 风险 | 兜底 |
|---|---|
| 看门狗把合法的长按打断了 | 超时给 3 秒（长按连删最多 3 秒），可接受；用户反馈需要再调 |
| 去掉语音键那条后，某些遥控器的语音键不触发 | 语音热键走 ATVV 通道，与该映射无关（0.18.0 无默认映射时语音正常可证） |
| 新固件仍把键盘按死 | 立刻回退到 `releases/0.18.0/ESP32S3RemoteBridge/_internal/firmware/remote_bridge.bin`（用户已确认这版可用） |
| 简化后丢掉用户依赖的功能 | 0.18.0~0.21.1 的产物全部保留在 `releases/`，随时可回退 |
