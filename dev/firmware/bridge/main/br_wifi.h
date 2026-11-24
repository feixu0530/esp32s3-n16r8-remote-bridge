/*
 * br_wifi - 开发板的 WiFi（STA）
 * ---------------------------------------------------------------------------
 * 手机通过 WiFi 连开发板，开发板把事件翻译成 USB HID。
 * 板子自己连路由器（STA），不依赖 PC 客户端在线。
 *
 * 配网流程：
 *   1. PC 客户端经 USB CDC 下发 WIFI ssid=... pass=...
 *   2. 板子存进 NVS（命名空间 "wifi"）并连接
 *   3. 连上后板子用 EV WIFI 把自己的 IP 上报，PC 客户端显示给用户
 *      （手机要连的就是这个 IP —— IDF 6.1 没有 mDNS 组件，没法用 xxx.local）
 *
 * 连不上时降级开热点：手机连 ESP32-Remote-XXXX 即可直接访问 192.168.4.1。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化 WiFi（不含连接：有凭据会自动连） */
void br_wifi_init(void);

/** 设置并保存 WiFi 凭据，然后立即连接（同名改密码，新的追加，最多记 6 个） */
bool br_wifi_set_credentials(const char *ssid, const char *pass);

/**
 * 把「记住的网络」列表报给 PC（DT WIFINET，每行一个，最后 OK WIFILIST）。
 * 换地方不用重新配网：开机按列表顺序试，哪个在场连哪个。
 */
void br_wifi_list_nets(void);

/** 删掉第 idx 个记住的网络（删的正是当前在用的就立刻换下一个） */
bool br_wifi_delete_net(int idx);

/** 断开当前连接（凭据仍保留） */
void br_wifi_disconnect(void);

/** 板子当前的 IP（没连上时返回空串） */
const char *br_wifi_ip(void);

/** 当前连接的 SSID（没连上时返回空串） */
const char *br_wifi_ssid(void);

/** 当前 RSSI（没连上时返回 0） */
int br_wifi_rssi(void);

/**
 * 扫描附近 AP 并把结果报给 PC（DT WIFIAP，每行一个）。
 *
 * ★ 为什么必须由固件来扫 ★
 *   ESP32-S3 只支持 2.4 GHz。路由器常常同时开着两个名字
 *   （"XXX-5G" 与 "XXX"），PC 上看着连着的那个若是 5 GHz 的，
 *   板子根本看不见 → 一直连不上，且毫无线索。
 *   让 PC 端列出「板子自己看得见的 AP」，就绕开了这个坑。
 */
void br_wifi_scan(void);

/**
 * 当前状态。
 *
 * 0 = 未连接 / 1 = 已连接 / 2 = 热点模式
 */
int br_wifi_state(void);

/**
 * 把状态上报给 PC 客户端（EV WIFI）。
 * 连接状态变化时自动调用一次。
 */
void br_wifi_report(void);

#ifdef __cplusplus
}
#endif
