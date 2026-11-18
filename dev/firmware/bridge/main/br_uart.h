/*
 * bridge_uart - UART0 协议通道接口
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化 UART0 协议通道 */
void br_uart_init(void);

/** 通道是否可用 */
bool br_uart_connected(void);

/** 把一行文本交给协议层处理 */
typedef void (*br_uart_line_fn)(const char *line);

/** 注册行处理回调（CDC 与 UART 两条通道共用同一套命令） */
void br_uart_set_line_handler(br_uart_line_fn fn);

void br_uart_push_line(const char *line);

/** 输出一段文本 */
void br_uart_write(const char *text, size_t len);

#ifdef __cplusplus
}
#endif
