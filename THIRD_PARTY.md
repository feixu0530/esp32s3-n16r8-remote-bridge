# 第三方组件

绿色版客户端 `ESP32S3RemoteBridge/` 内随包分发下列第三方组件，均为**未修改**的原样分发。
本项目自身代码采用 MIT（见 `LICENSE`），与这些组件的许可互不影响。

| 组件 | 用途 | 许可 | 项目地址 |
|---|---|---|---|
| Qt for Python (PySide6) | 界面框架 | LGPL-3.0 | https://www.qt.io/qt-for-python |
| esptool | 固件刷写 | GPL-2.0 | https://github.com/espressif/esptool |
| onnxruntime | 语音模型推理 | MIT | https://github.com/microsoft/onnxruntime |
| pyserial | 串口通信 | BSD-3-Clause | https://github.com/pyserial/pyserial |

如需这些组件的源代码：
- PySide6 / Qt 源码：https://code.qt.io/ （或 https://download.qt.io/official_releases/QtForPython/）
- esptool 源码：https://github.com/espressif/esptool
- onnxruntime 源码：https://github.com/microsoft/onnxruntime
- pyserial 源码：https://github.com/pyserial/pyserial

固件基于 **ESP-IDF**（Apache-2.0）与 **Apache NimBLE**（Apache-2.0）构建：
- https://github.com/espressif/esp-idf
- https://github.com/apache/mynewt-nimble

## 手机 App（Fremote）的上游

`Fremote-2.0.0.apk` 基于开源项目 **movo** 的 Android WebView 遥控端修改而来：
- 项目地址：https://github.com/dsnaaa/movo
- 原项目作者与贡献者保留署名；本项目的改动（BLE 直连、快捷键页、界面与文案调整）
  及原项目代码的许可遵循其仓库说明。
