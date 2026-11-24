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

/* ------------------------------------------------------------ 手机语音 */
/*
 * 手机（移动端）把采到的 PCM 推进来，板子从 USB UAC1 麦克风端点送出去，
 * PC 的输入法直接把它当麦克风输入去识别 —— PC 端零改动。
 *
 * ★ 与 ATVV 语音互斥仲裁 ★
 *   UAC1 端点只有一路 PCM。ATVV（遥控器麦克风）与手机麦克风不能同时供数据，
 *   否则两路音频交织 → 撕裂/爆音。
 *   谁先开始谁供，另一路的音频被丢弃。
 */
void br_voice_phone_start(void);
void br_voice_phone_end(void);
void br_voice_push_phone_pcm(const uint8_t *pcm, size_t len);

/**
 * 手机送来的 ADPCM（4bit IMA，和遥控器 ATVV 同一套编解码）。
 *
 * ★ 为什么手机也用 ADPCM ★
 *   手机 → 板子走 WiFi，实测一次请求的固定开销 ~90ms、带宽 ~15KB/s。
 *   16kHz 裸 PCM 是 32KB/s —— 这条链路根本送不到实时，
 *   音频只能一半一半地挤过去，电脑那边听到的是断断续续的碎片。
 *   ADPCM 压 4 倍（8KB/s）才有实时余量，
 *   而且解码器和遥控器共用（同一张步长表）——
 *   手机和遥控器从此真的是「同一条通道」。
 *
 * reset=true = 这一包是本次录音的第一包，要把解码器状态清零
 * （手机端每次录音都新建编码器，状态必须从头对齐）。
 */
void br_voice_push_phone_adpcm(const uint8_t *data, size_t len, bool reset);

/**
 * 语音热键看门狗：热键按下去之后若迟迟等不到抬起（ATVV 的 AUDIO_STOP 帧
 * 未必来），超时强制释放 —— 不然左Win 会一直按在 PC 上，键盘就废了。
 * 主循环周期调用。
 */
void br_voice_watchdog(void);
/** 链路断开：把可能还按着的语音热键放掉 */
void br_voice_on_disconnect(void);

