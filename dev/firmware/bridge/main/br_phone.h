/*
 * br_phone - 手机（移动端）事件 → USB HID 的翻译层
 * ---------------------------------------------------------------------------
 * 手机端页面（照抄 movo 项目）通过 WiFi 把事件发给板子，
 * 板子把事件翻译成 USB HID 报告发给 PC。
 *
 * ★ 事件集与 movo 服务端保持一致 ★
 *   movo 的前端页面（index.html / keyboard.html / voice.js）发的事件名
 *   一个都不改，板子这边照单全收。
 *
 * 事件分类：
 *   - 指针类（move / click / scroll / zoom / drag / mid）→ USB HID 鼠标报告
 *   - 键盘类（tap_key / key_action / key_combo）      → USB HID 键盘报告
 *   - PC 专属类（type_text / sys_action）             → 经 CDC 上报给 PC 客户端
 *   - 语音类（voice_start / voice_end / voice_audio） → USB UAC1 麦克风
 *
 * 「幽灵右键」防线见 br_usb.c 的 s_mouse_buttons 注释 ——
 * 只有板子自己按下过才允许抬起。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化（目前无需资源，预留） */
void br_phone_init(void);

/*
 * ★ 手机最近 15 秒有没有发过事件 ★
 *   手机是 WiFi 设备，不是蓝牙 —— 上报给 PC 时不能写死「已连接」，
 *   PC 端的「手机」页面要显示真实的在线状态。
 */
#define BR_PHONE_ONLINE_MS 15000
bool br_phone_recently_active(void);

/**
 * 处理一个手机事件。
 *
 * @param name  事件名（move / click / tap_key …）
 * @param args  参数对象（JSON 文本，形如 {"dx":1,"dy":2}），可为 NULL
 */
void br_phone_event(const char *name, const char *args);

/**
 * 语音：手机把采到的 PCM 推过来（16 kHz / 单声道 / int16）。
 *
 * 与 ATVV 语音互斥仲裁，见 br_voice.c。
 */
void br_phone_push_pcm(const uint8_t *pcm, size_t len);
/** 手机送来的 ADPCM 语音（和遥控器 ATVV 同一套解码器），reset = 本次录音第一包 */
void br_phone_push_adpcm(const uint8_t *data, size_t len, bool reset);

/** 语音开始（板子发热键唤醒 PC 的输入法） */
void br_phone_voice_start(void);

/** 语音结束 */
void br_phone_voice_end(void);

#ifdef __cplusplus
}
#endif
