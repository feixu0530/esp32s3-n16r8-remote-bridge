/*
 * bridge_usb - USB 复合设备（HID 键盘 + HID 消费者控制 + CDC 串口）
 * ---------------------------------------------------------------------------
 * 一个 USB 设备三个接口，共用一根线：
 *   1. HID 键盘        —— 映射后的按键输出
 *   2. HID 消费者控制  —— 音量/静音这类多媒体键
 *   3. CDC 串口        —— 与 PC 客户端通信的协议通道
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化 TinyUSB 复合设备 */
esp_err_t bridge_usb_init(void);

/** 输出一个 HID 键盘组合（最多 6 键 + 修饰键） */
void bridge_usb_send_keyboard(uint8_t modifier, const uint8_t *keys, uint8_t count);

/**
 * 键盘按键按下：加入「当前按着」集合并发报告。
 *
 * ★ 与 release 成对使用 —— USB HID 报告表达的是「当前按着哪些键」，
 *   抬起时必须发一个不含该键的报告，否则键会卡住。
 */
void bridge_usb_press_keyboard(uint8_t modifier, const uint8_t *keys, uint8_t count);

/** 键盘按键抬起：从「当前按着」集合移除并发报告 */
void bridge_usb_release_keyboard(uint8_t modifier, const uint8_t *keys, uint8_t count);

/**
 * ★ 按键释放看门狗 ★
 *
 *   任何按下的键/修饰键超过 3 秒就强制抬起。
 *   板子替 PC 按键时只要漏了一次「抬起」，那个键就会永远按在 PC 上
 *   —— PC 自己的键盘就废了。主循环每 200 ms 调一次。
 */
void bridge_usb_hid_watchdog(void);
/** 语音热键按住期间豁免 3 秒卡键保护 */
void bridge_usb_set_hold_exempt(bool on);

/** 输出消费者控制键（16 位 usage） */
void bridge_usb_send_consumer(uint16_t usage);

/** 消费者键按下 */
void bridge_usb_press_consumer(uint16_t usage);

/** 消费者键抬起（发 usage=0 的报告） */
void bridge_usb_release_consumer(uint16_t usage);

/* -------------------------------------------------------------- 鼠标 */

/** 鼠标按键位掩码 */
#define BR_MOUSE_LEFT   0x01
#define BR_MOUSE_RIGHT  0x02
#define BR_MOUSE_MIDDLE 0x04

/**
 * 鼠标相对移动（像素增量）。
 *
 * 内部会累加到整像素再发 —— 单个 HID 报告的位移字段只有 8 位有符号，
 * 直接截断会丢位移。
 */
void bridge_usb_mouse_move(int32_t dx, int32_t dy);

/** 鼠标滚轮（dy>0 向上滚） */
void bridge_usb_mouse_scroll(int32_t dy);

/**
 * 鼠标按键按下。
 *
 * 按下前会先清掉「本该没按着却残留」的键（抬起丢了的兜底）。
 */
void bridge_usb_press_mouse_button(uint8_t button);

/**
 * 鼠标按键抬起。
 *
 * ★ 只有板子自己按下过才发抬起 ★
 *   HID 鼠标报告是状态式的，发一个「本来没按着」的抬起，
 *   系统会当成一次真实的右键抬起，前景窗口直接弹右键菜单。
 */
void bridge_usb_release_mouse_button(uint8_t button);

/** 鼠标当前按着的键（诊断用） */
uint8_t bridge_usb_mouse_buttons(void);

/** 读一行 CDC 输入（阻塞，超时返回 false） */
bool bridge_usb_read_line(char *buf, size_t cap, uint32_t timeout_ms);

/** CDC 是否已连接 */
bool bridge_usb_cdc_connected(void);

/** HID 接口是否已挂载并可发送报告 */
bool bridge_usb_hid_ready(void);

/** CDC 写原始字节 */
void bridge_usb_write(const char *data, size_t len);

/** 把 ESP-IDF 日志重定向到 USB CDC（让 UART0 专供协议） */
void bridge_usb_install_log_hook(void);

/** 冲刷 CDC 发送缓冲（主循环周期调用） */
void bridge_usb_flush_cdc(void);

/** 打印 CDC 内部状态到 UART（诊断用） */
void bridge_usb_dump_cdc_state(void);

#ifdef __cplusplus
}
#endif
