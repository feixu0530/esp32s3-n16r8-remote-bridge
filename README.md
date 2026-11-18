# ESP32-S3 N16R8 遥控器桥接（Remote Bridge）

把 **Google Chromecast / Google TV 蓝牙遥控器**变成 PC 的键盘、鼠标和语音输入设备。

遥控器通过 BLE 连到 ESP32-S3，板子再把按键翻译成 USB HID 报告发给电脑；
语音键走 ATVV 音频通道，板子同时充当 USB 麦克风，声音直接送进 Windows 输入法。

- 作者：Figo（feixu0530@qq.com）
- 当前版本：**1.0.0**（PC 端与固件同号）
- 平台：Windows 10 / 11 x64

---

## 1. 硬件要求

| 项目 | 要求 |
|---|---|
| 开发板 | **ESP32-S3，N16R8**（16 MB Flash + 8 MB Octal PSRAM），板载 **CH343** USB 串口 |
| 遥控器 | Google Chromecast / Google TV 遥控器（BLE HID + ATVV 语音） |
| 电脑 | Windows 10 / 11 x64，一个空闲 USB 口 |

> ⚠️ 只在这块 **N16R8** 板子上验证过（固件按 16 MB Flash 布局编译）。
> 其它 S3 板子（尤其 Flash/PSRAM 不同的）请自行改 `sdkconfig` 重新编译。

---

## 2. 快速开始（不需要装任何开发环境）

1. 解压 `ESP32S3RemoteBridge-1.0.0-win64.zip`，双击 `ESP32S3RemoteBridge.exe`
2. **设备页** → 开发板下拉选 `COM12 — 协议通道` → 点「连接」
3. **蓝牙配对**：
   - 遥控器：长按 `Home` 键约 3 秒（指示灯闪烁，进入配对模式）
   - 软件：点「进入配对模式」
   - 两边同时进行，配对成功后设备页显示遥控器名字和 MAC
4. **按键页**：给每个键配动作（单击 / 双击 / 长按）
   - 改完**立即自动同步到板子**，不需要点保存
   - 支持组合键，例如 `ctrl+shift+p`、`lwin+d`、`,`（标点直接录）
   - 「检测真实按键」可连续按遥控器，界面自动跳到对应行
   - 「存为型号默认」把当前配置存成该型号的默认，重配后自动套用
5. **语音页**：
   - 「输入法设置」打开输入法自己的设置页，把语音快捷键设成和这里一致
     （默认 `左Ctrl + 左Win`；微信输入法会直接打开它的设置程序）
   - 之后按住遥控器语音键说话，松开结束

---

## 3. 烧录 / 升级固件

### 方式一：软件内刷写（推荐）

1. 设备页 → 刷写固件 → 烧录通道选 `COMx — 烧录通道`（板载 CH343）
2. 「选固件」→ 选中 `firmware/remote_bridge-1.0.0.bin`
3. 点：
   - **仅应用**：只写应用区（`0x10000`），配对表 / 按键映射 / 语音快捷键**全部保留**
   - **烧录**：bootloader + 分区表 + 应用一起写（换固件大版本时用）

绿色包**已内置 esptool**，不需要安装 ESP-IDF 或 Python。

### 方式二：命令行（esptool）

```bat
esptool --chip esp32s3 --port COM6 -b 460800 --before default-reset --after hard-reset ^
  write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB ^
  0x10000 remote_bridge-1.0.0.bin
```

> 刷完板子会重启；遥控器**不会自动回连**（它在空闲时不广播），
> 按一下遥控器任意键即可唤醒回连。

---

## 4. 目录说明（本发布包）

```
1.0.0/
├─ ESP32S3RemoteBridge/                    绿色版客户端（解压即用）
├─ ESP32S3RemoteBridge-1.0.0-win64.zip     绿色版客户端压缩包
├─ ESP32S3-all-remote-1.0.0-src.zip        完整源代码
├─ firmware/remote_bridge-1.0.0.bin        应用固件（刷到 0x10000）
├─ README.md  LICENSE  THIRD_PARTY.md
└─ SHA256SUMS.txt                          校验和（sha256sum -c 可自检）
```

---

## 5. 已知限制

- 遥控器空闲时会睡眠、不广播；板子重启后需要按键唤醒才会回连
- 语音是**按住说话**：热键最长按住 2 秒（安全兜底），说话过长会被截断
- 「打开应用 / 鼠标单击」这类动作只有 PC 能做，**客户端必须运行**；
  纯按键动作（含组合键）在拔掉 PC 后由板子独立完成
- 只支持一块板子接一台电脑（客户端单实例，避免抢串口）

---

## 6. 致谢

本项目的界面布局、按键映射思路、语音方案来自下面两个开源项目的启发，
**代码全部重新实现，未复制其源码**：

- [ZSTDJan/windows-remote-mic-app](https://github.com/ZSTDJan/windows-remote-mic-app)（GPL-3.0）
- [jackielozet/cmcc-voice-remote-bridge](https://github.com/jackielozet/cmcc-voice-remote-bridge)（MIT）

以及它们各自的上游 [nijez/open-voice-bridge](https://github.com/nijez/open-voice-bridge)。

---

## 7. 许可

本项目代码采用 **MIT License**，见 `LICENSE`。
绿色包内含第三方组件（Qt/PySide6、esptool 等），各自许可见 `THIRD_PARTY.md`。
