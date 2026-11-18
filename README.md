# ESP32-S3 N16R8 遥控器桥接（Remote Bridge）

把 **Google Chromecast / Google TV 蓝牙遥控器**变成 Windows 的键盘、鼠标和语音输入设备。

遥控器通过 BLE 连到 ESP32-S3，板子再把按键翻译成 USB HID 报告发给电脑；
语音键走 ATVV 音频通道，板子同时充当 USB 麦克风，声音直接送进 Windows 输入法。

- 作者：Figo（feixu0530@qq.com）
- 当前版本：**1.0.0**（PC 端与固件同号）
- 平台：Windows 10 / 11 x64

> 绿色版客户端、固件与完整源码压缩包见 **[Releases](https://github.com/feixu0530/esp32s3-n16r8-remote-bridge/releases)**。

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

## 2. 快速开始（用发布包，不需要开发环境）

1. 解压绿色包，双击 `ESP32S3RemoteBridge.exe`
2. **设备页** → 开发板下拉选 `COMxx — 协议通道` → 点「连接」
3. **蓝牙配对**：遥控器长按 `Home` 约 3 秒进入配对模式，软件同时点「进入配对模式」
4. **按键页**：给每个键配单击 / 双击 / 长按；改完立即自动同步到板子
   - 支持组合键（`ctrl+shift+p`、`lwin+d`、标点直接录）
   - 「检测真实按键」可连续按遥控器，界面自动跳到对应行
   - 「存为型号默认」把当前配置存成该型号默认，重配后自动套用
5. **语音页**：「输入法设置」打开输入法自己的设置页，把语音快捷键设成和这里一致
   （默认 `左Ctrl + 左Win`；微信输入法会直接打开它的设置程序）

---

## 3. 烧录 / 升级固件

### 方式一：软件内刷写（推荐）

1. 设备页 → 刷写固件 → 烧录通道选 `COMxx — 烧录通道`（板载 CH343）
2. 「选固件」→ 选中 `remote_bridge-x.y.z.bin`
3. 点：
   - **仅应用**：只写应用区（`0x10000`），配对表 / 按键映射 / 语音快捷键**全部保留**
   - **烧录**：bootloader + 分区表 + 应用一起写（换大版本时用）

绿色包**已内置 esptool**，不需要安装 ESP-IDF 或 Python。

### 方式二：命令行

```bat
esptool --chip esp32s3 --port COM6 -b 460800 --before default-reset --after hard-reset ^
  write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB ^
  0x10000 remote_bridge-1.0.0.bin
```

> 刷完板子会重启；遥控器**不会自动回连**（空闲时不广播），按一下遥控器任意键即可唤醒。

---

## 4. 从源码构建

### 客户端（Python 3.13 + PySide6）

```bat
cd dev\client\src
pip install PySide6 pyserial onnxruntime
python app.py
```

打包绿色版（PyInstaller onedir）：

```bat
cd dev\client
python build\package.py --version 1.0.0 --notes "..."
```

### 固件（ESP-IDF v6.1）

```bat
cd dev\firmware\bridge
idf.py set-target esp32s3
idf.py build
idf.py -p COM6 flash
```

> TinyUSB / esp_tinyusb 由 IDF 组件管理器按 `main/idf_component.yml` 拉取，仓库里不保存。

### 资源文件

图标 `.ico` 与遥控器照片 `.png` 是二进制，仓库里以 `*.b64` 保存。克隆后执行一次：

```bat
python tools\restore_assets.py
```

即可还原到 `dev/client/src/assets/` 对应位置。

---

## 5. 目录结构

```
dev/client/src        PySide6 客户端（core / platforms / ui-qml / voice）
dev/client/tests      闸门与真机测试脚本
dev/client/build      PyInstaller 打包脚本
dev/firmware/bridge   ESP-IDF 固件工程（main/ 为全部源码）
tools                 构建与调试小工具
```

---

## 6. 功能一览

- 蓝牙配对 / 自动回连 / 多遥控器列表
- 按键映射：单击 / 双击 / 长按，组合键、标点、左右修饰键
- 映射即时同步到板子；板子断网/拔掉 PC 仍可独立工作（纯按键动作）
- PC 专属动作：打开应用（内置常见应用 + 自定义任意 exe/快捷方式，数量不限）、
  鼠标左/右/中/X1/X2 单击与长按
- 语音：ATVV 音频直通 USB 麦克风；热键可配，微信输入法可一键打开设置
- 固件刷写：仅应用（保留数据）/ 完整刷写；绿色包内置 esptool
- 诊断日志（人话日志 + 板端日志）与一键导出（默认落到「下载」）

## 7. 已知限制

- 遥控器空闲时睡眠、不广播；板子重启后需按键唤醒才回连
- 语音为**按住说话**，热键最长按住 2 秒（安全兜底）
- PC 专属动作（打开应用 / 鼠标）需要客户端在运行
- 客户端单实例，避免多开抢串口

---

## 8. 致谢

界面布局、按键映射思路与语音方案参考了下面两个开源项目，**代码全部重新实现，未复制其源码**：

- [ZSTDJan/windows-remote-mic-app](https://github.com/ZSTDJan/windows-remote-mic-app)（GPL-3.0）
- [jackielozet/cmcc-voice-remote-bridge](https://github.com/jackielozet/cmcc-voice-remote-bridge)（MIT）
- 以及上游 [nijez/open-voice-bridge](https://github.com/nijez/open-voice-bridge)

## 9. 许可

本项目代码采用 **MIT License**，见 [LICENSE](LICENSE)。
绿色包内含第三方组件（Qt/PySide6、esptool 等），各自许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。
