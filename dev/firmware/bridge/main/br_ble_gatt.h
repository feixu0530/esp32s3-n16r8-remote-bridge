#pragma once

/*
 * br_ble_gatt - 板子作为 BLE 外设，让手机 App（WebView 壳）直接连上来。
 *
 * 手机连上后：
 *   - 写 C2P_CTL / C2P_BIN → 板子解析成与 HTTP 时代相同的事件，
 *     交给 br_phone_event() 翻译成 USB HID；
 *   - 板子通过 P2C_EVT notify 推事件（voice_state 等）。
 *
 * 帧格式见 br_ble_gatt.c 顶部注释。
 */
#include <stdbool.h>
#include "esp_err.h"
#include "host/ble_hs.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 注册 GATT 服务。
 *
 * ★ 必须在 nimble_port_init() 之后、host 同步之前调用 ★
 *   ble_gatts_add_svcs 要在 host 起来之前登记，
 *   同步完成（on_sync）时服务表已经就绪。
 */
esp_err_t br_ble_gatt_init(void);

/**
 * host 同步完成后启动广播（在 br_ble_core.c 的 on_sync 里调用）。
 */
void br_ble_gatt_on_sync(void);
void br_ble_gatt_set_phone_name(const char *name);
/* 主动补报一次手机身份（客户端连上时问 INFO 就顺带推给他） */
void br_ble_gatt_report_phone(void);
/* 主动断开当前连接的手机 */
void br_ble_gatt_drop_phone(void);

/** 手机是否连上且订阅了事件通知 */
bool br_ble_gatt_phone_connected(void);
void br_ble_gatt_forget_phone(void);

/** 板子 → 手机：发一条事件（name + data JSON 文本） */
bool br_ble_gatt_phone_send(const char *name, const char *data);

/** GAP 事件回调（广播启动时注册给 ble_gap_adv_start） */
int br_ble_gatt_gap_event(struct ble_gap_event *event, void *arg);

#ifdef __cplusplus
}
#endif
