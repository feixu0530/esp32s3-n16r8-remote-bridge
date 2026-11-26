/*
 * br_hid - HID 输入报告订阅 + 按键解析
 *
 * ★ 状态机的关键设计 ★
 *
 * 1. NimBLE 不允许在过程回调里嵌套发起过程，所以整个流程必须
 *    「发起 → 等回调 → 再发起」地串起来：回调里只改 step/issued，
 *    真正的发起动作由主循环的 br_hid_drive() 做。
 *
 * 2. ★ 描述符不再自己去发现 ★
 *    GATT 主发现流程（br_ble_core）已经把每个特征的 CCCD 和
 *    Report Reference 句柄记在表里了，这里直接读表。
 *
 *    之前这里自己按「单个特征」算句柄区间发 Find-Info，
 *    真机（Google Chromecast 遥控器）上 7 个报告里 6 个直接返回
 *    BLE_HS_EBADDATA(10)，剩下的一个什么都没返回：
 *      没有任何 CCCD → 没有任何订阅 → 没有任何按键通知
 *      → 按键不亮灯、学习模式收不到键、语音键也没信号。
 */
#include "br_hid.h"

#include <string.h>
#include <stdio.h>

#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include <host/ble_hs.h>
#include <host/ble_gap.h>
#include <host/ble_gatt.h>
#include <host/ble_uuid.h>
#include <os/os_mbuf.h>

#include "br_proto.h"
#include "br_ble_core.h"   /* br_ble_peer_char_at() —— 复用已发现的特征表 */

static const char *TAG = "br_hid";

/* HID 特征（描述符的识别在 br_ble_core 的发现流程里做） */
#define UUID_REPORT      0x2A4D
#define UUID_REPORT_MAP  0x2A4B
#define UUID_BATTERY     0x2A19   /* 电量：拿来当链路保活的读对象 */
#define UUID_HID_CP      0x2A4C   /* HID Control Point：0x01 = 退出挂起 */
#define UUID_PROTOCOL_MODE 0x2A4E /* 0x01 = Report Protocol */

/* 发现步骤（每个步骤 = 发起一个 NimBLE 过程） */
enum {
    STEP_IDLE = 0,
    STEP_TABLE,        /* 从已发现的特征表里挑出 HID 特征（零往返） */
    STEP_REF,          /* 读 Report Reference（report id + 类型） */
    STEP_MAP,          /* 读 Report Map（拿角色） */
    STEP_PROTO,        /* 小米：读/写 Protocol Mode = Report */
    STEP_CP,           /* 小米：订阅前先 Exit Suspend */
    STEP_SUB,          /* 写 CCCD 订阅 */
    STEP_DONE,
};

typedef struct br_hid_ctx {
    bool     used;
    int      step;
    bool     issued;          /* 本步骤的过程已发起，等回调 */
    int      sub;             /* 处理第几个报告特征 */
    int      peer_idx;        /* 第几台遥控器（遥测用） */
    uint16_t conn_handle;     /* 链路句柄（取证命令要用） */
    int      sub_ok;          /* CCCD 写成功个数 */
    int      sub_fail;        /* CCCD 写失败个数 */

    uint16_t map_char_handle;
    uint16_t pending_attr;    /* 当前正在读的句柄 */
    bool     map_read;
    bool     map_tried;       /* Report Map 只尽力读一次 */
    bool     verify_tried;    /* CCCD 回读只做一次 */
    int      verify_index;    /* 逐条验证输入报告的 CCCD */
    uint16_t verify_val_handle;

    /*
     * ★ 特征表还没填好时的重试 ★
     *   连接刚建立时 br_hid_begin 可能跑在服务发现完成之前，
     *   这时表是空的。以前这里直接放弃（STEP_DONE），
     *   整条订阅流程就废了 —— 真机日志：
     *     EV HID phase=table total=0 reports=0 cccd=0
     *   之后再也不出现 phase=begin，零订阅 = 零通知 = 灯不亮。
     */
    int      table_try;
    int64_t  next_table_try_us;
    int64_t  issued_at_us;     /* 当前这一步的操作是什么时候发出去的 */

    /*
     * ★ 链路保活 ★
     *   见下面 STEP_DONE 的说明。
     */
    uint16_t ka_handle;          /* 电量特征（0x2A19）的值句柄 */
    uint16_t cp_handle;          /* HID 控制点（0x2A4C）的值句柄 */
    uint8_t  cp_props;           /* HID 控制点支持的写操作 */
    bool     cp_written;         /* 控制点只写一次 */
    uint16_t pm_handle;          /* Protocol Mode（0x2A4E）的值句柄 */
    uint8_t  pm_props;
    uint8_t  pm_value;
    int16_t  pm_write_status;    /* -1=未写；0..=ATT 状态 */
    int16_t  cp_status;
    bool     batt_logged;        /* 电量只打一次 */
    uint8_t  cc_last_usage;      /* Chromecast 按键：当前按着的那个键 */
    int64_t  next_ka_us;

    /*
     * ★ 小米开麦命令 ★
     *   TV 侧系统蓝牙栈（BleRemoteControllerService$AudioRCGattCallback）
     *   对 HID 服务的 Report ID 4 写 20 字节：
     *     [3]=0x80 [4]=0x01 开麦，[4]=0x00 关麦。
     *   不写这条命令，遥控器只发按键报告、永远不发音频。
     */
    uint16_t audio_enable_handle;
    uint8_t  audio_enable_props;
    bool     audio_enable_pending;
    bool     audio_enable_on;
    int64_t  audio_enable_retry_us;
    bool     voice_down;         /* 去重：键盘 0x3E / 20 字节控制报告 */

    /*
     * ★ 允许订阅数据通道 ★
     *   小米一代的语音音频装在 HID 的大包报告里，默认不订（会顶断链路）。
     */
    bool     xiaomi_mode;

    br_hidmap_t map;
    br_hid_report_t reports[BR_HID_MAX_REPORTS];
    int      report_count;

    uint8_t  map_buf[512];
    uint16_t map_len;
} br_hid_ctx_t;

/* 表为空时的重试节奏：最多等 BR_HID_TABLE_TRIES × BR_HID_TABLE_RETRY_US */
#define BR_HID_TABLE_TRIES     20
#define BR_HID_TABLE_RETRY_US  300000

/* 大包（语音数据流）最多记多少条 / 每条最多打多少字节 */
#define BR_HID_RAW_LOG_MAX     4
#define BR_HID_RAW_HEX_MAX     64

/* 链路保活的间隔：必须明显小于 supervision timeout（5 秒） */
#define BR_HID_KEEPALIVE_US    1000000

/*
 * ★ 单个 ATT 操作的超时 ★
 *
 * 实锤（真机日志）：读 Report Reference 描述符（handle 41）之后
 *   NimBLE: GATT procedure initiated: read; att_handle=41
 * 就再没有任何回调 —— 对端不回应这个描述符的读请求。
 * 状态机就一直卡在 issued=1 后面，订阅再也走不到，
 * 于是零订阅 = 零通知 = 按键不亮灯、也学不到键。
 *
 * 所以每个操作都必须有超时：超了就当这一步失败，跳过去继续下一步。
 */
#define BR_HID_OP_TIMEOUT_US   3000000

static br_hid_ctx_t s_ctx[8];
static br_hid_key_fn s_key_fn;
static void *s_key_ctx;
static br_hid_voice_fn s_voice_fn;
static void *s_voice_ctx;
static int8_t s_emit_peer_idx = -1;

void br_hid_reset(void)
{
    memset(s_ctx, 0, sizeof(s_ctx));
}

/**
 * ★ 断链时必须清掉这台设备的 HID 上下文 ★
 *
 * 不清会怎样（真机日志实锤）：
 *   上一次连接的上下文停在 STEP_TABLE，新连接一建立，
 *   br_hid_drive() 立刻拿**上一轮的句柄**去跑 pull_table()，
 *   特征表此时当然是空的 → 于是报
 *     EV HID phase=table total=0 reports=0 cccd=0
 *   而且这个空跑还会把状态机推进到 STEP_DONE。
 */
void br_hid_peer_reset(int peer_idx)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) {
        return;
    }
    memset(&s_ctx[peer_idx], 0, sizeof(s_ctx[peer_idx]));
}

void br_hid_set_key_handler(br_hid_key_fn fn, void *ctx)
{
    s_key_fn = fn;
    s_key_ctx = ctx;
}

void br_hid_set_voice_handler(br_hid_voice_fn fn, void *ctx)
{
    s_voice_fn = fn;
    s_voice_ctx = ctx;
}

void br_hid_set_xiaomi(int peer_idx, bool on)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) {
        return;
    }
    s_ctx[peer_idx].xiaomi_mode = on;
}

/* ------------------------------------------------------------------ 取证
 *
 * ★ 读/写任意特征的值 ★
 *   小米一代的语音音频不来的时候，需要看它那几路 Feature/Output 报告里
 *   是不是有「开麦」开关 —— 这两条命令就是干这个的。
 */
static int probe_read_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                         struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    uint16_t handle = (uint16_t)(uintptr_t)arg;
    uint16_t status = error ? error->status : 0;
    if (status != 0 || !attr || !attr->om) {
        ESP_LOGW(TAG, "PROBE 读 handle=%u 失败: status=%u", handle, status);
        return 0;
    }
    uint16_t len = OS_MBUF_PKTLEN(attr->om);
    struct os_mbuf *dup = os_mbuf_pullup(attr->om, len ? len : 1);
    if (!dup) {
        ESP_LOGW(TAG, "PROBE 读 handle=%u 失败: pullup 失败", handle);
        return 0;
    }
    const uint8_t *p = OS_MBUF_DATA(dup, uint8_t *);
    ESP_LOGI(TAG, "PROBE 读 handle=%u len=%u: ", handle, len);
    for (uint16_t i = 0; i < len && i < 64; i++) {
        ESP_LOGI(TAG, "%02X ", p[i]);
    }
    ESP_LOGI(TAG, "");
    return 0;
}

bool br_hid_probe_read(int peer_idx, uint16_t handle)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) {
        return false;
    }
    br_hid_ctx_t *c = &s_ctx[peer_idx];
    if (!c->used || !c->conn_handle) {
        return false;
    }
    int rc = ble_gattc_read(c->conn_handle, handle, probe_read_cb,
                            (void *)(uintptr_t)handle);
    ESP_LOGI(TAG, "PROBE 读 handle=%u rc=%d", handle, rc);
    return rc == 0;
}

static int probe_write_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                          struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    uint16_t handle = (uint16_t)(uintptr_t)arg;
    uint16_t status = error ? error->status : 0;
    ESP_LOGI(TAG, "PROBE 写 handle=%u status=%u", handle, status);
    return 0;
}

bool br_hid_probe_write(int peer_idx, uint16_t handle,
                        const uint8_t *data, uint16_t len)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) {
        return false;
    }
    br_hid_ctx_t *c = &s_ctx[peer_idx];
    if (!c->used || !c->conn_handle || !data || len == 0) {
        return false;
    }
    uint8_t props = 0;
    for (int i = 0; i < br_ble_peer_char_count(peer_idx); i++) {
        br_chr_info_t ci;
        if (br_ble_peer_char_at(peer_idx, i, &ci) && ci.val_handle == handle) {
            props = ci.props;
            break;
        }
    }
    bool no_rsp = (props & BLE_GATT_CHR_PROP_WRITE_NO_RSP) != 0 &&
                  (props & BLE_GATT_CHR_PROP_WRITE) == 0;
    int rc;
    if (no_rsp) {
        rc = ble_gattc_write_no_rsp_flat(c->conn_handle, handle, data, len);
    } else {
        rc = ble_gattc_write_flat(c->conn_handle, handle, data, len,
                                  probe_write_cb, (void *)(uintptr_t)handle);
    }
    ESP_LOGI(TAG, "PROBE 写 handle=%u len=%u no_rsp=%u rc=%d",
             handle, len, no_rsp, rc);
    return rc == 0;
}

static void emit_key(uint8_t src, uint16_t usage, bool down)
{
    if (!s_key_fn) {
        return;
    }
    br_hid_key_t k = { src, usage, s_emit_peer_idx, down };
    s_key_fn(&k, s_key_ctx);
}

/**
 * ★ 硬件自检：造一个假的按键事件 ★
 *
 * 用途：把「按键 → 板子灯 → PC 学到键」这条链路和
 * 「遥控器到底发不发通知」这件事分开验证 ——
 * 链路自检通过、遥控器仍然没动静，那就是对端不发；
 * 链路自检也不通过，那就是我们的代码问题。
 */
void br_hid_test_key(void)
{
    ESP_LOGI(TAG, "自检：发一个假的按键事件（usage=0x04）");
    s_emit_peer_idx = -1;
    emit_key(BR_HID_SRC_KEYBOARD, 0x04, true);
    /*
     * ★ 按住 150 ms 再松 ★
     *   以前按下和抬起在同一微秒里发完 —— LED 只有一瞬，
     *   人眼几乎看不到（用户实测：灯只是非常微弱地闪一下）。
     *   按正常按键的节奏停留一下，蓝色才看得清。
     */
    vTaskDelay(pdMS_TO_TICKS(150));
    emit_key(BR_HID_SRC_KEYBOARD, 0x04, false);
}

/* --------------------------------------------------------------- 回调 */

/** 回调统一在这里推进状态机：清 issued，指向下一步 */
static void step_next(br_hid_ctx_t *c, int next)
{
    c->issued = false;
    c->step = next;
}

/**
 * ★ HID 流程的 CDC 遥测 ★
 *
 * 这块板子的日志只能从 USB CDC 协议通道出来（没有串口可插），
 * 而「按了键没反应」这种问题必须知道状态机卡在哪一步。
 * 一次连接大约 12 行，不会淹掉协议通道。
 *
 *   EV HID idx=0 phase=reports n=7
 *   EV HID idx=0 phase=role id=1e role=4 bytes=8
 *   EV HID idx=0 phase=sub_done id=1e status=0
 *   EV HID idx=0 phase=done reports=7 ok=3 fail=0
 */
static void hid_ev(br_hid_ctx_t *c, const char *phase,
                   const char *k1, long v1,
                   const char *k2, long v2,
                   const char *k3, long v3)
{
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "idx", c->peer_idx);
    br_kv_add(&kv, "phase", phase);
    if (k1) {
        br_kv_add_int(&kv, k1, v1);
    }
    if (k2) {
        br_kv_add_int(&kv, k2, v2);
    }
    if (k3) {
        br_kv_add_int(&kv, k3, v3);
    }
    br_proto_send_ev("HID", &kv);
}

/**
 * 保活读的回调：顺便把电量值打出来（看遥控器是不是在正常状态）。
 */
static int ka_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                 struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    c->issued = false;
    uint16_t status = error ? error->status : 0;
    if (status == 0 && attr && attr->om) {
        struct os_mbuf *dup = os_mbuf_pullup(attr->om, 1);
        if (dup && OS_MBUF_PKTLEN(dup) >= 1) {
            const uint8_t *p = OS_MBUF_DATA(dup, uint8_t *);
            uint8_t v = p[0];
            if (!c->batt_logged) {
                c->batt_logged = true;
                ESP_LOGI(TAG, "电量读到了: %u%%", v);
            }
        }
    } else if (!c->batt_logged) {
        c->batt_logged = true;
        ESP_LOGW(TAG, "电量读失败: status=%u", status);
    }
    return 0;
}

/**
 * HID 控制点写的回调：只要把 issued 放掉。
 */
static int cp_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                 struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    uint16_t status = error ? error->status : 0;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    ESP_LOGI(TAG, "HID 控制点写完: status=%u", status);
    c->issued = false;
    return 0;
}

/**
 * ★ 回读 CCCD 的回调：看对端到底有没有打开通知 ★
 */
static int verify_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                     struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    uint16_t status = error ? error->status : 0;
    c->issued = false;

    if (status != 0 || !attr || !attr->om) {
        ESP_LOGW(TAG, "回读 CCCD 失败: status=%u", status);
        hid_ev(c, "verify_err", "status", status, NULL, 0, NULL, 0);
        return 0;
    }
    uint16_t len = OS_MBUF_PKTLEN(attr->om);
    struct os_mbuf *dup = os_mbuf_pullup(attr->om, len ? len : 1);
    if (!dup || len < 2) {
        hid_ev(c, "verify_short", "len", len, NULL, 0, NULL, 0);
        return 0;
    }
    const uint8_t *p = OS_MBUF_DATA(dup, uint8_t *);
    uint16_t val = (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
    ESP_LOGI(TAG, "CCCD 回读: handle=%u 值=0x%04X（0x0001=通知）",
             c->pending_attr, val);
    for (int i = 0; i < c->report_count; i++) {
        if (c->reports[i].cccd_handle == c->pending_attr) {
            c->reports[i].cccd_value = val;
            break;
        }
    }
    hid_ev(c, "verify", "cccd", c->pending_attr, "val", val, NULL, 0);
    return 0;
}

static int pm_write_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       struct ble_gatt_attr *attr, void *arg);

static int post_map_step(const br_hid_ctx_t *c)
{
    if (c->xiaomi_mode) {
        if (c->pm_handle) {
            return STEP_PROTO;
        }
        if (c->cp_handle) {
            return STEP_CP;
        }
    }
    return STEP_SUB;
}

static int post_proto_step(const br_hid_ctx_t *c)
{
    return (c->xiaomi_mode && c->cp_handle) ? STEP_CP : STEP_SUB;
}

static void pm_write_start(br_hid_ctx_t *c)
{
    static const uint8_t report_mode[1] = {0x01};
    bool no_rsp = (c->pm_props & BLE_GATT_CHR_PROP_WRITE_NO_RSP) != 0 &&
                  (c->pm_props & BLE_GATT_CHR_PROP_WRITE) == 0;
    if (no_rsp) {
        int rc = ble_gattc_write_no_rsp_flat(c->conn_handle, c->pm_handle,
                                             report_mode, sizeof(report_mode));
        c->pm_write_status = (int16_t)rc;
        ESP_LOGI(TAG, "Protocol Mode 写 0x01 no_rsp rc=%d", rc);
        step_next(c, post_proto_step(c));
        return;
    }
    c->issued = true;
    c->issued_at_us = esp_timer_get_time();
    int rc = ble_gattc_write_flat(c->conn_handle, c->pm_handle, report_mode,
                                  sizeof(report_mode), pm_write_cb, c);
    if (rc != 0) {
        c->issued = false;
        c->pm_write_status = (int16_t)rc;
        step_next(c, post_proto_step(c));
    }
}

static int pm_read_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                      struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    uint16_t status = error ? error->status : 0;
    c->issued = false;
    if (status == 0 && attr && attr->om) {
        uint16_t len = OS_MBUF_PKTLEN(attr->om);
        struct os_mbuf *dup = os_mbuf_pullup(attr->om, len ? len : 1);
        if (dup && len >= 1) {
            const uint8_t *p = OS_MBUF_DATA(dup, uint8_t *);
            c->pm_value = p[0];
        }
    }
    ESP_LOGI(TAG, "Protocol Mode 读: status=%u value=%u", status, c->pm_value);
    pm_write_start(c);
    return 0;
}

static int pm_write_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    uint16_t status = error ? error->status : 0;
    c->issued = false;
    c->pm_write_status = (int16_t)status;
    ESP_LOGI(TAG, "Protocol Mode 写 0x01: status=%u", status);
    step_next(c, post_proto_step(c));
    return 0;
}

static int cp_pre_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                     struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    uint16_t status = error ? error->status : 0;
    c->issued = false;
    c->cp_status = (int16_t)status;
    ESP_LOGI(TAG, "HID Exit Suspend（订阅前）: status=%u", status);
    step_next(c, STEP_SUB);
    return 0;
}

static void cp_pre_start(br_hid_ctx_t *c)
{
    static const uint8_t exit_suspend[1] = {0x01};
    bool no_rsp = (c->cp_props & BLE_GATT_CHR_PROP_WRITE_NO_RSP) != 0 &&
                  (c->cp_props & BLE_GATT_CHR_PROP_WRITE) == 0;
    c->cp_written = true;
    if (no_rsp) {
        int rc = ble_gattc_write_no_rsp_flat(c->conn_handle, c->cp_handle,
                                             exit_suspend, sizeof(exit_suspend));
        c->cp_status = (int16_t)rc;
        ESP_LOGI(TAG, "HID Exit Suspend（订阅前）no_rsp rc=%d", rc);
        step_next(c, STEP_SUB);
        return;
    }
    c->issued = true;
    c->issued_at_us = esp_timer_get_time();
    int rc = ble_gattc_write_flat(c->conn_handle, c->cp_handle, exit_suspend,
                                  sizeof(exit_suspend), cp_pre_cb, c);
    if (rc != 0) {
        c->issued = false;
        c->cp_status = (int16_t)rc;
        step_next(c, STEP_SUB);
    }
}

/**
 * 读回调：Report Reference(2 字节) 或 Report Map。
 *
 * ★ 为什么又在读描述符了 ★
 *   v0.9.7 去掉它，是因为那时候链路**还没加密**，
 *   在未加密状态下读描述符对端不应答（read 发起后再无回调），
 *   而且它占住 GATT 过程槽 → 后面的 CCCD 写全 EBUSY。
 *   现在 open_channels() 是在 ENC_CHANGE(status=0) 之后才调的，
 *   链路是加密的 —— 值得再试一次。
 *   而且这一步是**关键**：Report Reference 给出每条报告的
 *   report id 和 report type（1=Input），
 *   我们据此**只订阅 Input 报告**（跟安卓盒子/CMCC 一致）。
 */
static int attr_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                   struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    uint16_t status = error ? error->status : 0;

    if (status != 0) {
        ESP_LOGW(TAG, "读属性失败: status=%u（attr=%u）", status,
                 c->pending_attr);
        hid_ev(c, "read_err", "status", status, "attr", c->pending_attr,
               NULL, 0);
        c->issued = false;
        if (c->step == STEP_REF) {
            c->sub++;
            step_next(c, STEP_REF);
        } else {
            step_next(c, post_map_step(c));
        }
        return 0;
    }
    if (!attr || !attr->om) {
        c->issued = false;
        return 0;
    }

    uint16_t len = OS_MBUF_PKTLEN(attr->om);
    if (len == 0) {
        c->issued = false;
        return 0;
    }
    /*
     * ★ pullup 的长度必须用实际长度 ★
     *   之前写的是 sizeof(c->map_buf)（512），
     *   而 Report Map 通常只有一两百字节 ——
     *   pullup 要求 mbuf 里有这么多连续字节，不够就返回 NULL，
     *   于是我们**静默地**什么都没解析到（连日志都没有）。
     */
    uint16_t want = (len > sizeof(c->map_buf)) ? (uint16_t)sizeof(c->map_buf)
                                                : len;
    struct os_mbuf *dup = os_mbuf_pullup(attr->om, want);
    if (!dup) {
        ESP_LOGW(TAG, "pullup 失败（len=%u）", len);
        c->issued = false;
        return 0;
    }
    uint16_t n = OS_MBUF_PKTLEN(dup);
    if (n > want) {
        n = want;
    }

    /* Report Reference：2 字节 —— [0]=report id, [1]=report type(1=Input) */
    if (len == 2 && n >= 2) {
        const uint8_t *p = OS_MBUF_DATA(dup, uint8_t *);
        for (int i = 0; i < c->report_count; i++) {
            br_hid_report_t *r = &c->reports[i];
            if (r->ref_handle == c->pending_attr) {
                r->report_id = p[0];
                r->report_type = p[1];
                ESP_LOGI(TAG, "report id=0x%02X type=%u (val handle %u)",
                         r->report_id, p[1], r->val_handle);
                hid_ev(c, "ref", "id", r->report_id, "type", p[1], NULL, 0);
                break;
            }
        }
        c->issued = false;
        c->sub++;
        step_next(c, STEP_REF);
        return 0;
    }
    /* Report Map */
    memcpy(c->map_buf, OS_MBUF_DATA(dup, uint8_t *), n);
    c->map_len = (uint16_t)n;
    /*
     * ★ 把原始字节打出来 ★
     *   解析出来的角色全是 NONE(role=0)，说明我们的解析器跟这只遥控器的
     *   Report Map 对不上。先看原始字节长什么样，再决定解析规则。
     */
    ESP_LOGI(TAG, "report map 原始 %u 字节:", n);
    for (int off = 0; off < (int)n; off += 16) {
        char line[80];
        int len = 0;
        len += snprintf(line + len, sizeof(line) - len, "  %03X:", off);
        for (int k = 0; k < 16 && off + k < (int)n; k++) {
            len += snprintf(line + len, sizeof(line) - len, " %02X",
                            c->map_buf[off + k]);
        }
        ESP_LOGI(TAG, "%s", line);
    }
    int count = br_hidmap_parse(&c->map, c->map_buf, c->map_len);
    ESP_LOGI(TAG, "report map: %u bytes, %d reports", n, count);
    hid_ev(c, "map", "bytes", n, "ids", count, NULL, 0);

    /* ★ 用 Map 给每条报告定角色 ★ */
    for (int i = 0; i < c->report_count; i++) {
        br_hid_report_t *r = &c->reports[i];
        uint16_t bytes = 0;
        r->role = br_hidmap_role(&c->map, r->report_id, &bytes);
        r->bytes = bytes;
        ESP_LOGI(TAG, "  report id=0x%02X role=%d bytes=%u",
                 r->report_id, r->role, bytes);
        hid_ev(c, "role", "id", r->report_id, "role", r->role, NULL, 0);
    }

    c->map_read = true;
    c->issued = false;
    step_next(c, post_map_step(c));
    return 0;
}

/** 写回调：CCCD 订阅写完之后推进状态 */
static int write_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                    struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    uint16_t status = error ? error->status : 0;

    /* 出错时 att_handle 指向被拒绝的那个属性，能告诉我们是不是加密不够 */
    uint16_t att = error ? (uint16_t)error->att_handle : 0;

    if (status != 0) {
        ESP_LOGE(TAG, "cccd write failed: %u (att_handle=%u)", status, att);
        c->sub_fail++;
        if (c->sub < c->report_count) {
            c->reports[c->sub].subscribed = false;
        }
    } else {
        c->sub_ok++;
    }
    hid_ev(c, "sub_done", "status", status, "handle", att, NULL, 0);
    c->sub++;
    step_next(c, STEP_SUB);
    return 0;
}

/* --------------------------------------------------------------- 驱动 */

bool br_hid_begin(int peer_idx, uint16_t conn_handle)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) {
        return false;
    }
    /*
     * xiaomi_mode 是「这台设备是谁」决定的，不是这一轮连接决定的 ——
     * 重开一轮订阅不能把它清掉（否则小米那台的语音通道就没了）。
     */
    bool allow = s_ctx[peer_idx].xiaomi_mode;
    memset(&s_ctx[peer_idx], 0, sizeof(s_ctx[peer_idx]));
    s_ctx[peer_idx].xiaomi_mode = allow;
    s_ctx[peer_idx].used = true;
    s_ctx[peer_idx].peer_idx = peer_idx;
    s_ctx[peer_idx].conn_handle = conn_handle;
    s_ctx[peer_idx].step = STEP_TABLE;
    s_ctx[peer_idx].pm_write_status = -1;
    s_ctx[peer_idx].cp_status = -1;
    ESP_LOGI(TAG, "开始 HID 订阅");
    hid_ev(&s_ctx[peer_idx], "begin", "conn", conn_handle, NULL, 0, NULL, 0);
    return true;
}

/**
 * ★ 从已发现的特征表里取 HID 需要的东西 ★
 *
 * 零往返。核心 GATT 发现流程已经填好每个特征的 cccd / ref 句柄，
 * 这里只是把 0x2A4D（Report）和 0x2A4B（Report Map）挑出来。
 */
static void pull_table(br_hid_ctx_t *c)
{
    int total = br_ble_peer_char_count(c->peer_idx);
    int with_cccd = 0;

    c->report_count = 0;
    c->map_char_handle = 0;

    for (int i = 0; i < total; i++) {
        br_chr_info_t ci;
        if (!br_ble_peer_char_at(c->peer_idx, i, &ci) || ci.uuid_type != 16) {
            continue;
        }
        if (ci.uuid16 == UUID_REPORT && c->report_count < BR_HID_MAX_REPORTS) {
            br_hid_report_t *r = &c->reports[c->report_count++];
            memset(r, 0, sizeof(*r));
            r->used = true;
            r->def_handle = ci.def_handle;
            r->val_handle = ci.val_handle;
            r->cccd_handle = ci.cccd_handle;
            r->ref_handle = ci.ref_handle;
            r->props = ci.props;
            /* ★ 能不能通知，看特征自己的属性 ★ */
            r->notify = (ci.props & 0x10) != 0;
            if (ci.cccd_handle) {
                with_cccd++;
            }
        } else if (ci.uuid16 == UUID_REPORT_MAP) {
            c->map_char_handle = ci.val_handle;
        } else if (ci.uuid16 == UUID_BATTERY) {
            /* 电量特征留作链路保活读的对象 */
            c->ka_handle = ci.val_handle;
        } else if (ci.uuid16 == UUID_HID_CP) {
            /* HID 控制点：有些设备挂起后要主机写 0x01 才会开始发输入报告 */
            c->cp_handle = ci.val_handle;
            c->cp_props = ci.props;
        } else if (ci.uuid16 == UUID_PROTOCOL_MODE) {
            c->pm_handle = ci.val_handle;
            c->pm_props = ci.props;
            ESP_LOGI(TAG, "Protocol Mode handle=%u props=0x%02x",
                     ci.val_handle, ci.props);
        } else if ((ci.props & 0x10) && ci.cccd_handle &&
                   c->report_count < BR_HID_MAX_REPORTS) {
            /*
             * ★ 不是 0x2A4D、但同样「能通知 + 有 CCCD」的特征也订上 ★
             *
             * 之前只订 0x2A4D（标准 Report），结果漏掉了
             *   uuid=2A33 props=26 cccd=69   ← HID 服务里另一个通知特征
             * 对端该发不发、我们这边零通知，很可能就差这一条。
             * 宁可多订（多几个无害的通知），也不能漏订。
             */
            br_hid_report_t *r = &c->reports[c->report_count++];
            memset(r, 0, sizeof(*r));
            r->used = true;
            r->def_handle = ci.def_handle;
            r->val_handle = ci.val_handle;
            r->cccd_handle = ci.cccd_handle;
            r->ref_handle = ci.ref_handle;
            r->props = ci.props;
            r->notify = true;
            r->other = true;
            ESP_LOGI(TAG, "额外订阅候选: uuid=0x%04X handle=%u cccd=%u",
                     ci.uuid16, ci.val_handle, ci.cccd_handle);
            with_cccd++;
        }
    }

    hid_ev(c, "table", "total", total, "reports", c->report_count,
           "cccd", with_cccd);

    if (c->report_count == 0) {
        /*
         * ★ 表还没填好 —— 别直接放弃 ★
         *
         * br_hid_begin 有可能跑在服务发现完成之前（连接刚建立），
         * 这时表是空的。以前这里直接 step_next(STEP_DONE)，
         * 整条订阅流程就废了：真机日志里只有
         *   EV HID phase=table total=0 reports=0 cccd=0
         * 之后再没有 phase=begin —— 零订阅 = 零通知 = 灯不亮、键学不到。
         *
         * 现在改成有限次重试，等发现把表填好。
         */
        int64_t now = esp_timer_get_time();
        if (now < c->next_table_try_us) {
            return;                 /* 还没到重试时间，先歇着 */
        }
        c->table_try++;
        if (c->table_try <= BR_HID_TABLE_TRIES) {
            c->next_table_try_us = now + BR_HID_TABLE_RETRY_US;
            hid_ev(c, "table_wait", "total", total, "try", c->table_try,
                   NULL, 0);
            return;                 /* 留在 STEP_TABLE */
        }
        ESP_LOGW(TAG, "等了 %d 次还是 0 个 Report 特征，放弃订阅",
                 c->table_try);
        hid_ev(c, "no_report", "total", total, "try", c->table_try,
               NULL, 0);
        c->sub = 0;
        step_next(c, STEP_DONE);
        return;
    }

    c->table_try = 0;
    c->sub = 0;
    /*
     * ★ 流程：读 Report Reference → 读 Report Map → 写 CCCD ★
     *
     * v0.9.7 把前两步去掉了（那时链路还没加密，读描述符对端不应答，
     * 还占住 GATT 过程槽）。现在 open_channels() 是在加密成功之后才调的，
     * 链路是加密的 —— 可以再按 CMCC / 安卓盒子的顺序来一遍：
     *   - Report Reference 给出每条报告的 report id 和类型（1=Input）
     *   - Report Map 给出每个 report id 的角色（键盘/消费者/数据）
     *   - 只订阅 Input 且不是数据通道的报告（跟盒子一致）
     *
     * 每一步都有超时（BR_HID_OP_TIMEOUT_US），
     * 对端不应答就跳过这一步，不影响后面的订阅。
     */
    step_next(c, STEP_REF);
}

/*
 * ★ 找小米的开麦控制报告 ★
 *
 * TV 侧系统蓝牙栈按 HID 服务里的 Report 序号取 instance 3。真机报告表：
 *   rid=1 键盘, rid=2 输出, rid=3 输入, rid=4 控制（可写、不通知）。
 * 正常路径按 Report Reference 的 rid=4 找；描述符没读到时，
 * 退化成「第 4 条 0x2A4D」——和 TV 的 instance 3 同一条。
 */
static void pick_audio_enable(br_hid_ctx_t *c)
{
    if (!c->xiaomi_mode || c->audio_enable_handle) {
        return;
    }
    const uint8_t wr = BLE_GATT_CHR_PROP_WRITE |
                       BLE_GATT_CHR_PROP_WRITE_NO_RSP;
    br_hid_report_t *fallback = NULL;
    int report_no = 0;

    for (int i = 0; i < c->report_count; i++) {
        br_hid_report_t *r = &c->reports[i];
        if (r->other) {
            continue;
        }
        if (report_no == 3) {
            fallback = r;
        }
        report_no++;
        if (r->report_id == 4 && (r->props & wr)) {
            c->audio_enable_handle = r->val_handle;
            c->audio_enable_props = r->props;
            break;
        }
    }
    if (!c->audio_enable_handle && fallback && (fallback->props & wr)) {
        c->audio_enable_handle = fallback->val_handle;
        c->audio_enable_props = fallback->props;
    }
    if (c->audio_enable_handle) {
        ESP_LOGI(TAG, "小米开麦控制 handle=%u props=0x%02X",
                 c->audio_enable_handle, c->audio_enable_props);
        hid_ev(c, "au_ctrl", "handle", c->audio_enable_handle,
               "props", c->audio_enable_props, NULL, 0);
    } else {
        ESP_LOGW(TAG, "没找到可写的小米开麦控制报告");
    }
}

/**
 * 语音键按下/松开（键盘 0x3E 与 20 字节控制报告都会调，这里去重）。
 * 同时安排写开麦/关麦命令；真正的写由 br_hid_drive 在空闲时发起。
 */
static void xiaomi_voice_event(br_hid_ctx_t *c, bool down)
{
    if (c->voice_down == down) {
        return;
    }
    c->voice_down = down;
    c->audio_enable_on = down;
    c->audio_enable_pending = true;
    c->audio_enable_retry_us = 0;
    ESP_LOGI(TAG, "小米语音键 %s", down ? "按下" : "松开");
    hid_ev(c, "voice", "down", down ? 1 : 0, NULL, 0, NULL, 0);
    if (s_voice_fn) {
        s_voice_fn(c->peer_idx, down, s_voice_ctx);
    }
}

static int audio_enable_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                           struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    br_hid_ctx_t *c = (br_hid_ctx_t *)arg;
    uint16_t status = error ? error->status : 0;
    c->issued = false;
    if (status != 0) {
        c->audio_enable_pending = true;
        c->audio_enable_retry_us = esp_timer_get_time() + 200000;
        ESP_LOGW(TAG, "小米开麦命令写失败: status=%u", status);
        hid_ev(c, "au_err", "status", status, NULL, 0, NULL, 0);
        return 0;
    }
    ESP_LOGI(TAG, "小米开麦命令已发: %s",
             c->audio_enable_on ? "开" : "关");
    hid_ev(c, "au_ok", "on", c->audio_enable_on ? 1 : 0, NULL, 0, NULL, 0);
    return 0;
}

void br_hid_drive(int peer_idx, uint16_t conn_handle)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) {
        return;
    }
    br_hid_ctx_t *c = &s_ctx[peer_idx];
    if (!c->used) {
        return;
    }
    /* 本步骤的过程还没回来 —— 等 */
    if (c->issued) {
        /*
         * ★ 等太久就当它失败了 ★
         *   对端不回应某个 ATT 操作时，NimBLE 不会回调，
         *   没有超时的话整个订阅流程就死在这儿。
         */
        int64_t now = esp_timer_get_time();
        if (now - c->issued_at_us > BR_HID_OP_TIMEOUT_US) {
            ESP_LOGW(TAG, "step %d 的操作超时（%d ms 没回调），跳过",
                     c->step, (int)(BR_HID_OP_TIMEOUT_US / 1000));
            hid_ev(c, "op_timeout", "step", c->step, "attr",
                   c->pending_attr, NULL, 0);
            c->issued = false;
            if (c->step == STEP_REF) {
                c->sub++;
                step_next(c, STEP_REF);
            } else if (c->step == STEP_MAP) {
                c->sub = 0;
                step_next(c, STEP_SUB);
            } else if (c->step == STEP_PROTO) {
                step_next(c, post_proto_step(c));
            } else if (c->step == STEP_CP) {
                step_next(c, STEP_SUB);
            } else if (c->step == STEP_SUB) {
                c->sub++;
                step_next(c, STEP_SUB);
            } else {
                step_next(c, STEP_DONE);
            }
        }
        return;
    }

    int rc = 0;

    switch (c->step) {
    case STEP_TABLE:
        pull_table(c);
        return;

    case STEP_REF: {
        /* 把所有报告的 ref 描述符读完 */
        while (c->sub < c->report_count) {
            br_hid_report_t *r = &c->reports[c->sub];
            if (!r->ref_handle) {
                c->sub++;
                continue;
            }
            break;
        }
        if (c->sub >= c->report_count) {
            c->sub = 0;
            step_next(c, STEP_MAP);
            return;
        }
        br_hid_report_t *r = &c->reports[c->sub];
        c->pending_attr = r->ref_handle;
        c->issued = true;
        c->issued_at_us = esp_timer_get_time();
        rc = ble_gattc_read(conn_handle, r->ref_handle, attr_cb, c);
        break;
    }

    case STEP_MAP: {
        if (!c->map_char_handle) {
            ESP_LOGW(TAG, "没有 Report Map 特征，跳过角色判定");
            hid_ev(c, "no_map", NULL, 0, NULL, 0, NULL, 0);
            c->sub = 0;
            step_next(c, STEP_SUB);
            return;
        }
        c->pending_attr = c->map_char_handle;
        c->issued = true;
        c->issued_at_us = esp_timer_get_time();
        rc = ble_gattc_read(conn_handle, c->map_char_handle, attr_cb, c);
        break;
    }

    case STEP_PROTO: {
        c->pending_attr = c->pm_handle;
        c->issued = true;
        c->issued_at_us = esp_timer_get_time();
        rc = ble_gattc_read(conn_handle, c->pm_handle, pm_read_cb, c);
        break;
    }

    case STEP_CP: {
        cp_pre_start(c);
        return;
    }

    case STEP_SUB: {
        if (c->sub >= c->report_count) {
            step_next(c, STEP_DONE);
            ESP_LOGI(TAG, "HID 设置完成：%d 个报告（成功 %d 失败 %d）",
                     c->report_count, c->sub_ok, c->sub_fail);
            /* ★ 把订上的每个报告都打出来 ★
               下次「按了键没反应」时，一眼能看出到底订上了哪些特征。 */
            for (int i = 0; i < c->report_count; i++) {
                const br_hid_report_t *r = &c->reports[i];
                ESP_LOGI(TAG, "  report[%d] val=%u cccd=%u ref=%u notify=%d sub=%d",
                         i, r->val_handle, r->cccd_handle, r->ref_handle,
                         r->notify ? 1 : 0, r->subscribed ? 1 : 0);
            }
            if (c->xiaomi_mode) {
                hid_ev(c, "init", "proto_val", c->pm_value,
                       "pm_status", c->pm_write_status,
                       "cp_status", c->cp_status);
            }
            hid_ev(c, "done", "reports", c->report_count,
                   "ok", c->sub_ok, NULL, 0);
            return;
        }
        br_hid_report_t *r = &c->reports[c->sub];
        if (!r->cccd_handle) {
            hid_ev(c, "no_cccd", "val", r->val_handle, NULL, 0, NULL, 0);
            c->sub++;
            return;
        }
        /*
         * ★ 只订阅「能通知」的报告 ★
         *   没有 notify 属性的报告（输出/特性报告）写 CCCD 只会被拒。
         */
        if (!r->notify) {
            hid_ev(c, "not_notify", "val", r->val_handle, NULL, 0, NULL, 0);
            c->sub++;
            return;
        }
        /*
         * ★ Report Reference 读到了类型就按类型筛 ★
         *   type: 1=Input（要订）、2=Output、3=Feature。
         *   读不到（type 仍是 0）就照订 —— 多订一条最多多点噪声，
         *   少订一条就是那个键永远学不到。
         */
        if (r->report_type != 0 && r->report_type != 1 && !c->xiaomi_mode) {
            /*
             * ★ 例外：小米一代 ★
             *   它有一批特征：属性带 notify，Report Reference 却报 type=3
             *   （Feature）。按规范 Feature 报告不会通知 —— 但小米就用它们
             *   传语音音频。所以对小米这台不按 type 筛：带 notify + 有 CCCD 就订。
             */
            hid_ev(c, "not_input", "id", r->report_id, "type",
                   r->report_type, NULL, 0);
            c->sub++;
            return;
        }
        /*
         * ★ 数据通道不订 ★
         *   Map 里认出来的 DATA 角色（遥控器的语音/大数据通道）
         *   订了会把链路顶断（CMCC v3 实测），按键报告也就跟着上不来。
         *   ★ 例外：小米一代的语音音频就装在数据通道报告里 ★
         *     那台必须订（br_hid_set_xiaomi 只对小米放开）。
         */
        if (r->role == BR_ROLE_DATA && !c->xiaomi_mode) {
            ESP_LOGI(TAG, "  report id=0x%02X 是数据通道，不订阅",
                     r->report_id);
            hid_ev(c, "skip_data", "id", r->report_id, NULL, 0, NULL, 0);
            c->sub++;
            return;
        }
        /*
         * ★ subscribed 必须在这里置位 ★
         *   br_hid_on_notify() 只认 subscribed 的报告，
         *   之前没有任何地方写这个标志 —— CCCD 就算写成功了，
         *   通知上来也会被那句 `!r->subscribed` 直接丢掉。
         */
        r->subscribed = true;
        static const uint8_t enable_notify[2] = {0x01, 0x00};
        c->issued = true;
        c->issued_at_us = esp_timer_get_time();
        rc = ble_gattc_write_flat(conn_handle, r->cccd_handle,
                                   enable_notify, sizeof(enable_notify),
                                   write_cb, c);
        if (rc != 0) {
            ESP_LOGE(TAG, "write cccd failed: %d", rc);
            hid_ev(c, "sub_err", "rc", rc, "cccd", r->cccd_handle, NULL, 0);
            r->subscribed = false;
            c->sub_fail++;
            c->issued = false;
            c->sub++;
        }
        /* 成功则等 write_cb 推进 */
        return;
    }

    case STEP_DONE: {
        /*
         * ★ 小米开麦/关麦 ★
         *   TV 侧对 Report ID 4（HID 服务 instance 3）写 20 字节：
         *     00 00 00 80 01 00…  开麦
         *     00 00 00 80 00 00…  关麦
         *   这里在语音键状态变化后，等 GATT 空闲时补发。
         */
        pick_audio_enable(c);
        if (c->audio_enable_pending && !c->audio_enable_handle) {
            c->audio_enable_pending = false;
        }
        if (c->audio_enable_pending && c->audio_enable_handle) {
            int64_t now = esp_timer_get_time();
            if (now >= c->audio_enable_retry_us) {
                uint8_t pkt[20] = {0};
                pkt[3] = 0x80;
                pkt[4] = c->audio_enable_on ? 0x01 : 0x00;
                bool no_rsp =
                    (c->audio_enable_props & BLE_GATT_CHR_PROP_WRITE_NO_RSP) != 0 &&
                    (c->audio_enable_props & BLE_GATT_CHR_PROP_WRITE) == 0;
                int arc;
                if (no_rsp) {
                    arc = ble_gattc_write_no_rsp_flat(conn_handle,
                                                      c->audio_enable_handle,
                                                      pkt, sizeof(pkt));
                    c->issued = false;
                } else {
                    c->issued = true;
                    c->issued_at_us = now;
                    arc = ble_gattc_write_flat(conn_handle,
                                               c->audio_enable_handle,
                                               pkt, sizeof(pkt),
                                               audio_enable_cb, c);
                }
                if (arc == 0) {
                    c->audio_enable_pending = false;
                    if (no_rsp) {
                        ESP_LOGI(TAG, "小米开麦命令已发(no_rsp): %s",
                                 c->audio_enable_on ? "开" : "关");
                    }
                } else {
                    c->issued = false;
                    c->audio_enable_retry_us = now + 200000;
                }
                return;
            }
            return;
        }

        /*
         * ★ HID 控制点：写一次 0x01（Exit Suspend）★
         *
         * 有些 HID 设备（尤其是 Google 系的）上电是「挂起」状态，
         * 主机不告诉它「我在线」，它就不发输入报告。
         * 这一步是**试验性**的：写失败也不影响别的。
         */
        if (!c->cp_written && c->cp_handle) {
            c->cp_written = true;
            static const uint8_t exit_suspend[1] = {0x01};
            bool no_rsp = (c->cp_props & BLE_GATT_CHR_PROP_WRITE_NO_RSP) != 0 &&
                          (c->cp_props & BLE_GATT_CHR_PROP_WRITE) == 0;
            int crc;
            if (no_rsp) {
                crc = ble_gattc_write_no_rsp_flat(conn_handle, c->cp_handle,
                                                   exit_suspend, sizeof(exit_suspend));
                c->issued = false;
            } else {
                c->issued = true;
                c->issued_at_us = esp_timer_get_time();
                crc = ble_gattc_write_flat(conn_handle, c->cp_handle,
                                           exit_suspend, sizeof(exit_suspend),
                                           cp_cb, c);
            }
            if (crc != 0) {
                c->issued = false;
                ESP_LOGW(TAG, "写 HID 控制点失败(no_rsp=%u): %d", no_rsp, crc);
            }
            return;
        }

        /*
         * ★ 订阅完回读一次 CCCD：确认对端真的把通知打开了 ★
         *
         * 写 CCCD 返回 status=0 只说明「对端收下了这个写请求」，
         * 到底有没有真的打开通知，得把描述符读回来看值。
         * 值应该是 0x0001（通知）或 0x0002（指示）。
         * 读回来是 0x0000 就说明订阅**根本没生效**。
         */
        if (!c->verify_tried) {
            while (c->verify_index < c->report_count) {
                br_hid_report_t *r = &c->reports[c->verify_index++];
                if (!r->subscribed || !r->cccd_handle) continue;
                c->pending_attr = r->cccd_handle;
                c->verify_val_handle = r->val_handle;
                c->issued = true;
                c->issued_at_us = esp_timer_get_time();
                int vrc = ble_gattc_read(conn_handle, r->cccd_handle,
                                         verify_cb, c);
                if (vrc == 0) return;
                c->issued = false;
                ESP_LOGW(TAG, "回读 CCCD handle=%u 失败: %d",
                         r->cccd_handle, vrc);
            }
            c->verify_tried = true;
            return;
        }

        /*
         * ★ 链路保活 ★
         *
         * 遥控器只有按键时才发数据，平时一声不吭。
         * 而链路的 supervision timeout 只有 2.56 秒（NimBLE 默认值，
         * 见 br_ble_core.c 的 request_conn_params 注释），
         * 于是「连上了、订阅也写好了、用户还没按键」这三秒里链路就死了，
         * 用户真正按键的时候我们早就不在线了。
         *
         * 所以每 1 秒读一次电量特征：不管读回来是电量还是
         * 「Insufficient Authentication」错误，都是对端的包，
         * 都能把 supervision timer 续上。
         */
        if (!c->ka_handle) {
            return;
        }
        int64_t now = esp_timer_get_time();
        if (now < c->next_ka_us) {
            return;
        }
        c->next_ka_us = now + BR_HID_KEEPALIVE_US;
        c->issued = true;
        c->issued_at_us = now;
        int rc = ble_gattc_read(conn_handle, c->ka_handle, ka_cb, c);
        if (rc != 0) {
            c->issued = false;
            /* 读不了就算了，别刷日志 */
        }
        return;
    }

    default:
        return;
    }

    if (rc != 0) {
        ESP_LOGE(TAG, "step %d 发起失败: %d", c->step, rc);
        c->issued = false;
        /*
         * ★ 失败的处理必须和「超时」的处理一致 ★
         *
         *   以前这里只认 STEP_SUB，其他步骤一律 step_next(STEP_DONE) ——
         *   读 Report Reference 失败一次（rc=6，链路正忙），
         *   整条订阅流程就跳到结束：**一个 CCCD 都没写**，
         *   遥控器的按键报告永远收不到
         *   （真机症状：按遥控器，板子灯不亮、日志里一条 EV KEY 都没有）。
         *
         *   Report Reference / Report Map 只是「用来判角色」的，
         *   读不到照样能订 —— 解析时还有「按载荷长度猜」的兜底。
         */
        if (c->step == STEP_SUB) {
            c->sub++;
            step_next(c, STEP_SUB);
        } else if (c->step == STEP_REF) {
            c->sub++;              /* 这个报告的引用读不到，跳过它 */
            step_next(c, STEP_REF);
        } else if (c->step == STEP_MAP) {
            c->sub = 0;
            step_next(c, post_map_step(c)); /* 映射读不到也照样初始化/订阅 */
        } else if (c->step == STEP_PROTO) {
            pm_write_start(c);
        } else if (c->step == STEP_CP) {
            step_next(c, STEP_SUB);
        } else {
            step_next(c, STEP_DONE);
        }
    }
}

/* --------------------------------------------------------------- 解析 */

/*
 * ★ Chromecast 遥控器的普通按键报告 ★
 *
 * 来源（已验证过的实现）：
 *   reference/winmic-src/apps/windows/rc003/discussions/chromecast-remote-integration.md
 *     「普通按下通知位于 ATT 属性 0x0029，报告固定为 8 字节：
 *       首字节是按键码，后七字节为零；松开报告为 8 字节全零。」
 *   同项目 remote_layout.py 的 CHROMECAST_REPORT_CODES / CHROMECAST_CONSUMER_USAGES
 *
 * 也就是说：**报告里没有 report id 字节**，第 1 个字节直接就是按键码。
 * 以前我们按标准 HID 键盘报告去解（字节 0 当修饰键、字节 1~6 当键码），
 * 键码会整体错位一格 —— 这是「收到了也学不出来」的一个原因。
 *
 * 这里把按键码**翻译成标准 HID consumer usage**，
 * 这样 PC 侧的映射表、快捷键、显示全是标准用法，不用为这只遥控器单独一套。
 */
typedef struct {
    uint8_t  code;     /* 遥控器自己的按键码 */
    uint16_t usage;    /* 翻译成的标准 HID consumer usage */
} chromecast_key_t;

static const chromecast_key_t k_chromecast_keys[] = {
    { 0x01, 0x019E },  /* 电源 */
    { 0x03, 0x0042 },  /* 上 */
    { 0x04, 0x0043 },  /* 下 */
    { 0x05, 0x0044 },  /* 左 */
    { 0x06, 0x0045 },  /* 右 */
    { 0x07, 0x0041 },  /* 确定 */
    { 0x08, 0x00E2 },  /* 静音 */
    { 0x0A, 0x0223 },  /* 首页 */
    { 0x0B, 0x0224 },  /* 返回 */
    { 0x0C, 0x00E9 },  /* 音量+ */
    { 0x0D, 0x00EA },  /* 音量- */
    { 0x0E, 0x0077 },  /* YouTube */
    { 0x0F, 0x0078 },  /* Netflix */
    { 0x11, 0x0189 },  /* 输入源 */
};

static bool chromecast_lookup(uint8_t code, uint16_t *usage)
{
    for (size_t i = 0; i < sizeof(k_chromecast_keys) / sizeof(k_chromecast_keys[0]); i++) {
        if (k_chromecast_keys[i].code == code) {
            *usage = k_chromecast_keys[i].usage;
            return true;
        }
    }
    return false;
}

static void parse_keyboard(br_hid_report_t *r, const uint8_t *data, uint16_t len)
{
    if (len < 2) {
        return;
    }
    const uint8_t *kbd = data;

    /* 修饰键变化也当作事件（Shift/Ctrl 单独用） */
    for (int bit = 0; bit < 4; bit++) {
        bool now = (kbd[0] >> bit) & 1;
        bool was = (r->last[0] >> bit) & 1;
        if (now != was) {
            static const uint16_t mod_usage[4] = {
                0xE1, 0xE0, 0xE2, 0xE3   /* lshift lctrl lalt lwin */
            };
            emit_key(BR_HID_SRC_KEYBOARD, mod_usage[bit], now);
        }
    }

    /*
     * ★ 键码从哪一字节开始 ★
     *
     *   标准键盘报告 = [修饰键][保留][键码×6] → 键码从第 2 字节开始
     *   7 字节的变体  = [修饰键][键码×6]      → 键码从第 1 字节开始
     *
     * 之前一律从第 1 字节读 —— 对小米那台（键码在第 2 字节）虽然取值碰巧对，
     * 但第 1 个「保留字节」一旦非 0 就会变成一个假按键，
     * 第 7 字节的键码也会漏掉。
     */
    int base = (len >= 8) ? 2 : 1;
    for (int i = 0; i < 6 && (size_t)(i + base) < len; i++) {
        uint8_t cur = kbd[i + base];
        uint8_t prev = r->last[i + base];
        if (cur != prev) {
            if (prev) {
                emit_key(BR_HID_SRC_KEYBOARD, prev, false);
            }
            if (cur) {
                emit_key(BR_HID_SRC_KEYBOARD, cur, true);
            }
        }
    }
}

static void parse_consumer(br_hid_report_t *r, const uint8_t *data, uint16_t len)
{
    if (len < 2) {
        return;
    }
    uint16_t cur = (uint16_t)(data[0] | ((uint16_t)data[1] << 8));
    uint16_t prev = (uint16_t)(r->last[0] | ((uint16_t)r->last[1] << 8));
    if (cur != prev) {
        if (prev) {
            emit_key(BR_HID_SRC_CONSUMER, prev, false);
        }
        if (cur) {
            emit_key(BR_HID_SRC_CONSUMER, cur, true);
        }
    }
}

bool br_hid_dump_reports(int peer_idx)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) return false;
    br_hid_ctx_t *c = &s_ctx[peer_idx];
    if (!c->used) return false;
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "idx", peer_idx);
    br_kv_add_int(&kv, "count", c->report_count);
    br_kv_add_int(&kv, "step", c->step);
    br_proto_send_ev("HIDTABLE", &kv);
    for (int i = 0; i < c->report_count; i++) {
        const br_hid_report_t *r = &c->reports[i];
        br_kv_init(&kv);
        br_kv_add_int(&kv, "idx", peer_idx);
        br_kv_add_int(&kv, "n", i);
        br_kv_add_int(&kv, "def", r->def_handle);
        br_kv_add_int(&kv, "val", r->val_handle);
        br_kv_add_int(&kv, "cccd", r->cccd_handle);
        br_kv_add_int(&kv, "cccdv", r->cccd_value);
        br_kv_add_int(&kv, "ref", r->ref_handle);
        br_kv_add_int(&kv, "rid", r->report_id);
        br_kv_add_int(&kv, "type", r->report_type);
        br_kv_add_int(&kv, "role", r->role);
        br_kv_add_int(&kv, "bytes", r->bytes);
        br_kv_add_int(&kv, "notify", r->notify);
        br_kv_add_int(&kv, "sub", r->subscribed);
        br_kv_add_int(&kv, "other", r->other);
        br_proto_send_dt("HIDREPORT", &kv);
    }
    return true;
}

/**
 * ★ 收到一条 HID 输入报告 ★
 *
 * 三件事：
 *   1. 认角色（键盘 / 消费者控制）——
 *      优先用 Report Map 按 report id 查；
 *      Map 读不到就按载荷长度猜（9/8 字节=键盘，3/2 字节=消费者）。
 *   2. **剥掉 report id 字节** ——
 *      HID 输入报告的第 1 个字节是 report id（当报告带 id 时），
 *      不剥掉的话键码全错位（CMCC 的 decodeKeyboard 就是先剥再解）。
 *   3. 跟上一帧比，出按下/抬起事件。
 */
void br_hid_on_notify(int peer_idx, uint16_t attr_handle,
                      const uint8_t *data, uint16_t len)
{
    if (peer_idx < 0 || peer_idx >= (int)(sizeof(s_ctx) / sizeof(s_ctx[0]))) {
        return;
    }
    s_emit_peer_idx = (int8_t)peer_idx;
    br_hid_ctx_t *c = &s_ctx[peer_idx];
    if (!c->used) {
        return;
    }

    for (int i = 0; i < c->report_count; i++) {
        br_hid_report_t *r = &c->reports[i];
        if (r->val_handle != attr_handle || !r->subscribed) {
            continue;
        }
        uint16_t n = len;
        if (n > sizeof(r->last)) {
            n = sizeof(r->last);
        }

        /*
         * ★ 小米语音键的主判据 ★
         *   TV 侧系统蓝牙栈看 Report ID 1 的键盘报告：
         *     按下 value[2]==0x3E；松开 value[2]==0 && value[0]==0。
         *   20 字节控制报告也会报同一件事，统一走 xiaomi_voice_event 去重。
         */
        if (c->xiaomi_mode && r->report_id == 1 && n >= 3) {
            if (data[2] == 0x3E) {
                xiaomi_voice_event(c, true);
                return;
            }
            if (data[2] == 0 && data[0] == 0 && c->voice_down) {
                xiaomi_voice_event(c, false);
                return;
            }
        }

        /*
         * ★ 大包 = 语音音频一类的高频数据流 ★
         *
         * 小米一代的语音就装在 60 字节的 HID 报告里，133 帧/秒。
         * 这种包绝不能按「按键位图」去解（480 个位会炸出 480 个假按键事件），
         * 也不能每帧都往协议行里灌（USB CDC 会被刷爆）。
         *
         * 判据：**长 + 不是按键类角色**。
         *   键盘/消费者/手柄角色哪怕长也要照常解（手柄报告本来就长）。
         *
         * 做法：只把前若干条原样打到串口日志里（取证用），
         * 协议侧的 HIDRAW 同样只发前若干条。
         */
        bool raw_stream = (len > 16) &&
                          (r->role == BR_ROLE_VENDOR || r->role == BR_ROLE_DATA ||
                           r->role == BR_ROLE_NONE || r->role == BR_ROLE_GENERIC);
        if (raw_stream) {
            r->raw_total++;
            if (r->raw_logged < BR_HID_RAW_LOG_MAX) {
                r->raw_logged++;
                char hx[BR_HID_RAW_HEX_MAX * 2 + 1];
                uint16_t hl = (len > BR_HID_RAW_HEX_MAX) ? BR_HID_RAW_HEX_MAX : len;
                for (uint16_t k = 0; k < hl; k++) {
                    snprintf(&hx[k * 2], 3, "%02X", data[k]);
                }
                hx[hl * 2] = '\0';
                ESP_LOGI(TAG,
                         "RAW h=%u b0=%02X role=%u len=%u: %s",
                         attr_handle, data[0], r->role, len, hx);
            } else if ((r->raw_total % 100) == 0) {
                ESP_LOGI(TAG, "RAW h=%u 已收到 %d 包（只记前 %d 包）",
                         attr_handle, r->raw_total, BR_HID_RAW_LOG_MAX);
            }
        }

        /*
         * ★ 只把前 N 条大包发给协议侧 ★
         *   raw_logged 到上限后不再增长；以前用 <= 判断会变成
         *   「24 之后每一帧都发」，133 帧/秒 × 3 条通道把 USB CDC 拖死，
         *   主机任务堵在串口上，最终 ACL 缓冲耗尽（ACL buf alloc failed）。
         */
        if (!raw_stream || r->raw_total <= BR_HID_RAW_LOG_MAX) {
            br_kv_t raw;
            br_kv_init(&raw);
            br_kv_add_int(&raw, "idx", peer_idx);
            br_kv_add_int(&raw, "handle", attr_handle);
            br_kv_add_int(&raw, "n", len);
            br_kv_add_hex(&raw, "hex", data, len);
            br_proto_send_ev("HIDRAW", &raw);
        }
        if (n == 0) {
            return;
        }

        /*
         * 小米语音键的 20 字节控制报告也是 Feature：先在 HIDRAW 之后识别，
         * 再让下面的非 Input 守卫原样保存并返回。只按报告元数据识别，
         * 不依赖某个连接的 ATT 句柄，也不把控制报告伪装成 ATVV。
         */
        /*
         * 20 字节控制包：byte0=01/00，其余全 0。
         * 必须校验“其余全 0”，否则其它 20 字节 Feature 数据
         * 会被误报成语音按下/松开。
         */
        if (c->xiaomi_mode && r->report_type != 1 &&
            len == 20 && data && data[0] <= 1) {
            bool ctrl20 = true;
            for (uint16_t k = 1; k < len; k++) {
                if (data[k] != 0) {
                    ctrl20 = false;
                    break;
                }
            }
            if (ctrl20) {
                xiaomi_voice_event(c, data[0] == 1);
            }
        }

        /* 小米的语音控制/音频是 Feature 报告，首字节不能当键盘修饰键。 */
        if (c->xiaomi_mode && r->report_type != 1) {
            memcpy(r->last, data,
                   (len > sizeof(r->last)) ? sizeof(r->last) : len);
            r->has_last = true;
            return;
        }

        uint8_t rid = data[0];
        uint8_t role = r->role;
        uint16_t payload_bytes = 0;    /* Map 里说的载荷长度（不含 id 字节） */

        if (c->map_read) {
            uint8_t mrole = br_hidmap_role(&c->map, rid, &payload_bytes);
            if (mrole != BR_ROLE_NONE && mrole != BR_ROLE_GENERIC) {
                role = mrole;
            }
        }
        if (role == BR_ROLE_NONE || role == BR_ROLE_GENERIC) {
            if (n == 9 || n == 8) {
                role = BR_ROLE_KEYBOARD;
            } else if (n == 3 || n == 2) {
                role = BR_ROLE_CONSUMER;
            } else {
                role = BR_ROLE_GENERIC;
            }
        }
        r->role = role;

        const uint8_t *p = data;
        uint16_t plen = n;

        /*
         * ★ Chromecast 风格报告：8 字节、首字节按键码、后七字节全 0 ★
         *
         * 这份报告里**没有 report id 字节**，第一个字节直接就是按键码。
         * 按标准键盘报告去解会整体错位一格。
         * 判据：长度 8 且第 2~8 字节全 0（标准键盘报告的第 2 字节是键码，
         * 按键时必然非 0，所以不会误判）。
         *
         * ★ 小米那台不能走这条分支 ★
         *   小米的按键也是 8 字节标准键盘报告（[修饰][保留][键码×6]），
         *   松手时的全 0 包同样「后七字节全 0」——
         *   走进这条分支，抬起事件就被吞了（灯一直常亮、手势也判不出来）。
         */
        bool tail_zero = true;
        for (uint16_t k = 1; k < n; k++) {
            if (data[k] != 0) {
                tail_zero = false;
                break;
            }
        }
        if (n == 8 && tail_zero && !c->xiaomi_mode) {
            uint8_t code = data[0];
            if (code == 0) {
                /* 松开：8 字节全零 */
                if (r->cc_last_usage) {
                    emit_key(BR_HID_SRC_CONSUMER, r->cc_last_usage, false);
                    r->cc_last_usage = 0;
                }
            } else {
                uint16_t usage = 0;
                if (chromecast_lookup(code, &usage)) {
                    if (r->cc_last_usage) {
                        emit_key(BR_HID_SRC_CONSUMER, r->cc_last_usage, false);
                    }
                    r->cc_last_usage = usage;
                    emit_key(BR_HID_SRC_CONSUMER, usage, true);
                    ESP_LOGI(TAG, "Chromecast 按键码 0x%02X -> usage 0x%04X",
                             code, usage);
                    hid_ev(c, "cc_key", "code", code, "usage", usage, NULL, 0);
                } else {
                    ESP_LOGW(TAG, "未知按键码 0x%02X", code);
                    hid_ev(c, "cc_unknown", "code", code, NULL, 0, NULL, 0);
                }
            }
            memcpy(r->last, data, n);
            r->has_last = true;
            return;
        }

        /*
         * ★ 数据流不进按键解码 ★
         *
         * 语音音频那种长包按位图解，会把每个比特的变化都当成一次按键 ——
         * 一帧 60 字节就是 480 个假事件，直接把协议行和手势引擎打爆。
         * 这里只留日志（上面已经打过），不产生任何按键事件。
         */
        if (raw_stream) {
            memcpy(r->last, data,
                   (len > sizeof(r->last)) ? sizeof(r->last) : len);
            r->has_last = true;
            return;
        }

        /*
         * Report Reference 是 report ID 是否存在的权威信息。
         * Chromecast 键盘输入报告在表中为 6 字节，通知线上带 ID 后为 7；
         * 旧逻辑只剥 9/3 字节报告，导致 ID=1 被误解析成修饰键。
         * vRemoter 同样先检查/剥离匹配的 report ID。
         */
        bool strip = r->report_id != 0 && n == r->bytes + 1 &&
                     data[0] == r->report_id;
        if (strip) {
            p = data + 1;
            plen = n - 1;
        }

        switch (role) {
        case BR_ROLE_KEYBOARD:
            parse_keyboard(r, p, plen);
            break;
        case BR_ROLE_CONSUMER:
            parse_consumer(r, p, plen);
            break;
        case BR_ROLE_GAMEPAD:
        case BR_ROLE_GENERIC:
        case BR_ROLE_VENDOR:
            for (uint16_t b = 0; b < plen; b++) {
                for (int bit = 0; bit < 8; bit++) {
                    bool now = (p[b] >> bit) & 1;
                    bool was = (r->last[b] >> bit) & 1;
                    if (now != was) {
                        emit_key(BR_HID_SRC_DATA,
                                 (uint16_t)(b * 8 + bit), now);
                    }
                }
            }
            break;
        default:
            break;
        }

        memcpy(r->last, p, plen);
        r->has_last = true;
        return;
    }
}
