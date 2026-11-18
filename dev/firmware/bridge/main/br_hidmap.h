/*
 * br_hidmap - HID Report Map 解析（判定 report 角色）
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BR_HIDMAP_MAX_REPORTS 64

/* report 角色 */
#define BR_ROLE_NONE     0   /* 认不出来 */
#define BR_ROLE_KEYBOARD 1   /* 键盘：修饰键 + 6 键码 */
#define BR_ROLE_CONSUMER 2   /* 消费者控制：16 位 usage */
#define BR_ROLE_DATA     3   /* 数据通道（如 0xFC 语音），不订阅 */
#define BR_ROLE_GAMEPAD  4   /* 游戏手柄：多字节按键位图 */
#define BR_ROLE_GENERIC  5   /* 通用兜底：Report Map 认不出来时按内容猜 */
#define BR_ROLE_VENDOR   6   /* 厂商自定义页（0xFF01 等）：语音通道等 */

typedef struct {
    bool     seen;
    uint16_t page;    /* Usage Page（厂商自定义页可达 0xFF01，需 16 位） */
    uint8_t  usage;   /* 集合的第一个 Usage */
    uint16_t bits;    /* 该 report 的总位数 */
} br_hidmap_entry_t;

typedef struct {
    br_hidmap_entry_t entries[BR_HIDMAP_MAX_REPORTS];
} br_hidmap_t;

/** 清空解析结果 */
void br_hidmap_reset(br_hidmap_t *map);

/**
 * 解析 Report Map 描述符。
 * 返回识别出的有效 report 数量。
 */
int br_hidmap_parse(br_hidmap_t *map, const uint8_t *report_map, uint16_t len);

/**
 * 判定某个 report id 的角色，并输出该 report 的字节数。
 * 返回 BR_ROLE_* 之一。
 */
uint8_t br_hidmap_role(const br_hidmap_t *map, uint8_t report_id,
                       uint16_t *bytes);

#ifdef __cplusplus
}
#endif
