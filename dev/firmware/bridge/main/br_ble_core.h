/*
 * br_ble_core - BLE 主机端（Central + Observer）
 * ---------------------------------------------------------------------------
 * 职责：
 *   1. 扫描并记录候选遥控器
 *   2. 连接、服务发现、特征发现
 *   3. 完整的 GATT 转储（诊断 / P0 探针）
 *   4. 多遥控器轮转连接
 *
 * ★ 来自 CMCC 实测的硬约束（改这个文件前先读）★
 *
 *   1. 扫描回调里绝对不能连接、不能停扫描。
 *      在 GAP 上下文里停扫描会把协议栈状态机卡住，
 *      之后 esp_ble_gattc_open 要等将近半分钟才发得出去（实测 31 秒）。
 *      → 本实现：回调只记录候选，连接动作一律回到主循环任务里做。
 *
 *   2. 扫描与连接必须严格串行。
 *      并行会互相抢控制器：扫描启动 EBUSY(15)、连接 EBUSY(2)/EEXIST(21)。
 *
 *   3. 用扫描时拿到的对象去连接，地址类型必然真实。
 *      配置里存的 addr_type 不可信（遥控器可能用随机地址）。
 *
 *   4. 连接参数必须钉住：itvl=24(30ms) / latency=50 / timeout=500。
 *      拒绝从机的改参请求，否则会被顶到 lat=99，音频流停摆。
 */
#pragma once

#include "br_types.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 初始化 NimBLE 主机 */
esp_err_t br_ble_init(void);

/**
 * 把 BLE 地址格式化成 "7E:A2:AA:BB:CC:02" 这样的显示串。
 *
 * ★ NimBLE 的地址是小端字节序 ★
 *   显示时要从 addr[5] 排到 addr[0]（也就是设备上看到的那串 MAC）。
 *   别的地方**不要**再手写一遍 snprintf —— 写反了 PC 端就按地址
 *   匹配不上遥控器（人话日志会没有名字、MAC 还会是反的）。
 */
void br_addr_to_str(const uint8_t *addr, char *out, size_t cap);

/* ------------------------------------------------------------------ 扫描 */
bool br_ble_scan_start(bool continuous);

/**
 * 开始一次**扫描会话**（PC 端点「扫描」按钮）。
 * 会话开始会清空候选表；会话内各轮扫描复用同一张表，
 * 这样设备的 SCAN_RSP（带名字）即使落在后面几轮也能补上名字。
 */
bool br_ble_scan_session_start(bool continuous);
void br_ble_scan_stop(void);
bool br_ble_scanning(void);
int  br_ble_candidate_count(void);

/* ------------------------------------------------------------------ 连接 */
bool br_ble_connect_addr(const uint8_t *addr, uint8_t addr_type, const char *name);
bool br_ble_connect_candidate(int cand_idx);
void br_ble_disconnect_peer(int idx);

int  br_ble_peer_count(void);
int  br_ble_connected_count(void);
const char *br_ble_peer_name(int idx);
bool br_ble_peer_addr(int idx, uint8_t *addr, uint8_t *type);
uint16_t br_ble_peer_conn_handle(int idx);

/**
 * ★ 已发现特征表的只读快照 ★
 *
 * GATT 发现完成后，chrs[] 里已经带着每个特征的 CCCD 和
 * Report Reference 句柄（核心发现流程用「整个服务范围」的
 * Find-Info 拿到，实测在 Chromecast / 小米遥控器上都稳）。
 *
 * HID 层直接读这张表就行 —— 不要再自己按单个特征算句柄区间去发
 * Find-Info：那条路在真机上会拿到 BLE_HS_EBADDATA(10)，
 * 于是 CCCD 一个都订阅不上，按键通知为零。
 *
 * 逐条取（不用一次拷 40 条），是为了不占 BLE 任务的栈。
 */
typedef struct {
    uint16_t def_handle;
    uint16_t val_handle;
    uint16_t cccd_handle;   /* 0 = 没有 CCCD */
    uint16_t ref_handle;    /* Report Reference，0 = 没有 */
    uint8_t  props;         /* BLE_GATT_CHR_F_* 位掩码 */
    uint8_t  uuid_type;     /* 16 / 32 / 128 */
    uint16_t uuid16;        /* 仅 uuid_type==16 时有意义 */
} br_chr_info_t;

int  br_ble_peer_char_count(int peer_idx);
bool br_ble_peer_char_at(int peer_idx, int i, br_chr_info_t *out);

/* -------------------------------------------------------------- 自动模式 */
/*
 * P0 探针用：扫到符合条件的目标就自动连上，连上后自动转储 GATT。
 */
void br_ble_set_auto(bool on);
void br_ble_set_auto_dump(bool on);

/**
 * WiFi 优先模式。
 *
 * ESP32-S3 单天线，BLE 扫描会打断 WiFi 的 4 次握手（reason=201）。
 * WiFi 连接期间打开它，暂停自动扫描；连上后关闭，恢复扫描。
 */
void br_ble_set_wifi_priority(bool on);
/** 目标名字子串（NULL=不限） */
void br_ble_set_target_name(const char *substr);

/* ---------------------------------------------------------------- 诊断 */
/** 转储某台已连接设备的全部服务与特征 */
void br_ble_gatt_dump(int peer_idx);

/** 重跑一遍 HID 订阅（取证用：打开数据通道订阅后要重订） */
bool br_ble_hid_resubscribe(int peer_idx);

/**
 * 枚举 BLE bond 表（已配对设备）。
 * 遥控器空闲时不广播，只能靠 bond 里的 identity address 主动连。
 * 返回已配对设备数。
 */
int br_ble_list_bonded(void);

/**
 * 开一个配对窗口：窗口内扫到**没登记过**的设备就自动收下
 * （落进 NVS 配对表并发 EV PAIR），PC 端不用再逐个发 PAIR。
 *
 * seconds <= 0 表示关闭窗口。
 */
bool br_ble_pair_window_start(int seconds);

/** 配对窗口还剩多少秒（0 = 没开窗） */
int br_ble_pair_window_remaining(void);
bool br_ble_pair_window_auto(void);

/**
 * PC 端发起配对：连上目标，NimBLE 自动完成配对并保存 bond。
 */
bool br_ble_pair(const uint8_t *addr, uint8_t addr_type, const char *name);

/**
 * 解除配对：断开 + 删 NVS 配对表条目 + 顺带清 bond。
 * idx 用 DT RM 行里的 idx（配对表位置），不是 bond 表下标。
 */
bool br_ble_unpair(int rem_idx);

/**
 * 列出已配对的遥控器（发 DT RM 行 + OK REMOTES）。
 * 数据源是 NVS 配对表，不是 BLE bond 表。
 */
int br_ble_list_remotes(void);

/** 手机刚发过事件 —— 暂停 BLE 扫描几秒，把射频让给 WiFi */
void br_ble_note_phone_activity(void);

/** 手机是否正在占用板子（扫描要不要让路） */
bool br_ble_phone_busy(void);

/**
 * ★ 唯一的「地址字符串 -> NimBLE 字节序」入口 ★
 * 解析 "AA:BB:CC:DD:EE:FF" 为打印顺序（NimBLE 内部小端），
 * 与 addr_to_str 成对，任何地方都不许另写一份。
 */
bool br_ble_addr_parse(const char *s, uint8_t out[6]);

/** 连接 bond 表里的第 idx 台设备 */
bool br_ble_connect_bonded(int idx);

/**
 * 自主重连的一步：轮转尝试已配对设备。
 * 遥控器空闲不广播，只能靠反复重试抓它。
 * 返回 1 表示这次发起了连接。
 */
int br_ble_autoreconnect_tick(void);

/**
 * 开启/关闭按键学习。
 * 开启后按下的第一个键会被上报为 LEARN 事件，然后自动退出学习。
 */
void br_ble_set_learn(int idx, bool on);

/** 打印内部状态（候选/连接/发现进度） */
void br_ble_dump_state(void);

#ifdef __cplusplus
}
#endif
