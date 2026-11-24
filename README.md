# ESP32-S3 N16R8 遥控器桥接（Remote Bridge）

把 **Google Chromecast / Google TV 蓝牙遥控器** 和 **手机 App** 变成 PC 的键盘、鼠标、语音输入与快捷键盘。

遥控器通过 BLE 连到 ESP32-S3，板子再把按键翻译成 USB HID 报告发给电脑；
语音键走 ATVV 音频通道，板子同时充当 USB 麦克风，声音直接送进 Windows 输入法。

- 作者：Figo（feixu0530@qq.com）
- 当前版本：**2.0.0**（PC 端 / 固件 / APP 同号）
- 平台：Windows 10 / 11 x64

> 完整使用说明（PC / APP 逐页）见 **[docs/USER_MANUAL.md](docs/USER_MANUAL.md)**。

---

## 1. 硬件要求

| 项目 | 要求 |
|---|---|
| 开发板 | **ESP32-S3，N16R8**（16 MB Flash + 8 MB Octal PSRAM），板载 **CH343** USB 串口 |
| 遥控器 | Google Chromecast / Google TV 遥控器（BLE HID + ATVV 语音） |
| 电脑 | Windows 10 / 11 x64 |
| 数据线 | **Type-C 数据线 1~2 根**：日常使用 1 根；**刷固件时需要 2 根**（见下方说明） |

> ⚠️ 只在这块 **N16R8** 板子上验证过（固件按 16 MB Flash 布局编译）。
> 其它 S3 板子（尤其 Flash/PSRAM 不同的）请自行改 `sdkconfig` 重新编译。

---

## 2. 快速开始（不需要装任何开发环境）

1. 解压 `ESP32S3RemoteBridge-2.0.0-win64.zip`，双击 `ESP32S3RemoteBridge.exe`
2. **设备页** → 开发板下拉选 `COM12 — 协议通道` → 点「连接」
3. **蓝牙配对**：
   - 遥控器：长按 `Home` 键约 3 秒（指示灯闪烁，进入配对模式）
   - 软件：点「进入配对模式」
   - 两边同时进行，配对成功后设备页显示遥控器名字和 MAC
4. **手机 App**：安装 `Fremote-2.0.0.apk` → 首次打开进设置页 →「扫描」→ 点 `remote-bridge`
5. **按键页**：给每个键配动作（单击 / 双击 / 长按）
   - 改完**立即自动同步到板子**，不需要点保存
   - 支持组合键，例如 `ctrl+shift+p`、`lwin+d`、`,`（标点直接录）
   - 「检测真实按键」可连续按遥控器，界面自动跳到对应行
   - 「存为型号默认」把当前配置存成该型号的默认，重配后自动套用
6. **语音页**：
   - 「输入法设置」打开输入法自己的设置页，把语音快捷键设成和这里一致
     （默认 `左Ctrl + 左Win`；微信输入法会直接打开它的设置程序）
   - 之后按住遥控器语音键说话，松开结束

---

## 3. 烧录 / 升级固件

### 该刷哪个文件？（只有两种情况）

GitHub Release 里**只有一个固件** `remote_bridge-2.0.0.bin`，它就是应用固件，直接刷它即可：

| 场景 | 选择文件 | 写入地址 | 数据是否保留 |
|---|---|---|---|
| **日常升级**（1.x → 2.0.0，或以后的 2.0.x） | `remote_bridge-2.0.0.bin` | `0x10000`（应用区） | 配对表、按键映射、语音快捷键**全部保留** |
| **首次刷写 / 换板子 / 板子变砖** | 不用单独下载任何东西：绿色包内 `_internal/firmware/` 已带全三个文件 | `0x0` + `0x8000` + `0x10000` | 会清空板内配置，需要重新配对 |

- 日常升级**不要**刷 bootloader / 分区表，也不需要它们；它们只随绿色包提供，
  由客户端「烧录」按钮自动按地址写入。
- 旧 WiFi 回退版固件已停用，不再发布。

### ⚠️ 先看清楚：烧录需要**两根数据线**

开发板有**两个 Type-C 口**：

| 口 | 用途 | Windows 里显示为 |
|---|---|---|
| 一侧的**原生 USB**（ESP32-S3 自带） | 日常通信（协议通道） | `COMx — 协议通道` |
| **右侧的 Type-C**（板载 CH343） | **只在刷固件时用**（烧录通道） | `COMx — 烧录通道` |

- 平时用软件、配按键：**只插 1 根**（原生 USB）就够
- **刷写 / 升级固件：要把右侧 Type-C 也接到 PC（共 2 根）**，
  否则「刷写固件」那一行的通道下拉里根本不会出现烧录通道
- 刷完可以拔掉右侧那根，只留原生 USB 继续用

### 方式一：软件内刷写（推荐）

1. 设备页 → 刷写固件 → 烧录通道选 `COMx — 烧录通道`（板载 CH343）
2. 「选固件」→ 选中 `remote_bridge-2.0.0.bin`（Release 里唯一的 bin）
3. 点：
   - **仅应用**（日常升级就点这个）：只写应用区 `0x10000`，配对表 / 按键映射 / 语音快捷键**全部保留**
   - **烧录**（首次/救砖）：使用绿色包内置的 bootloader + 分区表 + 应用，
     依次写 `0x0` / `0x8000` / `0x10000`，会清空板内配置

绿色包**已内置 esptool**，不需要安装 ESP-IDF 或 Python。

> 烧录通道就是板子**右侧 Type-C（CH343）**；没插这根线时，设备页「烧录通道」会显示
> 「无烧录通道」，点烧录会提示找不到通道。

### 方式二：命令行（esptool）

```bat
esptool --chip esp32s3 --port COM6 -b 460800 --before default-reset --after hard-reset ^
  write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB ^
  0x10000 remote_bridge-2.0.0.bin
```

> 刷完板子会重启；遥控器**不会自动回连**（它在空闲时不广播），
> 按一下遥控器任意键即可唤醒回连。

---

## 4. Release 文件说明

GitHub Release 只放下面这几项，**固件只有一个 bin**：

```
ESP32S3RemoteBridge-2.0.0-win64.zip     Windows 绿色版（解压即用）
ESP32S3-all-remote-2.0.0-src.zip        完整源代码
Fremote-2.0.0.apk                       手机 App
remote_bridge-2.0.0.bin                 应用固件（日常升级只刷它，0x10000）
MANUAL-2.0.0-zh.md                      使用手册
SHA256SUMS.txt                          以上文件的 SHA256（sha256sum -c 自检）
```

解压绿色包后，`_internal/firmware/` 里还有 bootloader / 分区表 / 应用三件套，
只在客户端「烧录」全量写入时使用，**不需要单独下载**。

---

## 5. 手机 App（v2.0.0 新增）

手机通过 **BLE 直连板子**（不连板子 WiFi），装上 `Fremote-2.0.0.apk` 即可当：

- **触控板**：移动、轻点左键、双指滚动/缩放、侧边滚轮条，灵敏度/左右手可调
- **陀螺仪空鼠**：手机当空中鼠标，左右两块为左右键，可调灵敏度
- **文字输入**：页面上方输入条直接打字，停一下自动发送到 PC 光标处；也有全键盘页
- **语音**：按住麦克风说话，松手立即结束；声音经板子 USB 麦克风送进 PC 输入法出字
- **自定义快捷键页**：底部「快捷键」进入第三种模式（与触控板/陀螺仪并列），
  3 列网格、最多 64 个键，可上下滑动；每个键的动作在 PC「按键 → 手机布局」配置：
  - **组合键**：板子直接发 HID，关掉 PC 客户端仍可用
  - **打开应用**：由 PC 客户端执行（需客户端在线）
  - PC 端可拖动按钮吸附网格、改名（点名字）、删除（右键，需确认），改动实时同步手机与板子
- **中英双语**：设置页可切换中文 / English（所有页面统一）

**连接与配对**：首次安装打开 App → 设置页「扫描」→ 点 `remote-bridge`；
之后自动连接只针对保存过的那台 MAC，附近别人的同款板子不会连错。
删除配对在 **PC 端「设备」页**对应行「删除」；被删后手机重连会被板子拒绝，
直到 PC 重新「进入配对模式」。详见 **[docs/USER_MANUAL.md](docs/USER_MANUAL.md)**。

---

## 6. 已知限制

- 遥控器空闲时会睡眠、不广播；板子重启后需要按键唤醒才会回连
- 语音是**按住说话**：**按多久录多久**，松开（遥控器发 AUDIO_STOP）才结束；
  只有连续 3 秒收不到音频时才自动松开，10 分钟为硬上限（防卡键）
- 「打开应用 / 鼠标单击」这类动作只有 PC 能做，**客户端必须运行**；
  纯按键动作（含组合键）在拔掉 PC 后由板子独立完成
- 只支持一块板子接一台电脑（客户端单实例，避免抢串口）
- 手机端没有「删除配对」入口（防止手机自己解除限制），删除只能在 PC 端操作
- 手机 BLE 身份未做绑定（bond）时地址会轮换，PC 端按 MAC 记的手机名字可能丢；
  但自动连接以手机保存的板子地址为准，不影响连接本身

---

## 7. 致谢

本项目的界面布局、按键映射思路、语音方案来自下面两个开源项目的启发，
**代码全部重新实现，未复制其源码**：

- [ZSTDJan/windows-remote-mic-app](https://github.com/ZSTDJan/windows-remote-mic-app)（GPL-3.0）
- [jackielozet/cmcc-voice-remote-bridge](https://github.com/jackielozet/cmcc-voice-remote-bridge)（MIT）

以及它们各自的上游 [nijez/open-voice-bridge](https://github.com/nijez/open-voice-bridge)。

手机 App 的 WebView 页面与交互原型来自开源项目 **movo**
（[https://github.com/dsnaaa/movo](https://github.com/dsnaaa/movo)，Android WebView 遥控端），
本项目在其 APK 基础上改写页面并改为 BLE 直连，App 名称也由 movo 改为 **Fremote**；
原项目的作者与贡献者保留全部署名，许可见 `THIRD_PARTY.md`。
另感谢 **乐鑫 ESP-IDF / NimBLE / TinyUSB**、**Qt / PySide6**、**sherpa-onnx / onnxruntime**
与所有开源组件（许可见 `THIRD_PARTY.md`）。

---

## 8. 许可

本项目代码采用 **MIT License**，见 `LICENSE`。
绿色包内含第三方组件（Qt/PySide6、esptool 等），各自许可见 `THIRD_PARTY.md`。
