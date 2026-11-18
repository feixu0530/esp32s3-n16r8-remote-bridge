# 第三方组件

绿色版客户端随包分发下列第三方组件（均为未修改的原样分发）。
本项目自身代码采用 MIT（见 LICENSE），与这些组件的许可互不影响。

| 组件 | 用途 | 许可 | 项目地址 |
|---|---|---|---|
| Qt for Python (PySide6) | 界面框架 | LGPL-3.0 | https://www.qt.io/qt-for-python |
| esptool | 固件刷写 | GPL-2.0 | https://github.com/espressif/esptool |
| onnxruntime | 语音模型推理 | MIT | https://github.com/microsoft/onnxruntime |
| pyserial | 串口通信 | BSD-3-Clause | https://github.com/pyserial/pyserial |

固件基于 ESP-IDF（Apache-2.0）与 Apache NimBLE（Apache-2.0）构建：
- https://github.com/espressif/esp-idf
- https://github.com/apache/mynewt-nimble
