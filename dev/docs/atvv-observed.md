# ATVV 实测记录（Chromecast Voice Remote）

> 记录时间：2026-11-05
> 记录方式：ESP32-S3（IDF v6.1 / NimBLE 2.x）连上遥控器后做全量 GATT 发现
> 固件：`dev/firmware/bridge`（探针版 0.9.0-probe）

## 1. 结论摘要

**ATVV 服务在 ESP32 侧完全可见**，语音方案（ESP32 做 BLE 中心 + 转发音频 + PC 侧解码识别）成立。

```
EV CONN     idx=0 addr=7E:A2:AA:BB:CC:02 type=0 name=bonded handle=0
DT ATVVSVC  uuid=AB5E0001-5A21-4F05-BC7D-AF01F617B664  start=80 end=88
DT ATVV     AB5E0002 (TX)      handle=82 props=0x0C
DT ATVV     AB5E0003 (audio)   handle=84 props=0x12
DT ATVV     AB5E0004 (control) handle=87 props=0x12
EV GATTDONE idx=0 svcs=8 chrs=36 atvv=1
EV ATVVREADY idx=0 audio=84 ctl=87 tx=82 ok=1
```

## 2. 遥控器的全部服务

| 服务 UUID | 句柄范围 | 说明 |
|---|---|---|
| `1800` | 1..7 | Generic Access |
| `1801` | 8..11 | Generic Attribute |
| `180A` | 12..30 | Device Information |
| `180F` | 31..34 | Battery |
| `1812` | 35..73 | **HID**（按键通道） |
| `AE40` | 74..79 | 厂商私有（Google） |
| `AB5E0001-…` | 80..88 | **ATVV 语音** |
| `D343BFC0-…` | 89..100 | 厂商私有 |

## 3. ATVV 服务的特征布局

| 特征 | def | val | props | 含义 |
|---|---|---|---|---|
| `AB5E0002-…` | 81 | 82 | 0x0C | TX：主机 → 遥控器（Write / Write-No-Response） |
| `AB5E0003-…` | 83 | 84 | 0x12 | audio：遥控器 → 主机（Notify） |
| `AB5E0004-…` | 86 | 87 | 0x12 | control：遥控器 → 主机（Notify） |

props 位含义（BLE GATT Char Properties）：
- `0x02` = Read，`0x04` = Write-No-Response，`0x08` = Write，`0x10` = Notify，`0x20` = Indicate
- TX `0x0C` = Write-No-Response + Write
- audio / control `0x12` = Notify + Write-No-Response

## 4. 已确认的行为

| 事实 | 依据 |
|---|---|
| 遥控器**空闲时不广播** | 扫 40 秒 160 个设备里没有它；只能靠 bond 表的 identity address 主动连 |
| 主动连接需要遥控器「上线」 | 遥控器还连着电视时主动连必超时（`EV CONNFAIL status=13` = ETIMEOUT） |
| 配对模式（长按 Home）下能连上 | 实测成功，且能完成全量 GATT 发现 |
| identity address 是 `7E:A2:AA:BB:CC:02`，**type=0（PUBLIC）** | bond 表枚举 + 实测连接成功 |
| NimBLE 不允许在过程回调里嵌套发起过程 | 在 `disc_all_chrs` 回调里直接调 `disc_all_dscs` 会失败 → CCCD 一个都发现不到（`cccd=0`）。必须由主循环驱动状态机 |

## 5. 待确认：control 通道的 opcode 语义

两个资料源**互相冲突**，必须以真机字节为准：

| 来源 | `0x08` | AUDIO_START 布局 |
|---|---|---|
| windows-remote-mic-app（`atvv_protocol.py`） | `OPCODE_MIC_BUTTON = 0x08`（麦克风键按下） | `[0x04, len, reason, codec, sid]`，`codec==2` 表示 ADPCM |
| Google Voice over BLE 规范 | `START_SEARCH = 0x08`（遥控器请求主机开始搜索） | `[0x04, reason=3(HTT), codec, sid]`，`codec==0` |
| RemoteMapper-ESP32 文档 | — | `[0x04, 0x03, 0x00, SID]` |

**计划**：固件订阅 control 特征后按住语音键，把原始字节打到串口（`DT CTL hex=…`），
据此定死：

- 语音键按下的 opcode 与载荷
- 一段语音的开始/结束事件
- 会话号（sid）的位置与宽度
- 音频帧长（winmic 记录 120 字节/帧 → 240 样本）

⚠️ 官方规范里还有 `START_SEARCH(0x08)` 与 `MIC_OPEN(0x0C)` 这类「主机主动开麦」流程。
**本项目只做 hold-to-talk**（遥控器自己开麦），不需要主机写 TX；
但 hold 模式下的按键边沿到底由哪个 opcode 表达，仍需实测。

## 6. 对实现的约束（从踩坑里得到的）

1. **BLE 回调里只做一次 memcpy**。通知帧先进队列，协议组装全部在主循环里做。
   （CMCC 实测：回调里阻塞发送 → 遥控器判定拥塞主动断链）
2. **发现流程必须由主循环驱动状态机**，不能在过程回调里发起下一个过程。
3. **栈要省着用**。`br_kv_t` 是栈上局部变量，曾因为开成 2048 字节把 `cdc` 任务打爆栈
   （`A stack overflow in task cdc has been detected.`）。现在限死 512 字节。
4. **ESP_LOG 不要被钩子吞掉**。旧实现把 `esp_log_set_vprintf` 指向空函数，
   结果现场一行日志都看不到。现在日志固定走 UART0（CH343）。
