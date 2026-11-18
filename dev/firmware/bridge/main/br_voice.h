#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

esp_err_t br_voice_init(void);
/**
 * 处理 ATVV 控制帧。
 *
 * 返回 true = 这次真的把语音热键按出去了（通道开着）。
 * 返回 false = 没按（比如语音通道被关了）—— 事件要靠它说实话，
 * 不然日志会说「触发了」而输入法根本没收到。
 */
bool br_voice_control(const uint8_t *data, size_t len);
void br_voice_audio(const uint8_t *data, size_t len);
size_t br_voice_read_pcm(void *dst, size_t max_bytes);
bool br_voice_streaming(void);
bool br_voice_set_hotkey(uint8_t modifier, const uint8_t *keys, uint8_t count);

/**
 * 读回当前语音快捷键（PC 端语音页要显示板子上真正生效的值）。
 *
 * modifier 是 USB HID 修饰键位掩码；keys/count 是普通键（最多 6 个）。
 * 三个指针都可以传 NULL 表示不取该项。
 */
bool br_voice_get_hotkey(uint8_t *modifier, uint8_t *keys, uint8_t *count);
/** 语音通道开关（PC 端控制：关掉就不再替 PC 按语音快捷键） */
void br_voice_set_enabled(bool on);
bool br_voice_enabled(void);

/**
 * 语音热键看门狗：热键按下去之后若迟迟等不到抬起（ATVV 的 AUDIO_STOP 帧
 * 未必来），超时强制释放 —— 不然左Win 会一直按在 PC 上，键盘就废了。
 * 主循环周期调用。
 */
void br_voice_watchdog(void);

