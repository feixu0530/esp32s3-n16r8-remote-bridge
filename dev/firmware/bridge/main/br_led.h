/*
 * bridge_led - 板载 RGB 灯接口
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化 RMT 通道与灯珠（含启动自检闪烁） */
void bridge_led_init(void);

/** 启动 LED 状态机任务（需在调用 init 后创建） */
void bridge_led_task(void *arg);

/** 遥控器按键按下 */
void bridge_led_key_down(void);

/** 遥控器按键抬起 */
void bridge_led_key_up(void);

/**
 * 设置 BLE 连接状态。
 * 未连接 -> 蓝色呼吸；已连接 -> 熄灭（等按键再亮）。
 */
void bridge_led_set_connected(bool on);

/**
 * 正在尝试连接遥控器 —— 蓝色慢呼吸。
 * 让用户一眼看出「板子在找遥控器」还是「遥控器不在」。
 */
void bridge_led_connecting(void);

/** 空闲（没连上，也没在尝试）—— 熄灭 */
void bridge_led_idle(void);

/**
 * 正在等人来配对 —— **快速闪蓝**。
 * 「未连接」只是慢慢呼吸，配对窗口是明显的快闪，
 * 用户一眼就知道「板子在等人，把遥控器按到配对模式」。
 */
void bridge_led_set_pairing(bool on);

/** 语音通道有数据 */
void bridge_led_voice_active(void);

/** 语音通道静默（仍在传输但没新帧） */
void bridge_led_voice_idle(void);

/** 直接输出 RGB（不做状态管理，供自检用） */
void bridge_led_raw(uint8_t r, uint8_t g, uint8_t b);

#ifdef __cplusplus
}
#endif
