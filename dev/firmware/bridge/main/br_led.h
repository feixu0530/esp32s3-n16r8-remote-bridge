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
void bridge_led_set_phone_linked(bool on);

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

/**
 * ★ 配对成功提示 —— 绿灯快闪 1.5 秒 ★
 *
 *   配完 Chromecast 遥控器它**立刻就睡了**，板子随即回到
 *   「未连接 → 蓝色呼吸」—— 用户看着像「还在配对，没成」。
 *   明确闪一下绿灯，这事就说清楚了。
 */
void bridge_led_pair_ok(void);

/**
 * ★ 手机端有输入（触摸板/陀螺仪/按键/文字）★
 *
 *   手机走蓝牙以后，用户看不到 PC 那边接没收到 ——
 *   灯闪一下就是「板子收到了」的直接反馈。
 *   30Hz 的 move 会被限流成「持续亮、停手 150ms 后灭」，不会频闪。
 */
void bridge_led_phone_activity(void);

/** 手机输入闪灯累计次数（排障/自动化验证用：心跳不该让它增长） */
uint32_t bridge_led_activity_count(void);

/** 语音通道有数据 */
void bridge_led_voice_active(void);
/** 语音结束：把语音灯关掉（只关语音模式） */
void bridge_led_voice_off(void);

/** 语音通道静默（仍在传输但没新帧） */
void bridge_led_voice_idle(void);

/** 直接输出 RGB（不做状态管理，供自检用） */
void bridge_led_raw(uint8_t r, uint8_t g, uint8_t b);

#ifdef __cplusplus
}
#endif
