/*
 * br_ble_gatt - 板子作为 BLE 外设（Peripheral），让手机 App 直接连上来
 * ---------------------------------------------------------------------------
 * ★ 为什么要有这个 ★
 *   ESP32-S3 的 WiFi 和 BLE 共用一根天线。
 *   手机走 WiFi（板子跑 HTTP）时，WiFi 射频常开，
 *   BLE 只能抢到很少时隙 —— 多台蓝牙遥控器一起用就撑不住：
 *   第二台连上后第一台的链路报 LL Response Timeout（HCI 0x22）被断。
 *   （实测：配了第二台遥控器，第一台按键就失效。）
 *
 *   手机也改走 BLE 之后，板子不需要 WiFi，射频全给 BLE，
 *   手机 + 多台遥控器可以同时在线。
 *
 * ★ 帧格式（4 字节头 + 载荷）★
 *   [0] type  1=JSON 控制帧  2=紧凑指针帧  3=ADPCM 语音  4=裸 PCM
 *   [1] evid  type=2 时区分事件；其它填 0
 *   [2] flags bit0=first  bit1=last（分帧重组用）
 *   [3] len   载荷字节数
 *
 *   type=1 的载荷就是 HTTP 时代 POST /io 的 body：
 *     {"ev":"tap_key","data":{"key":"enter"}}
 *   —— 解析代码、事件语义零改动，只换了传输层。
 *
 *   type=2（指针类高频事件，省带宽）：
 *     evid=1 move    : int16 dx, int16 dy（小端）
 *     evid=2 click   : u8 button, u8 count
 *     evid=3 scroll  : int16 dy
 *     evid=4 drag_start / 5 drag_end / 6 mid_down / 7 mid_up（无载荷）
 */
#include "br_ble_gatt.h"
#include "br_ckeys.h"
#include "br_led.h"
#include "nvs.h"
#include "br_ble_core.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include "br_phone.h"
#include "br_proto.h"
#include "br_web.h"
#include "br_json_min.h"

static const char *TAG = "br_gatt";

/* ------------------------------------------------------------ UUID */

/* 6d6f766f-0001-4d4f-9a00-00000000abf0 = "movo"... 自定义 128bit 服务 */
static const ble_uuid128_t s_svc_uuid = BLE_UUID128_INIT(
    0xf0, 0xab, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9a,
    0x4f, 0x4d, 0x01, 0x00, 0x6f, 0x76, 0x6f, 0x6d);
/* C2P_CTL：手机 → 板子，控制事件（Write Without Response） */
static const ble_uuid128_t s_ctl_uuid = BLE_UUID128_INIT(
    0xf1, 0xab, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9a,
    0x4f, 0x4d, 0x01, 0x00, 0x6f, 0x76, 0x6f, 0x6d);
/* C2P_BIN：手机 → 板子，语音（Write Without Response） */
static const ble_uuid128_t s_bin_uuid = BLE_UUID128_INIT(
    0xf2, 0xab, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9a,
    0x4f, 0x4d, 0x01, 0x00, 0x6f, 0x76, 0x6f, 0x6d);
/* P2C_EVT：板子 → 手机，事件（Notify） */
static const ble_uuid128_t s_evt_uuid = BLE_UUID128_INIT(
    0xf3, 0xab, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9a,
    0x4f, 0x4d, 0x01, 0x00, 0x6f, 0x76, 0x6f, 0x6d);

#define PHONE_DEV_NAME "remote-bridge"

/* ------------------------------------------------------------ 状态 */

static uint8_t  s_own_addr_type;
static uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;

/*
 * ★ 手机的真实身份（名字 + 地址）★
 *
 *   以前手机在 PC 端只是一条「手机（临时遥控器）」的伪条目，
 *   地址写死 50:50:50:50:50:50，用户根本看不出连的是哪台手机。
 *   这里在手机连上来时：
 *     1) 从连接描述符里取对端地址（有身份地址就取身份地址，否则取当前 RPA）
 *     2) 读手机 GATT 的设备名（0x1800/0x2A00）——安卓/iOS 都会暴露
 *   然后通过 EV PHONE 上报给 PC，配对列表和手机页显示真实信息。
 */
static char    s_phone_name[48];
static uint8_t s_phone_addr[6];
static uint8_t s_phone_id[6];
static bool    s_phone_have_addr;
static bool    s_phone_has_id;

static void addr_to_str_local(const uint8_t *a, char *out, size_t cap)
{
    snprintf(out, cap, "%02X:%02X:%02X:%02X:%02X:%02X",
             a[5], a[4], a[3], a[2], a[1], a[0]);
}

static void report_phone(void);

/* 让板子主动断开当前手机（PC 端「删除这台手机」用） */
void br_ble_gatt_drop_phone(void)
{
    if (s_conn == BLE_HS_CONN_HANDLE_NONE) {
        return;
    }
    ESP_LOGI(TAG, "主动断开手机 handle=%u", s_conn);
    s_phone_name[0] = 0;
    report_phone();
    ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
}

void br_ble_gatt_report_phone(void)
{
    report_phone();
}

static void report_phone(void)
{
    char addr_s[24] = "";
    char id_s[24] = "";
    if (s_phone_have_addr) {
        addr_to_str_local(s_phone_addr, addr_s, sizeof(addr_s));
        if (s_phone_has_id) {
            addr_to_str_local(s_phone_id, id_s, sizeof(id_s));
        }
    }
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add(&kv, "addr", addr_s);
    br_kv_add(&kv, "id", id_s);
    br_kv_add(&kv, "name", s_phone_name);
    br_kv_add_int(&kv, "conn", s_conn != BLE_HS_CONN_HANDLE_NONE ? 1 : 0);
    br_proto_send_ev("PHONE", &kv);
    ESP_LOGI(TAG, "手机身份: name=%s addr=%s id=%s",
             s_phone_name[0] ? s_phone_name : "?",
             addr_s[0] ? addr_s : "?", id_s[0] ? id_s : "?");
}

/* 手机 App 主动上报的机型名（比读 GATT 0x2A00 可靠：安卓对未配对的 central 常常不给读） */
void br_ble_gatt_set_phone_name(const char *name)
{
    if (!name) {
        return;
    }
    size_t o = 0;
    for (const char *p = name; *p && o < sizeof(s_phone_name) - 1; p++) {
        if ((unsigned char)*p >= 0x20) {
            s_phone_name[o++] = *p;
        }
    }
    s_phone_name[o] = 0;
    report_phone();
}

static int phone_name_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                         struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)arg;
    if (error->status != 0 || !attr || !attr->om) {
        ESP_LOGI(TAG, "读设备名失败 status=%d（等 App 上报机型名）",
                 error ? error->status : -1);
        return 0;
    }
    uint8_t buf[64];
    uint16_t len = OS_MBUF_PKTLEN(attr->om);
    if (len >= sizeof(buf)) {
        len = sizeof(buf) - 1;
    }
    if (os_mbuf_copydata(attr->om, 0, len, buf) != 0) {
        return 0;
    }
    size_t o = 0;
    for (size_t i = 0; i < len && o < sizeof(s_phone_name) - 1; i++) {
        if (buf[i] >= 0x20 && buf[i] < 0x7F) {
            s_phone_name[o++] = (char)buf[i];
        }
    }
    s_phone_name[o] = 0;
    report_phone();
    return 0;
}
static uint16_t s_evt_handle;          /* P2C_EVT 的属性句柄（notify 用） */
static bool     s_notify_on;
/*
 * ★ 手机准入 ★
 *   PC 端「删除手机」后置位：除非重新打开配对窗口，否则手机连上就踢。
 *   避免「删了又立刻被手机抢回连接」，也避免陌生人手机误连。
 */
static bool     s_phone_blocked;
static bool     s_deny_pending;      /* 已连上但被拉黑：等手机订阅后再告知并踢掉 */
static esp_timer_handle_t s_deny_timer;
static volatile bool s_active;         /* 手机连上且订阅了事件 */

/* 语音重组缓冲（type=3 分帧） */
static uint8_t  s_voice[4096];
static size_t   s_voice_len;

static bool ready(void)
{
    return s_active && s_conn != BLE_HS_CONN_HANDLE_NONE;
}

static void deny_timeout_cb(void *arg)
{
    (void)arg;
    if (s_deny_pending && s_conn != BLE_HS_CONN_HANDLE_NONE) {
        s_deny_pending = false;
        ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
    }
}

void br_ble_gatt_forget_phone(void)
{
    s_phone_blocked = true;
    nvs_handle_t h;
    if (nvs_open("phone", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, "blocked", 1);
        nvs_commit(h);
        nvs_close(h);
    }
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
    }
}

bool br_ble_gatt_phone_connected(void)
{
    return ready();
}

/* ------------------------------------------------------------ 广播 */

static void start_advertising(void)
{
    struct ble_hs_adv_fields fields;
    memset(&fields, 0, sizeof(fields));
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;

    /*
     * 广播里只放 128bit 服务 UUID（18 字节）+ flags（3 字节）——
     * 手机按 service 过滤就能找到板子，不用扫全表。
     */
    fields.uuids128 = &s_svc_uuid;
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;

    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv fields rc=%d", rc);
        return;
    }

    /* 名字放 scan response（广播包塞不下） */
    struct ble_hs_adv_fields rsp;
    memset(&rsp, 0, sizeof(rsp));
    rsp.name = (const uint8_t *)PHONE_DEV_NAME;
    rsp.name_len = strlen(PHONE_DEV_NAME);
    rsp.name_is_complete = 1;
    rc = ble_gap_adv_rsp_set_fields(&rsp);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv rsp rc=%d", rc);
    }

    struct ble_gap_adv_params advp;
    memset(&advp, 0, sizeof(advp));
    advp.conn_mode = BLE_GAP_CONN_MODE_UND;
    advp.disc_mode = BLE_GAP_DISC_MODE_GEN;
    advp.itvl_min = BLE_GAP_ADV_ITVL_MS(150);
    advp.itvl_max = BLE_GAP_ADV_ITVL_MS(300);

    rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &advp,
                           br_ble_gatt_gap_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv start rc=%d", rc);
    } else {
        ESP_LOGI(TAG, "广播中：%s", PHONE_DEV_NAME);
    }
}

/* ------------------------------------------------------------ 收帧 */

static void handle_json(const uint8_t *p, size_t len)
{
    char body[512];
    if (len >= sizeof(body)) {
        ESP_LOGW(TAG, "json 帧太长 %u", (unsigned)len);
        return;
    }
    memcpy(body, p, len);
    body[len] = '\0';

    /*
     * ★ 载荷就是 HTTP 时代 POST /io 的 body ★
     *   {"ev":"tap_key","data":{...}}
     *   所以这里只是把「谁解析」从 http_io 换成这里，
     *   事件语义、br_phone_event 的实现都不用动。
     */
    char ev[48];
    if (!br_json_get_str(body, "ev", ev, sizeof(ev))) {
        ESP_LOGW(TAG, "json 帧缺 ev: %.40s", body);
        return;
    }
    static char data[384];
    br_json_get_raw(body, "data", data, sizeof(data));
    br_phone_event(ev, data[0] ? data : NULL);
}

/*
 * ★ JSON 分帧（type=5）★
 *
 * 一帧只能装 ~240 字节。中文 3 字节/字，一句长一点就超了 ——
 * 手机端以前直接整条丢弃（现象：英文能打，中文多了就没反应）。
 * 现在手机端把超长 JSON 切成若干 type=5 帧（flags: first/last），
 * 板子在这里拼回完整 JSON 再走原来的 handle_json。
 */
static char s_jtxt[1024];
static size_t s_jtxt_len;

static void handle_json_chunk(uint8_t flags, const uint8_t *p, size_t len)
{
    bool first = (flags & 0x01) != 0;
    bool last = (flags & 0x02) != 0;

    if (first) {
        s_jtxt_len = 0;
    }
    if (s_jtxt_len + len >= sizeof(s_jtxt) - 1) {
        s_jtxt_len = 0;     /* 溢出：丢掉这一段 */
        return;
    }
    memcpy(s_jtxt + s_jtxt_len, p, len);
    s_jtxt_len += len;

    if (last) {
        s_jtxt[s_jtxt_len] = 0;
        handle_json((const uint8_t *)s_jtxt, s_jtxt_len);
        s_jtxt_len = 0;
    }
}

static void handle_compact(uint8_t evid, const uint8_t *p, size_t len)
{
    char data[64];
    switch (evid) {
    case 1: {   /* move: int16 dx, int16 dy */
        if (len < 4) {
            return;
        }
        int16_t dx = (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
        int16_t dy = (int16_t)((uint16_t)p[2] | ((uint16_t)p[3] << 8));
        snprintf(data, sizeof(data), "{\"dx\":%d,\"dy\":%d}", dx, dy);
        br_phone_event("move", data);
        break;
    }
    case 2: {   /* click: u8 button, u8 count */
        if (len < 2) {
            return;
        }
        const char *b = (p[0] == 2) ? "right" : "left";
        snprintf(data, sizeof(data),
                 "{\"button\":\"%s\",\"count\":%u}", b, (unsigned)p[1]);
        br_phone_event("click", data);
        break;
    }
    case 3: {   /* scroll: int16 dy */
        if (len < 2) {
            return;
        }
        int16_t dy = (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
        snprintf(data, sizeof(data), "{\"dy\":%d}", dy);
        br_phone_event("scroll", data);
        break;
    }
    case 4: br_phone_event("drag_start", NULL); break;
    case 5: br_phone_event("drag_end", NULL); break;
    case 6: br_phone_event("mid_down", NULL); break;
    case 7: br_phone_event("mid_up", NULL); break;
    default: break;
    }
}

static void handle_voice(uint8_t type, uint8_t flags, const uint8_t *p, size_t len)
{
    bool first = (flags & 0x01) != 0;
    bool last = (flags & 0x02) != 0;

    if (first) {
        s_voice_len = 0;
    }
    if (s_voice_len + len > sizeof(s_voice)) {
        s_voice_len = 0;    /* 溢出就丢弃这一段 */
        return;
    }
    memcpy(s_voice + s_voice_len, p, len);
    s_voice_len += len;

    if (last && s_voice_len) {
        /* ADPCM 走手机语音那条路（和遥控器同一套解码器） */
        br_phone_push_adpcm(s_voice, s_voice_len, first);
        s_voice_len = 0;
    }
}

static void on_write(uint8_t type, uint8_t evid, uint8_t flags,
                     const uint8_t *p, size_t len)
{
    if (!ready()) {
        return;
    }
    switch (type) {
    case 1:
        handle_json(p, len);
        break;
    case 2:
        handle_compact(evid, p, len);
        break;
    case 5:
        handle_json_chunk(flags, p, len);
        break;
    case 3:
    case 4:
        handle_voice(type, flags, p, len);
        break;
    default:
        break;
    }
}

/* ------------------------------------------------------------ GATT 回调 */

static int gatt_access(uint16_t conn_handle, uint16_t attr_handle,
                       struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;

    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) {
        return BLE_ATT_ERR_UNLIKELY;
    }

    /* 首 4 字节是帧头，后面是载荷 */
    uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
    if (om_len < 4) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    uint8_t hdr[4];
    if (os_mbuf_copydata(ctxt->om, 0, 4, hdr) != 0) {
        return BLE_ATT_ERR_UNLIKELY;
    }
    size_t len = hdr[3];
    if (len > om_len - 4) {
        len = om_len - 4;
    }
    static uint8_t buf[512];
    if (len > sizeof(buf)) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    if (len && os_mbuf_copydata(ctxt->om, 4, len, buf) != 0) {
        return BLE_ATT_ERR_UNLIKELY;
    }
    /*
     * ★ 这里直接处理，不再排到别的任务 ★
     *   载荷很小（指针事件 4 字节、键盘事件几十字节），
     *   处理函数只做解析 + 调 br_phone_event（内部只碰 USB 队列），
     *   耗时是微秒级 —— 不会把 NimBLE host 任务拖住。
     */
    on_write(hdr[0], hdr[1], hdr[2], buf, len);
    return 0;
}

/* ------------------------------------------------------------ 板子→手机 */

bool br_ble_gatt_phone_send(const char *name, const char *data)
{
    if (!ready() || !s_notify_on) {
        return false;
    }
    static char buf[512];
    int n = snprintf(buf, sizeof(buf),
                     "{\"ev\":\"%s\",\"data\":%s}", name ? name : "?",
                     (data && data[0]) ? data : "null");
    if (n <= 0 || n >= (int)sizeof(buf)) {
        return false;
    }
    struct os_mbuf *om = ble_hs_mbuf_from_flat(buf, (uint16_t)n);
    if (!om) {
        return false;
    }
    int rc = ble_gatts_notify_custom(s_conn, s_evt_handle, om);
    return rc == 0;
}

/* br_web 的事件转发钩子：原来进 HTTP 轮询队列的，现在直接 notify 给手机 */
static void forward_event(const char *name, const char *data)
{
    (void)br_ble_gatt_phone_send(name, data);
}

/* ------------------------------------------------------------ GAP 事件 */

int br_ble_gatt_gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;

    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            s_conn = event->connect.conn_handle;
            ESP_LOGI(TAG, "手机连上 handle=%u", s_conn);
            if (s_phone_blocked && br_ble_pair_window_remaining() <= 0) {
                /*
                 * ★ 不能立刻踢 ★
                 *   手机还没订阅通知，这时发 denied 它收不到，
                 *   只会变成 1 秒一次的 133 重连风暴。
                 *   先留着，等它订阅（那时通知才发得出去）再告知并断开。
                 */
                ESP_LOGW(TAG, "手机已被电脑端移除，等订阅后告知并断开");
                s_deny_pending = true;
                if (s_deny_timer) {
                    esp_timer_stop(s_deny_timer);
                    esp_timer_start_once(s_deny_timer, 3000000);   /* 3 秒兜底 */
                }
                return 0;
            }
            if (s_phone_blocked && br_ble_pair_window_remaining() > 0) {
                s_phone_blocked = false;
                nvs_handle_t h;
                if (nvs_open("phone", NVS_READWRITE, &h) == ESP_OK) {
                    nvs_set_u8(h, "blocked", 0);
                    nvs_commit(h);
                    nvs_close(h);
                }
            }
            bridge_led_set_phone_linked(true);
            /* 取身份 + 读设备名 */
            struct ble_gap_conn_desc desc;
            memset(&desc, 0, sizeof(desc));
            if (ble_gap_conn_find(s_conn, &desc) == 0) {
                memcpy(s_phone_addr, desc.peer_ota_addr.val, 6);
                s_phone_have_addr = true;
                if (desc.peer_id_addr.type != 0xFF) {
                    memcpy(s_phone_id, desc.peer_id_addr.val, 6);
                    s_phone_has_id = true;
                } else {
                    s_phone_has_id = false;
                }
            }
            s_phone_name[0] = 0;
            report_phone();
            static const ble_uuid16_t s_devname_uuid = BLE_UUID16_INIT(0x2A00);
            int nrc = ble_gattc_read_by_uuid(s_conn, 1, 0xFFFF,
                                             (const ble_uuid_t *)&s_devname_uuid,
                                             phone_name_cb, NULL);
            if (nrc != 0) {
                ESP_LOGI(TAG, "读设备名失败 rc=%d（等 App 上报机型名）", nrc);
            }
        } else {
            s_conn = BLE_HS_CONN_HANDLE_NONE;
            ESP_LOGW(TAG, "手机连接失败 status=%d", event->connect.status);
            start_advertising();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "手机断开 reason=%d", event->disconnect.reason);
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        s_notify_on = false;
        s_active = false;
        s_voice_len = 0;
        s_phone_name[0] = 0;
        s_deny_pending = false;
        report_phone();
        bridge_led_set_phone_linked(false);
        start_advertising();    /* 断开就继续广播，等手机回来 */
        return 0;

    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.attr_handle == s_evt_handle) {
            s_notify_on = event->subscribe.cur_notify != 0;
            s_active = s_notify_on;
            ESP_LOGI(TAG, "事件通知 %s", s_notify_on ? "已开启" : "已关闭");
            if (s_notify_on) {
                if (s_deny_pending) {
                    s_deny_pending = false;
                    if (s_deny_timer) {
                        esp_timer_stop(s_deny_timer);
                    }
                    br_ble_gatt_phone_send("denied", "{}");
                    vTaskDelay(pdMS_TO_TICKS(30));
                    ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
                    return 0;
                }
                /* 手机订阅后才能收，这里补推一次自定义键清单 */
                br_ckeys_report();
            }
        }
        return 0;

    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "MTU=%u", event->mtu.value);
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        /* 没连上就继续广播 */
        if (s_conn == BLE_HS_CONN_HANDLE_NONE) {
            start_advertising();
        }
        return 0;

    default:
        return 0;
    }
}

/* ------------------------------------------------------------ 注册 */

/*
 * ★ 不要手工加 CCCD 描述符 ★
 *   带 BLE_GATT_CHR_F_NOTIFY 的特征，NimBLE 会**自动**生成
 *   0x2902（Client Characteristic Configuration）描述符并处理它的读写。
 *   手工再加一个 access_cb=NULL 的 0x2902 会被 ble_gatts_svc_is_sane
 *   判成非法定义 —— ble_gatts_count_cfg 直接返回 EINVAL(3)，
 *   整个服务注册失败（实测：日志 "ble_gatts_count_resources rc=3"）。
 */

static const struct ble_gatt_chr_def s_chrs[] = {
    {
        .uuid = &s_ctl_uuid.u,
        .access_cb = gatt_access,
        .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
    },
    {
        .uuid = &s_bin_uuid.u,
        .access_cb = gatt_access,
        .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
    },
    {
        .uuid = &s_evt_uuid.u,
        .access_cb = gatt_access,
        .val_handle = &s_evt_handle,
        .flags = BLE_GATT_CHR_F_NOTIFY,
    },
    { 0 },
};

static const struct ble_gatt_svc_def s_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_svc_uuid.u,
        .characteristics = s_chrs,
    },
    { 0 },
};

esp_err_t br_ble_gatt_init(void)
{
    /* 标准 GAP/GATT 服务 */
    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(s_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "count_cfg rc=%d", rc);
        return ESP_FAIL;
    }
    rc = ble_gatts_add_svcs(s_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "add_svcs rc=%d", rc);
        return ESP_FAIL;
    }
    {
        nvs_handle_t h;
        if (nvs_open("phone", NVS_READONLY, &h) == ESP_OK) {
            uint8_t v = 0;
            if (nvs_get_u8(h, "blocked", &v) == ESP_OK) {
                s_phone_blocked = (v != 0);
            }
            nvs_close(h);
        }
    }

    if (!s_deny_timer) {
        const esp_timer_create_args_t dargs = {
            .callback = deny_timeout_cb,
            .name = "deny_to",
        };
        esp_timer_create(&dargs, &s_deny_timer);
    }

    rc = ble_svc_gap_device_name_set(PHONE_DEV_NAME);
    if (rc != 0) {
        ESP_LOGW(TAG, "device_name rc=%d", rc);
    }

    /* 板子→手机的事件：接管 br_web 的转发 */
    br_web_set_event_forward(forward_event);
    ESP_LOGI(TAG, "GATT 服务就绪（%s）", PHONE_DEV_NAME);
    return ESP_OK;
}

void br_ble_gatt_on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) {
        ESP_LOGE(TAG, "ensure_addr rc=%d", rc);
        return;
    }
    rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    if (rc != 0) {
        ESP_LOGE(TAG, "infer_auto rc=%d", rc);
        return;
    }
    start_advertising();
}
