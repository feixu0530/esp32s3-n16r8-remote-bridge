# 小米蓝牙语音遥控器一代 —— 真机取证记录

> 取证日期：2026-11-24
> 板子：ESP32-S3 N16R8（固件带取证开关：放开小米名字识别 + 数据通道订阅 + 原始包日志）
> 遥控器：小米蓝牙语音遥控器一代，广播名 **"MI RC"**（Windows 侧另见 **"小米蓝牙语音遥控器"**）
> 抓包脚本：`dev/client/tools/xiaomi_probe.py`
> 原始日志：`dev/client/temp/xiaomi-capture-*.log`（协议侧 + 固件串口侧合并）

---

## 一、身份与服务清单

`DT PEER idx=0 addr=44:28:AA:BB:CC:01 conn=1 disc=7 svcs=7 chrs=58 atvv=0 enc=1 sub=1`

| 服务 | 句柄范围 | 说明 |
|---|---|---|
| 0x1801 | 1..4 | Generic Attribute |
| 0x1800 | 5..13 | Generic Access（名字/外观/…） |
| 0x180F | 14..17 | Battery（0x2A19） |
| 0x180A | 18..36 | Device Information |
| **0x1812** | 37..136 | **HID**（0x2A4B Report Map / 0x2A4D Report / 0x2A4C 控制点 / 0x2A22 引导键盘 / 0x2A4A / 0x2A4E） |
| `0000D1FF-3C17-D293-8E48-14FE2E4DA212` | 137..142 | 厂商服务 |
| `0000D0FF-3C17-D293-8E48-14FE2E4DA212` | 143.. | 厂商服务 |

**★ 关键结论：没有 ATVV（`ab5e0001`）服务（`atvv=0`）★**
→ 语音**不在** ATVV 通道上，和参考项目 `mac20777/vibecoding-voice` 的实测一致：
语音音频装在 HID 报告里。厂商服务 D0FF/D1FF 存在，但参考项目实测它们**不承载**语音流。

### 1.1 Device Information（句柄 20..34 实读）

| 属性 | 值 |
|---|---|
| Manufacturer Name | Xiaomi |
| Model Number | Model XiaomiMi |
| Serial Number | 3.0.0 |
| Hardware Revision | 1.0.2.64.21 |
| Firmware Revision | 1.0.2 |
| Software Revision | 3.0.1.8 |
| Battery | 48% |

这组身份字符串没有直接出现 `ARN9` 或 `RC003`；代际判断依据是用户确认的实物外形与
`codex-wand` 的 ARN9 图片，而不是把 Device Information 当成型号证明。

Windows 和 Edge Web Bluetooth 都能读到上述身份信息；但这不能证明它们能访问蓝牙 HID 服务。

## 二、Report Map（147 字节，原文）

```
000: 05 01 09 06 A1 01 85 01 05 07 19 E0 29 E7 15 00
010: 25 01 75 01 95 08 81 02 95 01 75 08 81 01 95 05
020: 75 01 05 08 19 01 29 05 91 02 95 01 75 03 91 01
030: 95 06 75 08 15 28 25 FE 05 07 19 28 29 FE 81 00
040: 05 0C 09 01 A1 01 85 F1 15 00 25 01 75 01 95 18
050: 09 B5 09 B6 09 B7 09 CD 09 E2 09 E5 09 E7 09 E9
060: 09 EA 0A 52 01 0A 53 01 0A 54 01 0A 55 01 0A 83
070: 01 0A 8A 01 0A 92 01 0A 94 01 0A 21 02 0A 23 02
080: 0A 24 02 0A 25 02 0A 26 02 0A 27 02 0A 2A 02 81
090: 02 C0 C0
```

解读：

* **Report ID 0x01** = 键盘页（Usage Page 0x07）
  `8 bit 修饰键 + 8 bit 保留 + 6 × 8 bit 键码` → 8 字节
* **Report ID 0xF1** = 消费者页（Usage Page 0x0C）
  24 bit 标志（B5/B6/B7/CD/E2/E5/E7/E9/EA）+ 8 个 1-bit 项（0x152/153/154/155/183/18A/192/194）
  + 7 个 2-byte 项（0x221/223/224/225/226/227/22A）
* 其余 Report ID（2..10、0xE0..0xE4）在 Map 里没有声明，
  但设备上有对应的特征（Report Reference 类型多为 Feature）。

## 三、按键报告（**键盘页**，handle 52）

8 字节标准键盘报告：`byte0=修饰键  byte1=保留  byte2..7=键码`，**不带 report id 字节**。

抓到的原始包与解析结果：

| 原始 hex | 解析 | 键 |
|---|---|---|
| `00 00 4A 00 00 00 00 00` | usage 0x4A | 主页 |
| `00 00 52 00 00 00 00 00` | usage 0x52 | 上 |
| `00 00 51 00 00 00 00 00` | usage 0x51 | 下 |
| `00 00 50 00 00 00 00 00` | usage 0x50 | 左 |
| `00 00 4F 00 00 00 00 00` | usage 0x4F | 右 |
| `00 00 28 00 00 00 00 00` | usage 0x28 | 确定 |
| `00 00 F1 00 00 00 00 00` | usage 0xF1 | 返回 |
| `00 00 65 00 00 00 00 00` | usage 0x65 | 菜单 |
| `00 00 80 00 00 00 00 00` | usage 0x80 | 音量+ |
| `00 00 81 00 00 00 00 00` | usage 0x81 | 音量− |
| `00 00 66 00 00 00 00 00` | usage 0x66 | 电源 |
| `00 00 3E 00 00 00 00 00` | usage 0x3E | 语音 |

**这张表与参考项目 `BUTTON_CODES` 完全一致**（互为交叉验证）。

⇒ 结论：

* 11 个可配键 + 1 个语音键（语音键走语音通道，不进映射表）
* key_id = `(BR_SRC_KEYBOARD << 12) | usage` → `"1:<usage>"`，
  与谷歌遥控器的消费者页 `"2:<usage>"` **物理隔离**
* usage 最大 0xF1（241）< 0x1000，**12 位 usage 够用**，不用改协议

## 四、圆环（触控环）

* 绕圈滑动只产生 上/下/左/右 四个离散 usage（0x52/0x51/0x50/0x4F），
  **没有滚轮语义**（消费者页 0x8A 等）→ 圆环就是十字键，不做「滑动=滚轮」。
* 圆心的圆盘是「确定」键（0x28）。

## 五、语音通道：非 ATVV，mSBC 仍待干净采样确认

### 5.1 已证实的事实

| 句柄 | 特征 | 包长 | 内容 | 含义 |
|---|---|---|---|---|
| 66 | 2A4D（Report Reference 类型 3） | 20 字节 | `01 00 00 …` / `00 00 00 …` | 与参考项目一致的语音开始 / 结束控制包 |
| 52 | 2A4D（键盘页） | 8 字节 | `00 00 3E 00 …` | 语音键的键码（0x3E） |
| 139 | `A001`（厂商服务 D1FF，可写+可通知） | 16 字节 | 旧采样中为全 0 | 尚不能据此判定设备能力 |

本机服务发现结果为 `atvv=0`，没有暴露 ATVV 服务 `AB5E0001`。
因此本机当前连接暴露的是**非 ATVV 的 HID 协议族**，不能直接套用谷歌遥控器的 ATVV + IMA ADPCM 路径。

### 5.2 旧采样为什么不能证明“没有音频”

旧探针在 `EV CONN` 后立即并发发送 `DUMP`、`HIDDATA`、`HIDREAD`，
与固件服务发现、加密和 HID 订阅争夺 GATT 过程槽。日志中反复出现：

- `svc discovery failed: 10`
- `EV HID ... total=0 reports=0`
- 先报告 `svcs=6 chrs=62`，随后报告表又被清零
- COM6 固件日志与 COM12 协议日志交错拼接

这些会话中未捕获到 60 字节包，只能说明**旧采样没有拿到音频通知**，
不能推出遥控器没有麦克风或不会下发音频。MTU 已实测协商到 247，
但 MTU 正常也不能替代通知链路的干净验证。

### 5.3 与参考项目的交叉验证

`mac20777/vibecoding-voice` 对同名设备、同 VID/PID `0x2717:0x32B8` 的实测记录显示：

- 20 字节 `01/00` 控制包；
- 三个轮换 HID 句柄上的 60 字节音频包；
- `byte0=0x01`、`byte1` 为 `08→38→C8→F8`、`byte2=0xAD`；
- bytes 2..58 为 57 字节 mSBC，单声道 16 kHz。

这与旧采样中的 20 字节控制包一致，因此 HID/mSBC 是必须验证的候选路径。
参考项目的具体句柄不能硬编码，必须在本机重新发现并订阅。

对参考项目附带的 `vibecoding-xiaomi-voice.pcap` 做了双向 ATT 解析：8.13 秒内共有
914 条通知，其中 60 字节音频包分别出现在 `0x0029`（307 条）、`0x002D`（301 条）、
`0x0031`（301 条），控制包出现在 `0x0025`（2 条）。**录制期间主机侧 ATT 写入为 0 条**。
因此该参考会话是在遥控器发出控制包后被动接收音频，不能据此臆造 Output Report、
`A001` 或 `A002` 激活写入。

### 5.4 代际判断

本机型号为 **ARN9，即小米蓝牙语音遥控器一代**。参考项目
`jinsihou19/codex-wand` 的 `Resources/ARN9-remote-photo.png` 与用户提供的实物图一致；
`PlainWorkshop/MiRemote` 也描述同一款黑色十二键外形。

需要区分“硬件代际”和“固件协议族”：同外观、同 VID/PID 的设备可能存在不同固件实现。
本机当前连接明确没有 ATVV，因此只能按本机实测走非 ATVV 的 HID 协议族，
不能因为其他设备在 macOS 上使用 ATVV 就直接套用该路径。

### 5.5 干净复采与句柄映射

修复后的 `xiaomi_probe.py` 默认被动等待 `GATTDONE`、`EV ENC status=0 enc=1`、
`EV HID phase=done` 全部完成，之后只发一次只读 `HIDREPORT`；
不再自动发送 `DUMP`、`HIDDATA`、`HIDREAD`、`HIDWRITE`。

干净会话 `xiaomi-clean-passive.log` 中：

- 服务发现完成：`svcs=7 chrs=58 atvv=0`；
- 加密成功：`EV ENC ... status=0 ... enc=1`；
- HID 订阅完成：`phase=done reports=28 ok=10`；
- 三路候选音频特征的 CCCD 均回读为 `0x0001`：
  值句柄 70/74/78，CCCD 句柄 71/75/79；
- 语音键确实产生 handle 66 的 20 字节 `01/00` 控制包和 handle 52 的语音键码；
- 该次会话没有收到 handle 70/74/78 的 60 字节通知。

参考项目与本机句柄的对应关系是固定偏移 **+0x1D**：

| 用途 | 参考项目 | 本机 |
|---|---|---|
| 语音控制 | 0x0025 | 0x0042（66） |
| 音频轮换通道 | 0x0029 / 0x002D / 0x0031 | 0x0046 / 0x004A / 0x004E（70/74/78） |
| 键盘报告 | 0x0017 | 0x0034（52） |

### 5.6 HID Control Point 写操作修正

本机 HID Control Point（handle 41）属性为 `0x04`，只支持 Write Without Response。
旧诊断函数统一发送 ATT Write Request，真机返回：

```text
PROBE 写 handle=41 len=1 rc=0
PROBE 写 handle=41 status=259
```

`rc=0` 只表示 NimBLE 接受了本地请求，`status=259` 表示对端拒绝了 ATT 操作。
因此旧会话不能证明 Exit Suspend 是否有效。现已按发现到的特征属性选择
`ble_gattc_write_no_rsp_flat()`，并将自动初始化的 Exit Suspend 值修正为规范值 `0x01`。
修正后的语音复采日志为 `xiaomi-write-command-voice.log`：

```text
DT CHR ... uuid=2A4C ... props=4 handle=41
EV ENC idx=0 status=0 ... enc=1
EV HID idx=0 phase=done reports=28 ok=10
PROBE 写 handle=41 len=1 no_rsp=1 rc=0
EV HIDRAW idx=0 handle=66 n=20 hex=0100000000000000000000000000000000000000
EV HIDRAW idx=0 handle=52 n=8 hex=00003E0000000000
EV HIDRAW idx=0 handle=66 n=20 hex=0000000000000000000000000000000000000000
```

该次 Write Command 没有出现 `status=259`，证明修正后的 ATT 操作已被对端接受；
但语音按下到松开期间仍未出现 handle 70/74/78 的 60 字节通知。
因此问题已从“错误 ATT 写类型”推进到“激活后遥控器没有向这三路候选特征发送音频”。

### 5.7 低延迟连接参数与护栏复采

`xiaomi-guard-recapture-20261124.log` 是一次完整干净复采：

- 23:48:42 连接后实际协商到 `itvl=7.50ms lat=0 to=5000ms`；
- 23:48:44 HID 订阅完成：`reports=28 ok=10`，三路候选音频 CCCD 均回读 `0x0001`；
- 23:48:52 遥控器把参数改成 `lat=299 to=12000ms`，固件立即识别并在
  23:48:55 恢复请求 `itvl=6 lat=0 to=500`，随后回到 `7.50ms / 0 / 5000ms`；
- 23:49:16 语音按下只有 handle 66 的 20 字节开始包和 handle 52 的 `00 00 3E ...`；
- 23:49:21 松开只有 handle 66 的 20 字节结束包和 handle 52 的全零键盘报告；
- 按住 5 秒期间仍没有 handle 70/74/78 的 60 字节音频通知。

同一次日志还暴露了一个独立的解析缺陷：语音结束包的 `usage=0x3E` 抬起之前，
多出了一条 `usage=0xE1`（左 Shift）抬起。`0xE1` 正是 20 字节语音控制 Feature 报告
首字节 `0x01` 被当作键盘修饰键位的结果。现已在 `br_hid_on_notify()` 中按
Report Reference 类型封住边界：小米模式下只有 `report_type=1`（Input）允许进入
按键差分，Feature/Output/未知类型只保留原始取证，不产生按键事件。
`tests/test_xiaomi.py` 增加了对应常驻闸门。

这次修复只消除假按键，**不会让尚未出现的音频通知凭空产生**；语音音频缺口仍需
设备侧证据继续定位。

### 5.8 Feature 报告守卫的真机复采（2026-11-25）

刷入带 `report_type != 1` 守卫的固件后，重新用 `xiaomi-feature-guard-20261125.log`
完成一次干净采样。连接后 `fw=2.1.0`、`atvv=0`，协商到 `itvl=7.50ms lat=0 to=5000ms`，
HID 订阅为 `reports=28 ok=10`，三路候选音频 CCCD 71/75/79 均回读 `0x0001`。

语音按住 5 秒期间，板子只收到：

```text
EV HIDRAW idx=0 handle=66 n=20 hex=0100000000000000000000000000000000000000
EV HIDRAW idx=0 handle=52 n=8 hex=00003E0000000000
EV KEY idx=0 src=1 usage=62 state=down addr=44:28:AA:BB:CC:01
```

松开时只收到：

```text
EV HIDRAW idx=0 handle=66 n=20 hex=0000000000000000000000000000000000000000
EV HIDRAW idx=0 handle=52 n=8 hex=0000000000000000
EV KEY idx=0 src=1 usage=62 state=up addr=44:28:AA:BB:CC:01
```

**修复已在真机验证：不再出现 `usage=225`（`0xE1`）假左 Shift。**
但语音按住期间仍没有 handle 70/74/78 的 60 字节通知；这只说明本次 ESP32 主机侧
没有收到音频，不能说明遥控器没有音频。`test_xiaomi.py` 的 Feature 报告守卫常驻闸门通过。

下一步取证是记录 `BLE_GAP_EVENT_DATA_LEN_CHG` 的实际 `rx_octets/rx_time`。
60 字节 ATT 通知要求主机 Rx LL payload 至少 67 字节，MTU 247 不能替代 DLE 证明。
固件已补充 `EV DLE` 协议日志与串口日志；这仍是被动诊断，不增加任何厂商特征写入。

### 5.9 Edge Web Bluetooth 的限制

Edge 直连能读取 Device Information，并枚举到 D0FF/D1FF 服务，但 Chromium 明确禁止网页访问
蓝牙 HID 服务 `0x1812`：

```text
SecurityError: getCharacteristic(s) called with blocklisted UUID
SecurityError: Origin is not allowed to access the service.
```

所以 Edge 页面不能替代 Windows 原生 HID 栈，也不能作为本项目的音频取证通道。
参考项目实际使用 Windows 原生 HID 连接加 USBPcap 被动观察 ATT 通知，而不是 Web Bluetooth。

### 5.10 标准 DLE 请求与复采结果（2026-11-25）

被动监听会话 `xiaomi-dle-20261125.log` 没有出现 `DATA_LEN_CHG`。为排除默认
27 字节 LL payload 的影响，固件仅对小米连接调用标准 `ble_gap_set_data_len()`，
请求 `tx_octets=251 / tx_time=2120`，并记录 `EV DLEREQ` 与 `EV DLE`。
这不是 Output Report，也不是 D0FF/D1FF 厂商特征写入。

会话 `xiaomi-dle-request2-20261125.log` 确认：

```text
EV DLE idx=0 tx_octets=251 tx_time=2120 rx_octets=251 rx_time=2120
EV DLEREQ idx=0 rc=0
FW DLE handle=3 tx=251/2120us rx=251/2120us
FW HID 设置完成：28 个报告（成功 10 失败 0）
```

因此主机侧实际 Rx LL payload 为 251 字节，足够承载 60 字节 ATT 通知；
DLE/MTU 不足以解释音频缺口。随后两次语音按住 5 秒，仍只收到 handle 66 的
20 字节控制包和 handle 52 的键盘报告，没有 handle 70/74/78 的 60 字节通知。
Feature 报告守卫继续有效，未出现 `usage=0xE1` 假按键。

当前证据把缺口收敛到：**遥控器没有向这次 ESP32 连接发送候选音频通知**。
不能据此说设备没有音频；参考项目的 Windows 原生 HID 会话能收到同类包，
仍需 Windows/USBPcap 同机对照判断是否是主机连接/初始化差异。
在取得本机真实音频帧或可信等效激活证据前，不实现 mSBC 解码。

### 5.11 v2.2.0 语音键热键桥接（2026-11-25）

v2.1.0 的真机日志已确认业务路由错误：语音键的键盘页 `0x3E` 被当成普通 F5，
产生 `EV KEY src=1 usage=62 state=down`，随后因默认表无绑定产生
`MAPMISS key=4158`；同一操作还产生 handle 66 的 20 字节 `01/00` 控制包。
因此 F5 只是标准键名转换正确，问题在于没有把该键接入语音热键。

v2.2.0 的修复边界是：

- 在 HIDRAW 之后、Feature 报告守卫之前识别小米 20 字节 `01/00` 控制包；
- 以 20 字节控制报告和键盘 `0x3E` 两路信号做 80 ms 释放宽限去重；
- 小米 `0x3E` 在学习、映射和 `EV KEY` 之前被消费，不再输出 F5/MAPMISS；
- 复用现有 PC 语音热键，physical-hold 不受 ATVV 的 1.5 秒无音频流分支影响；
- 热键最长按住 60 秒，禁用或所属 peer 断链时释放；
- 保留所有 60 字节候选报告的 HIDRAW，不实现 mSBC 解码，不增加厂商特征写入。

验收时必须同时保留板端 `EV HIDRAW` / `EV VOICE` 与 PC 输入法实际收音证据。
热键验收使用 **PC 本机麦克风**；即使热键桥接通过，板子 UAC1 上的小米遥控器
音频仍应继续标为“尚未捕获 60 字节音频”，不能写成“设备没有音频”。

### 5.13 Windows 原生连接参数（2026-11-25）

把同一只遥控器配对到 Windows 后，注册表
`HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices\4428a3b7beb6`
记录了 Windows 实际使用的连接参数：

```text
LERemoteConnParamsIntervalMin = 0x00000006   (7.5 ms)
LERemoteConnParamsIntervalMax = 0x00000006   (7.5 ms)
LERemoteConnParamsLatency     = 0x0000012b   (299)
LERemoteConnParamsLSTO        = 0x000004b0   (12000 ms)
```

Windows 的 HID 子设备和音频语音路径都在这组参数下工作。此前 ESP32 固件在小米
HID 打开后强行请求 `7.5 ms / latency=0 / timeout=5000 ms`，虽然按键和 20 字节
控制包正常，但 60 字节音频始终不出现。2.2.0 开发固件已改为与 Windows 一致的
`7.5 ms / latency=299 / timeout=12000 ms` 并完成真机复测：连接参数确认回到
`7.50 ms / 299 / 12000 ms`，语音按下仍只有 20 字节 `01/00`、键盘 `0x3E`，
**仍无 60 字节音频帧**。因此连接参数不是根因，下一步必须抓取 Windows 从
HID 打开到首次语音的完整 ATT 初始化。

### 5.12 当前交付状态

- v2.2.0 的代码交付范围是“小米语音键触发现有 PC 语音热键”；
- 真机验收必须看到 `EV VOICE sent=1 src=xiaomi_control`、PC 输入法进入收音、
  无 `EV KEY usage=62` / `MAPMISS 4158`，且松开后没有卡住的修饰键；
- 截至本节记录时，本机仍未取得真实 60 字节 mSBC 帧或可信等效激活证据；
- 因此小米遥控器音频解码/UAC1 收音仍未完成，不实现分支 B/mSBC 解码。

### 5.14 PID 32BA 的 Windows 全量会话（2026-11-25）

用 USBPcap 抓取 Windows 从配对到语音按下的完整 90 秒会话，按 BLE 连接拆分后：

```text
conn=2051（本机小米遥控器 44:28:AA:BB:CC:01）
  handle=66  n=20  count=2   ← 语音控制开始/结束
  handle=52  n=8   count=2   ← 键盘 0x3E 按下/松开
  其它音频通知：0
conn=2048（另一台 BLE 设备，不是这只遥控器）
  handle=40  n=7   count=3654
  handle=44  n=19  count=114
```

Windows 设备树显示这只遥控器是 **PID `0x32BA`**，而参考项目验证的是
PID `0x32B8`。社区针对 Realtek 同族遥控器的实现（`dikeckaan/xiaomi-rc-bridge`）
明确记录：

- 厂商服务 `D1FF/A001` 只通知、`A002` 只写；
- 语音键通过 Report ID 5 请求语音会话，但**小米/Realtek 的开麦命令未公开**；
- “microphone does not work yet”。

本机向 A002 写入 ATVV 风格的 `0C 00 02` 后，遥控器立即主动断链
（`HCI=0x13`），说明该命令不适用于这只固件。因此当前没有可验证的音频入口；
在取得小米电视侧的厂商命令或换成 PID `32B8` 同族遥控器前，不能宣称
遥控器麦克风已接入。

### 5.15 小米电视系统蓝牙栈里的开麦命令（2026-11-25）

TV：MiTV4A / Android 6.0.1，IP 192.168.2.209，ADB 可用但无 root，
`/data/misc/bluetooth/logs` 不可读。改为静态分析电视的系统蓝牙 APK：

```text
/system/app/Bluetooth/Bluetooth.apk
  com.xiaomi.mitv.settings.bluetooth.BleRemoteControllerService
  com.xiaomi.mitv.settings.bluetooth.BleRemoteControllerService$AudioRCGattCallback
```

关键常量与逻辑（反汇编逐一核对）：

```text
HID_STREAM_SYSTEM_KEY_INSTANCE   = 0   (Report ID 1，键盘报告；value[2]==0x3E 为语音键)
HID_STREAM_ENABLE_WRITE_INSTANCE = 3   (Report ID 4，可写、不通知 -> 开麦开关)
HID_STREAM_ENABLE_READ_INSTANCE  = 4   (Report ID 5，20 字节控制/状态)
HID_STREAM_DATA_MIN_INSTANCE     = 5   (Report ID 6)
HID_STREAM_DATA_MAX_INSTANCE     = 7   (Report ID 8)
```

开麦/关麦写的是 **HID Report 特征（0x2A4D）的 Report ID 4**，20 字节：

```text
开麦：00 00 00 80 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
关麦：00 00 00 80 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

按键触发条件（`AudioRCGattCallback.onCharacteristicChanged`）：

- Report ID 1：`value[2] == 0x3E` 按下；
  `value[2] == 0 && value[0] == 0` 松开；
- Report ID 5 的 20 字节控制报告也会报按下/松开（`data[0] == 1/0`）；
- 按下时 TV 还会调用 `GattService.buildInitialAudioConnection(mRc)`（native），
  并把连接参数切到 `interval=6 (7.5 ms), latency=299, timeout=1200 (12 s)`；
- 音频由 HID 主机走 `hidraw/uHID` 读原始报告，不经过 App 的 GATT 客户端。

本机 Report 表（`test` 日志与 HIDREPORT 实抓）与该实例顺序完全吻合：

```text
HID 服务 0x1812：2A4A(39) 2A4C(41) 2A4E(43) 2A4B(45) 2A22(47) 2A32(50)
Report ID 1 -> handle 52   键盘/语音键 0x3E
Report ID 2 -> handle 56   输出
Report ID 3 -> handle 59   输入
Report ID 4 -> handle 63   props=0x0A（read+write，无 notify）← 开麦
Report ID 5 -> handle 66   20 字节控制
Report ID 6 -> handle 70   音频
Report ID 7 -> handle 74   音频
Report ID 8 -> handle 78   音频
```

TV 里还有一条 `BluetoothPhoneRcSecure` 的经典蓝牙 RFCOMM/Opus 通道
（`11 22 33 00/11/22/33`），但服务名和代码都表明它是给**手机遥控器**的，
不是实体 BLE 遥控器；不要把它当成语音主路径。

PID 判定：`checkXiaomiRcPid()` 只看 `(pid & 0x3200) == 0x3200`，所以本机
`0x32BA` 属于小米遥控家族；TV 已知的音频型号包括 Realtek `0x32B8`、
Dialog `0x32B0/0x32B3`、Cypress `0x32B4`、Broadcom `0x3207`。

**固件对应实现（2.2.0 开发线）**：`br_hid.c` 在 HID 服务里按 Report ID 4
选开麦控制（找不到时退化为第 4 条 0x2A4D），语音键按下/松开时写上述
20 字节，并在 GATT 空闲时补发；Report ID 1 的 `0x3E` 与 Report ID 5 的
20 字节控制报告统一去重。真机验收必须看到：

```text
EV HID phase=au_ctrl handle=63 props=10
EV HID phase=voice down=1
小米开麦命令已发: 开
... 60 字节音频 RAW/HIDRAW ...
EV HID phase=voice down=0
小米开麦命令已发: 关
```

**真机确认（2026-11-25 22:00）**：两次按住语音键，板端日志：

```text
EV HID idx=0 phase=voice down=1
br_hid: 小米开麦命令已发: 开
EV HID idx=0 phase=au_ok on=1
br_voice: 小米音频开始 peer=0
br_voice: 小米音频 rx=137 ok=137 err=0 h2=f8 rms=1959
br_voice: 小米音频 rx=274 ok=274 err=0 h2=38 rms=1727
EV HID idx=0 phase=voice down=0
br_voice: 小米音频结束 物理键释放 rx=372 ok=372 err=0 rejected=0
br_hid: 小米开麦命令已发: 关
```

PC 侧用户确认能听到/识别遥控器说话，验收通过。需要注意：

- 早期每帧都发 `HIDRAW` 会把 USB CDC 拖死，造成 `ACL buf alloc failed`；
  现在只发前 4 帧。
- 测试时如果没有人读 COM12，TinyUSB 的 `Flush failed` 也会刷屏并拖慢主机任务；
  正常客户端会读 COM12，或测试时开一个读端。
- 遥控器空闲不广播，板子重启后必须按键唤醒；已配对的电视/电脑也可能抢占它。

Realtek 20 字节音频变体尚未遇到，本机 `0x32BA` 实际发的是 60 字节 mSBC。

## 六、顺带修掉的四个真 bug（都与「支持新遥控器」直接相关）

1. **小米那台的松手事件被吞掉**（灯一直常亮、手势判不出来）
   —— 8 字节「后七字节全 0」的报告被当成谷歌那种「首字节按键码」格式处理，
   全 0 的松手包走进了谷歌分支。修法：小米那台不走谷歌分支。
2. **Report Map 解析器从来没记下 page/usage**
   —— `map_entry()` 先把 `seen` 置真，后面 `if (!e->seen)` 的赋值块永远不执行，
   于是所有报告的角色都是 NONE（此前只靠「8 字节=键盘」的长度猜测兜着）。
3. **两张表都太小，把新遥控器的关键特征挡在门外**
   —— HID 报告表 16 条（这台有 20 多条 2A4D）、GATT 特征表 40 条（这台正好 40），
   厂商服务的特征从来没被枚举过。已扩到 64 / 96。
4. **语音控制 Feature 报告被当成键盘报告**
   —— 20 字节控制包的 `0x01/0x00` 首字节被差分成左 Shift 按下/抬起。
   修法：小米模式下只有 Input 报告能进入按键解析，Feature 报告只做原始取证。
