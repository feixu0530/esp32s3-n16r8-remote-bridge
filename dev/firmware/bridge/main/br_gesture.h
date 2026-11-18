/*
 * br_gesture - 板端按键手势引擎（单击 / 双击 / 长按）
 * ---------------------------------------------------------------------------
 * 为什么放在板子上：
 *   用户要求「按键规则按照遥控器保留在开发板，PC 端软件只是一个配置页面」。
 *   拔掉 PC，板子也得能识别双击和长按 —— 所以判定必须在固件里。
 *
 * 语义照抄参考项目 windows-remote-mic-app 的 ButtonGestureRecognizer
 * （reference/winmic-src/.../ovb_rc003/button_gesture.py:194-196）：
 *     DOUBLE_CLICK_SECONDS = 0.300
 *     LONG_PRESS_SECONDS    = 0.550
 *
 * 状态机（与参考项目一致）：
 *   - 只配了单击的键：**按下立即触发**，不等窗口（保住现有手感）
 *   - 配了双击的键：单击被**推迟**到 0.3 s 双击窗口超时后才触发
 *   - 第二次松开 → 触发双击
 *   - 按住满 0.55 s → 触发长按；长按触发后松开**不再**补单击
 *
 * 输出用 press/release 两段表示：
 *   - 单击（快路径）：down 时 press，up 时 release
 *   - 双击 / 推迟的单击：press 后立刻 release（一次点按）
 *   - 长按：0.55 s 时 press，up 时 release
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* 触发类型，取值与 br_types.h 的 BR_TRIG_* 一致 */
#define BR_GESTURE_SINGLE 0u
#define BR_GESTURE_DOUBLE 1u
#define BR_GESTURE_LONG  2u

#define BR_GESTURE_DOUBLE_MS 300u
#define BR_GESTURE_LONG_MS  550u

/** 同时跟踪的按键数上限 */
#define BR_GESTURE_MAX 16

/**
 * 查询某个键是否配了某种触发。
 * ctx 是 init 时传的上下文。
 */
typedef bool (*br_gesture_has_fn)(void *ctx, const uint8_t addr[6],
                                  uint32_t key_id, uint8_t trig);

/**
 * 触发回调。
 * press=true 表示「按下这个动作对应的输出」，press=false 表示「松开」。
 */
typedef void (*br_gesture_fire_fn)(void *ctx, const uint8_t addr[6],
                                   uint32_t key_id, uint8_t trig, bool press);

void br_gesture_init(br_gesture_has_fn has_fn, br_gesture_fire_fn fire_fn,
                      void *ctx);

/** 按下沿 */
void br_gesture_press(const uint8_t addr[6], uint32_t key_id);

/** 松开沿 */
void br_gesture_release(const uint8_t addr[6], uint32_t key_id);

/** 清空所有跟踪状态（遥控器断开时用） */
void br_gesture_reset(void);

/**
 * 手势引擎自检结果。
 *
 * 三个场景各测一遍：只配单击（快路径）、单击+双击、单击+长按。
 */
typedef struct {
    bool single_fired;   /* 快路径单击立刻触发 */
    bool double_fired;   /* 双击被识别 */
    bool long_fired;     /* 长按被识别 */
    bool single_after_double;  /* 双击之后**不应该**再补一次单击 */
    bool single_after_long;    /* 长按之后**不应该**再补一次单击 */
    bool pass;
} br_gesture_test_t;

/**
 * 跑一遍手势自检。
 *
 * ★ 不需要遥控器在场 ★
 *   临时接管 has/fire 回调，合成按键序列验证判定，然后恢复原回调。
 *   整个过程约 2 秒（要真的等 0.3 s / 0.55 s 的窗口）。
 */
bool br_gesture_self_test(br_gesture_test_t *out);
