/*
 * br_hid - HID 输入报告订阅 + 按键解析
 * ---------------------------------------------------------------------------
 * 职责：
 *   1. 从 br_ble_core 已发现的特征表里挑出 Report(0x2A4D) 与 Report Map(0x2A4B)
 *   2. 读 Report Reference(0x2908) 拿到 report id
 *   3. 读 Report Map(0x2A4B) 判定每个 report 的角色
 *   4. 给输入报告订阅通知（写 CCCD 0x2902）
 *   5. 解析键盘/消费者报告 → 按键事件
 *
 * 描述符一律用发现流程记好的句柄，本模块不再自己发 Find-Info。
 *
 * ★ 分工 ★
 *   固件只报**原始按键事件**（src + usage + down/up），
 *   手势判定（单击/双击/长按）和映射放在 PC 侧。
 *   → 固件保持"可信传输层"的定位（方案 D1），也省掉一份手势引擎。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "br_hidmap.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * ★ 一台遥控器最多记多少条 HID 报告特征 ★
 *
 * 16 太少了：小米一代的 HID 服务里有 20 多条 2A4D 特征，
 * 排在第 17 条以后的（其中就有带 CCCD、能通知的语音音频通道）
 * 压根不会被记进表里，也就永远订阅不到。
 */
#define BR_HID_MAX_REPORTS 64

/* 报告来源（与固件 br_types.h 的 BR_SRC_* 对齐） */
#define BR_HID_SRC_KEYBOARD 1
#define BR_HID_SRC_CONSUMER 2
#define BR_HID_SRC_DATA     3
#define BR_HID_SRC_VENDOR   4

/* 一个输入报告特征 */
typedef struct {
    bool     used;
    uint16_t def_handle;
    uint16_t val_handle;
    uint16_t cccd_handle;
    uint16_t ref_handle;     /* Report Reference 描述符 */
    uint8_t  report_id;
    uint8_t  report_type;      /* 1=Input 2=Output 3=Feature */
    uint8_t  role;           /* BR_ROLE_* */
    uint16_t bytes;
    bool     subscribed;
    uint16_t cccd_value;    /* last verified CCCD readback */
    bool     notify;         /* 特征属性里有没有 notify 位 */
    bool     other;          /* 不是标准 0x2A4D 报告，但同样能通知 */
    uint8_t  props;          /* 特征原始属性位（开麦控制报告要判可写） */
    uint16_t cc_last_usage;  /* Chromecast 按键：当前按着的那个键 */
    uint8_t  last[16];       /* 上次的数据，用于差分 */
    bool     has_last;
    int      raw_logged;     /* 大包（数据流）已记了多少条 */
    int      raw_total;      /* 大包一共收到多少条 */
} br_hid_report_t;

/* 单台设备的 HID 上下文（实现细节，见 br_hid.c） */
typedef struct br_hid_ctx br_hid_ctx_t;

/** 一个按键事件 */
typedef struct {
    uint8_t  src;        /* BR_HID_SRC_* */
    uint16_t usage;      /* HID usage */
    int8_t   peer_idx;   /* BLE peer context, -1 for synthetic events */
    bool     down;
} br_hid_key_t;

/** 按键事件回调（由 br_ble_core 接到协议层） */
typedef void (*br_hid_key_fn)(const br_hid_key_t *key, void *ctx);

/**
 * 小米语音键按/松（由键盘报告 0x3E 与 20 字节控制报告去重后上报）。
 * 开麦命令由 br_hid 自己写 Report ID 4，不依赖这个回调。
 */
typedef void (*br_hid_voice_fn)(int8_t peer_idx, bool down, void *ctx);

void br_hid_reset(void);
void br_hid_set_key_handler(br_hid_key_fn fn, void *ctx);
void br_hid_set_voice_handler(br_hid_voice_fn fn, void *ctx);

/**
 * ★ 硬件自检：造一个假的按键事件（会真的点灯、走完整的 PC 侧学习流程）★
 */
void br_hid_test_key(void);

/**
 * ★ 断链时清掉某台设备的 HID 上下文 ★
 *
 * 不清的话，上一次连接停在半路的上下文会继续驱动新一轮连接，
 * 拿上一轮的句柄去跑，读到的自然是空表。
 */
void br_hid_peer_reset(int peer_idx);

/**
 * ★ 允许这台设备订阅「数据通道」报告 ★
 *
 * 默认不订阅（CMCC v3 实测订了大数据通道会把链路顶断）。
 * 小米一代的语音音频就装在数据通道报告里，只能对那台放开。
 */
void br_hid_set_xiaomi(int peer_idx, bool on);

/**
 * 取证：读一个特征的值（内容打到串口日志）。
 * 用来找小米那台「开麦」开关在哪个特征里。
 */
bool br_hid_probe_read(int peer_idx, uint16_t handle);

/** 取证：往一个特征写一段数据。 */
bool br_hid_probe_write(int peer_idx, uint16_t handle,
                        const uint8_t *data, uint16_t len);

/**
 * 开始 HID 发现。
 *
 * 返回 true 表示已发起（后续由 br_hid_drive 推进）。
 */
bool br_hid_begin(int peer_idx, uint16_t conn_handle);

/**
 * 推进发现流程（**必须在回调之外调用**）。
 * NimBLE 不允许在过程回调里嵌套发起过程。
 */
void br_hid_drive(int peer_idx, uint16_t conn_handle);

/** Stream the discovered report table for diagnostics (read-only). */
bool br_hid_dump_reports(int peer_idx);

/** 收到输入报告通知 */
void br_hid_on_notify(int peer_idx, uint16_t attr_handle,
                      const uint8_t *data, uint16_t len);

#ifdef __cplusplus
}
#endif
