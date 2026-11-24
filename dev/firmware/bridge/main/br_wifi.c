/*
 * br_wifi - WiFi STA 实现
 * ---------------------------------------------------------------------------
 * 只做 STA（连路由器），不做 AP —— 但连不上时会降级开一个 SoftAP，
 * 让手机至少能连上板子（见 br_wifi_start_ap）。
 */
#include "br_wifi.h"
#include "br_ble_core.h"
#include "br_proto.h"

#include <string.h>
#include <stdlib.h>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <esp_event.h>
#include <esp_log.h>
#include <esp_mac.h>
#include <esp_netif.h>
#include <esp_timer.h>
#include <esp_wifi.h>
#include <esp_coexist.h>
#include <nvs_flash.h>

static const char *TAG = "br_wifi";

#define NVS_NS       "wifi"
#define NVS_KEY_SSID "ssid"
#define NVS_KEY_PASS "pass"

/*
 * ★ 记住多个 WiFi ★
 *   板子拿到家里、办公室、咖啡厅各配一次太烦 ——
 *   存一串网络，开机按顺序试，连上哪个用哪个。
 *   每个网络存成 NVS 里的一条 blob（net0..netN），值是 "ssid\0密码"。
 */
#define WIFI_MAX_NETS 6
#define NVS_KEY_NET   "net%d"
/*
 * ★ 缓冲区大小必须与 wifi_config_t 的字段一致 ★
 *   sta.ssid 是 uint8_t[32]、sta.password 是 uint8_t[64]，
 *   我们的 s_ssid/s_pass 若比它大一位，编译器会报 format-truncation
 *   （而本项目把警告当错误 → 直接编不过）。
 *
 *   名字要加前缀：MAX_SSID_LEN / MAX_PASS_LEN 是 IDF 自己定义的。
 */
#define BR_WIFI_SSID_MAX 31
#define BR_WIFI_PASS_MAX 63

typedef struct {
    char ssid[BR_WIFI_SSID_MAX + 1];
    char pass[BR_WIFI_PASS_MAX + 1];
    bool used;
} wifi_net_t;

static wifi_net_t s_nets[WIFI_MAX_NETS];
static int  s_net_count;
static int  s_net_cur;        /* 当前正在试的那个 */

static char s_ssid[BR_WIFI_SSID_MAX + 1];
static char s_pass[BR_WIFI_PASS_MAX + 1];
static char s_ip[16];
static int  s_rssi;
static int  s_state;            /* 0 未连 / 1 已连 / 2 热点 */
static bool s_have_creds;
static int  s_retry;
static int  s_fail_reason;   /* 最近一次连接失败的原因码（esp_wifi reason） */
static bool s_scan_pause;     /* 扫描期间暂停重连循环 */

/*
 * 状态变了就主动报给 PC。
 *
 * ★ 为什么可以直接在这儿发 ★
 *   bridge_usb_write() 只是把行入队（xQueueSend，非阻塞、线程安全），
 *   真正的发送在 usb_tx_task 里 —— 所以在 WiFi 事件任务里发不会堵住事件队列。
 *   以前这里绕了个「任务通知」，但那个任务句柄从来没被赋值过，
 *   结果 EV WIFI 一次都没发出去：板子连上了，PC 端却要等用户手动点刷新。
 */
static void report_state(void)
{
    br_wifi_report();
}

/*
 * 失败原因翻译成人话。
 *
 * ★ 为什么必须翻译 ★
 *   板子只报 reason=201，PC 端只能显示「未连接」——
 *   用户看到的是「家里明明有 WiFi，板子就是连不上」，毫无线索。
 *   实际上 201 绝大多数是**名字写错（大小写）/ 只开 5GHz**，
 *   跟密码毫无关系；不翻译就会一直往「密码错」的方向查。
 */
static const char *wifi_fail_hint(int reason)
{
    switch (reason) {
    case 0:
        return "";
    case 201:
        return "找不到这个 WiFi：名字要完全一致（大小写也算），"
               "而且 ESP32 只能连 2.4GHz —— 带 -5G 的名字连不上";
    case 202:
        return "密码不对";
    case 204:
        return "密码不对（握手失败）";
    case 15:
        return "WiFi 4 次握手超时：信号太弱或密码不对";
    default:
        return "连接失败";
    }
}

/* 前置声明（重连定时器在下面就要用到） */
static void wifi_priority_begin(void);
static void wifi_priority_end(void);
static void net_store(int idx);
static void load_nets(void);
static int nets_add(const char *ssid, const char *pass);

/*
 * 重连定时器。
 *
 * WIFI 事件回调跑在 WiFi 自己的任务上 —— 在那儿睡会堵住整个 WiFi 事件队列，
 * 表现是「断开一次之后再也不会重连」。所以重连交给 esp_timer 定时器。
 */
static esp_timer_handle_t s_retry_timer;

/** 安排一次退避重连 */
static void schedule_reconnect(void)
{
    if (!s_retry_timer) {
        return;
    }
    int delay_ms = 1000 * s_retry;
    if (delay_ms > 10000) {
        delay_ms = 10000;
    }
    esp_timer_stop(s_retry_timer);
    esp_timer_start_once(s_retry_timer, (uint64_t)delay_ms * 1000ULL);
}

/** 定时器回调：在 esp_timer 任务里，可以安全地调 esp_wifi_connect */
/*
 * ★ SSID 大小写自愈 ★
 *
 *   WiFi 的 SSID **大小写敏感**：路由器广播 "CMCC-7K5j"，
 *   用户在 PC 上打成 "CMCC-7k5j"，板子就永远找不到它 ——
 *   表现是 reason=201（NO_AP_FOUND），连接一直失败，
 *   而用户在 PC/手机上看着那个 WiFi 明明就在眼前。
 *
 *   这里在「找不到 AP」之后扫一次：
 *   有没有哪个正在广播的 AP，名字和用户填的**只差大小写**？
 *   有就把记住的名字改成实际广播的那个（密码不变），
 *   以后就自动连上了。
 */
static bool fix_ssid_case(void)
{
    if (s_net_cur < 0 || s_net_cur >= s_net_count) {
        return false;
    }
    wifi_scan_config_t sc = {
        .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .coex_background_scan = true,
    };
    uint16_t found = 0;
    bool fixed = false;

    br_ble_set_wifi_priority(true);
    if (esp_wifi_scan_start(&sc, true) == ESP_OK &&
        esp_wifi_scan_get_ap_num(&found) == ESP_OK && found > 0) {
        wifi_ap_record_t *res = calloc(found, sizeof(wifi_ap_record_t));
        if (res) {
            if (esp_wifi_scan_get_ap_records(&found, res) == ESP_OK) {
                for (uint16_t i = 0; i < found; i++) {
                    const char *ap = (const char *)res[i].ssid;
                    if (!ap[0]) {
                        continue;
                    }
                    if (strcasecmp(ap, s_nets[s_net_cur].ssid) == 0 &&
                        strcmp(ap, s_nets[s_net_cur].ssid) != 0) {
                        ESP_LOGW(TAG, "SSID 大小写对不上：记住的是 %s，"
                                 "实际广播 %s —— 按实际的改",
                                 s_nets[s_net_cur].ssid, ap);
                        snprintf(s_nets[s_net_cur].ssid,
                                 sizeof(s_nets[s_net_cur].ssid), "%s", ap);
                        snprintf(s_ssid, sizeof(s_ssid), "%s", ap);
                        net_store(s_net_cur);   /* 落盘，以后都按这个连 */
                        fixed = true;
                        break;
                    }
                }
            }
            free(res);
        }
    }
    br_ble_set_wifi_priority(false);
    return fixed;
}

static void on_retry_timer(void *arg)
{
    (void)arg;
    /*
     * 上次是「找不到 AP」→ 先试试是不是名字大小写不对。
     * （扫一次要一两秒，值得换：不然用户永远配不上网）
     */
    if (s_fail_reason == 201) {
        fix_ssid_case();
    }
    wifi_priority_begin();
    esp_wifi_connect();
}

/**
 * ★ WiFi 连接前让出射频 ★
 *
 *   ESP32-S3 单天线，BLE 扫描会把 WiFi 的 4 次握手打断（reason=201）。
 *   连接期间暂停 BLE 扫描，连上后再恢复。
 */
static void wifi_priority_begin(void)
{
    br_ble_set_wifi_priority(true);
}

/** WiFi 连上（或放弃）后恢复 BLE 扫描 */
static void wifi_priority_end(void)
{
    br_ble_set_wifi_priority(false);
}

/* ------------------------------------------------------------------ NVS */

static void load_credentials(void)
{
    nvs_handle_t nvs;
    size_t len;

    memset(s_ssid, 0, sizeof(s_ssid));
    memset(s_pass, 0, sizeof(s_pass));

    /* 老版本只存一条 ssid/pass —— 读出来当兜底 */
    if (nvs_open(NVS_NS, NVS_READONLY, &nvs) == ESP_OK) {
        len = sizeof(s_ssid);
        if (nvs_get_str(nvs, NVS_KEY_SSID, s_ssid, &len) != ESP_OK) {
            s_ssid[0] = '\0';
        }
        len = sizeof(s_pass);
        if (nvs_get_str(nvs, NVS_KEY_PASS, s_pass, &len) != ESP_OK) {
            s_pass[0] = '\0';
        }
        nvs_close(nvs);
    }

    load_nets();
    if (s_net_count > 0) {
        /* 开机从第一个记住的网络开始试 */
        s_net_cur = 0;
        snprintf(s_ssid, sizeof(s_ssid), "%s", s_nets[0].ssid);
        snprintf(s_pass, sizeof(s_pass), "%s", s_nets[0].pass);
    } else if (s_ssid[0] != '\0') {
        /* 只有老键：收编进列表，之后统一走多网络那套 */
        nets_add(s_ssid, s_pass);
        s_net_cur = 0;
    }
    s_have_creds = (s_ssid[0] != '\0');
}

static void save_credentials(const char *ssid, const char *pass)
{
    nvs_handle_t nvs;

    if (nvs_open(NVS_NS, NVS_READWRITE, &nvs) != ESP_OK) {
        ESP_LOGE(TAG, "打开 NVS 失败");
        return;
    }
    nvs_set_str(nvs, NVS_KEY_SSID, ssid);
    nvs_set_str(nvs, NVS_KEY_PASS, pass);
    nvs_commit(nvs);
    nvs_close(nvs);
}

/* 一条网络在 NVS 里的存法：ssid + '\0' + 密码 */
static void net_pack(const wifi_net_t *n, char *buf, size_t cap, size_t *len)
{
    size_t sl = strlen(n->ssid);
    size_t pl = strlen(n->pass);
    if (sl + 1 + pl > cap) {
        *len = 0;
        return;
    }
    memcpy(buf, n->ssid, sl);
    buf[sl] = '\0';
    memcpy(buf + sl + 1, n->pass, pl);
    *len = sl + 1 + pl;
}

static void net_unpack(const char *buf, size_t len, wifi_net_t *n)
{
    memset(n, 0, sizeof(*n));
    if (len == 0) {
        return;
    }
    size_t sl = strnlen(buf, len);
    if (sl >= sizeof(n->ssid)) {
        sl = sizeof(n->ssid) - 1;
    }
    memcpy(n->ssid, buf, sl);
    n->ssid[sl] = '\0';
    if (len > sl + 1) {
        size_t pl = len - sl - 1;
        if (pl >= sizeof(n->pass)) {
            pl = sizeof(n->pass) - 1;
        }
        memcpy(n->pass, buf + sl + 1, pl);
        n->pass[pl] = '\0';
    }
    n->used = true;
}

/** 把已保存的网络全部读出来 */
static void load_nets(void)
{
    nvs_handle_t nvs;
    char key[16];
    char buf[BR_WIFI_SSID_MAX + BR_WIFI_PASS_MAX + 2];
    size_t len;

    memset(s_nets, 0, sizeof(s_nets));
    s_net_count = 0;

    if (nvs_open(NVS_NS, NVS_READONLY, &nvs) != ESP_OK) {
        return;
    }
    for (int i = 0; i < WIFI_MAX_NETS; i++) {
        snprintf(key, sizeof(key), NVS_KEY_NET, i);
        len = sizeof(buf);
        if (nvs_get_blob(nvs, key, buf, &len) != ESP_OK) {
            continue;
        }
        net_unpack(buf, len, &s_nets[s_net_count]);
        if (s_nets[s_net_count].used) {
            s_net_count++;
        }
    }
    nvs_close(nvs);
    ESP_LOGI(TAG, "已记住 %d 个 WiFi", s_net_count);
}

/** 把一条网络写进 NVS（顺带同步老的 ssid/pass 键，别的地方还在读） */
static void net_store(int idx)
{
    nvs_handle_t nvs;
    char key[16];
    char buf[BR_WIFI_SSID_MAX + BR_WIFI_PASS_MAX + 2];
    size_t len = 0;

    if (nvs_open(NVS_NS, NVS_READWRITE, &nvs) != ESP_OK) {
        ESP_LOGE(TAG, "打开 NVS 失败");
        return;
    }
    net_pack(&s_nets[idx], buf, sizeof(buf), &len);
    snprintf(key, sizeof(key), NVS_KEY_NET, idx);
    if (len > 0) {
        nvs_set_blob(nvs, key, buf, len);
    } else {
        nvs_erase_key(nvs, key);
    }
    nvs_commit(nvs);
    nvs_close(nvs);
    save_credentials(s_nets[idx].ssid, s_nets[idx].pass);
}

/**
 * 加一个网络（同名就改密码，新的就追加）。
 * 返回它在列表里的下标，-1 表示失败。
 */
static int nets_add(const char *ssid, const char *pass)
{
    if (!ssid || !*ssid) {
        return -1;
    }
    for (int i = 0; i < s_net_count; i++) {
        if (strcmp(s_nets[i].ssid, ssid) == 0) {
            snprintf(s_nets[i].pass, sizeof(s_nets[i].pass), "%s", pass ? pass : "");
            net_store(i);
            return i;
        }
    }
    if (s_net_count >= WIFI_MAX_NETS) {
        /* 满了就顶掉最后一个（一般是过期的） */
        s_net_count--;
    }
    int i = s_net_count++;
    memset(&s_nets[i], 0, sizeof(s_nets[i]));
    snprintf(s_nets[i].ssid, sizeof(s_nets[i].ssid), "%s", ssid);
    snprintf(s_nets[i].pass, sizeof(s_nets[i].pass), "%s", pass ? pass : "");
    net_store(i);
    ESP_LOGI(TAG, "已记住第 %d 个 WiFi：%s", i + 1, ssid);
    return i;
}

static void nets_del(int idx)
{
    if (idx < 0 || idx >= s_net_count) {
        return;
    }
    for (int i = idx; i < s_net_count - 1; i++) {
        s_nets[i] = s_nets[i + 1];
    }
    s_net_count--;
    /* 把后面的挪上来，存回去 */
    nvs_handle_t nvs;
    if (nvs_open(NVS_NS, NVS_READWRITE, &nvs) == ESP_OK) {
        char key[16];
        for (int i = 0; i < WIFI_MAX_NETS; i++) {
            snprintf(key, sizeof(key), NVS_KEY_NET, i);
            if (i < s_net_count) {
                char buf[BR_WIFI_SSID_MAX + BR_WIFI_PASS_MAX + 2];
                size_t len = 0;
                net_pack(&s_nets[i], buf, sizeof(buf), &len);
                if (len) {
                    nvs_set_blob(nvs, key, buf, len);
                    continue;
                }
            }
            nvs_erase_key(nvs, key);
        }
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

/* ------------------------------------------------------------------ 事件 */

/**
 * 切到第 idx 个已记住的网络并开始连接。
 *
 * 换了地方（家里 → 办公室）不用重新配网：列表里哪个在场就连哪个。
 */
static bool net_begin(int idx)
{
    if (idx < 0 || idx >= s_net_count) {
        return false;
    }
    s_net_cur = idx;
    snprintf(s_ssid, sizeof(s_ssid), "%s", s_nets[idx].ssid);
    snprintf(s_pass, sizeof(s_pass), "%s", s_nets[idx].pass);

    wifi_config_t wc = { 0 };
    snprintf((char *)wc.sta.ssid, sizeof(wc.sta.ssid), "%s", s_ssid);
    snprintf((char *)wc.sta.password, sizeof(wc.sta.password), "%s", s_pass);
    wc.sta.threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK;
    if (esp_wifi_set_config(WIFI_IF_STA, &wc) != ESP_OK) {
        return false;
    }
    wifi_priority_begin();
    if (esp_wifi_connect() != ESP_OK) {
        wifi_priority_end();
        return false;
    }
    ESP_LOGI(TAG, "试第 %d 个 WiFi：%s", idx + 1, s_ssid);
    return true;
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id,
                          void *data)
{
    (void)arg;

    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
        return;
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *disc = (wifi_event_sta_disconnected_t *)data;
        s_state = 0;
        s_ip[0] = '\0';
        s_rssi = 0;
        /*
         * ★ 打印断开原因 ★
         *   reason 数字看不出所以然，但结合 esp_wifi 的 reason 枚举：
         *     201 = NO_AP_FOUND        —— **找不到这个 AP**
         *          （名字写错、**大小写不对**、只开 5GHz、信号不在场）
         *     202 = AUTH_FAIL          —— 密码错
         *     204 = 4WAY_HANDSHAKE_TIMEOUT —— 密码错（WPA 三次握手失败）
         *   ★ 以前这段注释把 201 和 202 写反了 ★
         *   看错注释就会把「名字写错」当成「密码错」，方向整个反掉。
         */
        s_fail_reason = disc->reason;
        ESP_LOGW(TAG, "WiFi 断开 reason=%d，重试 %d", disc->reason, s_retry);
        report_state();
        /*
         * ★ 重连要退避，但不能在事件回调里睡 ★
         *   WIFI 事件的回调跑在 WiFi 自己的任务上，
         *   在这儿 vTaskDelay 会把整个 WiFi 事件队列堵住，
         *   表现是「断开之后再也连不上」。
         *   改成用定时器安排重连。
         */
        /* 扫描期间不把「主动断开」当成失败，也不再重连（扫完再恢复） */
        if (s_scan_pause) {
            return;
        }
        if (s_retry < 10) {
            s_retry++;
        }
        /*
         * ★ 记住多个 WiFi 时轮流试 ★
         *   同一个网络试 3 次还不行（密码错 / 不在场），就换列表里的下一个。
         *   换地方不用重新配网 —— 板子自己找到在场的那个。
         */
        if (s_net_count > 1 && s_retry >= 3) {
            s_retry = 0;
            if (net_begin((s_net_cur + 1) % s_net_count)) {
                return;
            }
        }
        schedule_reconnect();
        return;
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&ev->ip_info.ip));
        s_state = 1;
        s_retry = 0;
        wifi_priority_end();
        ESP_LOGI(TAG, "WiFi 已连接 %s，IP %s", s_ssid, s_ip);
        report_state();
        return;
    }
    if (base == IP_EVENT && id == IP_EVENT_ASSIGNED_IP_TO_CLIENT) {
        ESP_LOGI(TAG, "SoftAP 有设备接入");
        return;
    }
}

/**
 * 扫描附近 AP。
 *
 * ★ 为什么固件要能扫 ★
 *   ESP32-S3 只支持 2.4 GHz，而路由器常常同时开着 5 GHz 与 2.4 GHz 两个名字
 *   （"XXX-5G" / "XXX"）。用户在 PC 上填的 SSID 若是 5 GHz 的那个，
 *   板子根本看不见 → 一直 reason=201（NO_AP_FOUND），
 *   而 PC 上看着「WiFi 连着」完全正常，极难自己想到是频段问题。
 *   所以固件必须能列出**它自己看得见的** AP，让 PC 端照着选。
 */
void br_wifi_scan(void)
{
    /*
     * ★ 扫描时长必须用默认值 ★
     *   IDF 的 coex 要求：**蓝牙开着的时候不能自己指定 active scan 的时长**，
     *   否则 esp_wifi_scan_start 直接报错：
     *     "Error! Should use default active scan time parameter for
     *      WiFi scan when Bluetooth is enabled!!!!!!"
     *   报错的后果是扫出 0 个 AP —— 板子「一个 WiFi 都看不见」，
     *   用户在 PC 上看到的就是「明明家里有 WiFi，板子就是连不上」。
     *   这里把 scan_time 留空（= 用 IDF 默认值）。
     */
    wifi_scan_config_t sc = {
        .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .coex_background_scan = true,
    };
    uint16_t found = 0;
    wifi_ap_record_t *res = NULL;

    /* 扫描期间要停掉 BLE 扫描 —— 同射频，扫不起来 */
    br_ble_set_wifi_priority(true);

    /*
     * ★ 扫描时必须先把「连网重试循环」停下来 ★
     *   板子连不上网时每几秒就会重试一次，
     *   而 STA 正在连接/重连的时候发起扫描，扫描结果会**一个 AP 都扫不到**
     *   （实测：扫描直接返回 0 个，用户看到的是「板子一个 WiFi 都看不见」）。
     *   所以这里先主动断开并挂起重连，扫完再恢复。
     */
    s_scan_pause = true;
    esp_wifi_disconnect();

    if (esp_wifi_scan_start(&sc, true) != ESP_OK) {
        br_proto_send_err("WIFISCAN", "start", NULL);
        br_ble_set_wifi_priority(false);
        s_scan_pause = false;
        schedule_reconnect();
        return;
    }

    if (esp_wifi_scan_get_ap_num(&found) != ESP_OK || found == 0) {
        br_proto_send_err("WIFISCAN", "none", NULL);
        br_ble_set_wifi_priority(false);
        s_scan_pause = false;
        schedule_reconnect();
        return;
    }
    res = calloc(found, sizeof(wifi_ap_record_t));
    if (!res) {
        br_proto_send_err("WIFISCAN", "nomem", NULL);
        br_ble_set_wifi_priority(false);
        s_scan_pause = false;
        schedule_reconnect();
        return;
    }
    if (esp_wifi_scan_get_ap_records(&found, res) != ESP_OK) {
        free(res);
        br_proto_send_err("WIFISCAN", "get", NULL);
        br_ble_set_wifi_priority(false);
        s_scan_pause = false;
        schedule_reconnect();
        return;
    }
    for (uint16_t i = 0; i < found; i++) {
        br_kv_t kv;
        br_kv_init(&kv);
        br_kv_add(&kv, "ssid", (const char *)res[i].ssid);
        br_kv_add_int(&kv, "rssi", res[i].rssi);
        br_kv_add_int(&kv, "chan", res[i].primary);
        br_kv_add_int(&kv, "auth", res[i].authmode);
        br_proto_send_dt("WIFIAP", &kv);
    }
    free(res);
    br_ble_set_wifi_priority(false);
    br_proto_send_ok("WIFISCAN", NULL);
    /* 扫完恢复连接：接着试当前这个网络 */
    s_scan_pause = false;
    schedule_reconnect();
}

/* ------------------------------------------------------------------ 热点 */

/*
 * 降级热点。
 *
 * 场景：用户还没配网，或者路由器暂时不可用。
 * 手机连上 ESP32-Remote-XXXX 就能直接访问 192.168.4.1，
 * 页面照样能用（触控板/键盘不依赖路由器）。
 */
static void start_ap(void)
{
    wifi_config_t cfg = { 0 };
    uint8_t mac[6];
    char ssid[32];

    esp_wifi_get_mac(WIFI_IF_STA, mac);
    snprintf(ssid, sizeof(ssid), "ESP32-Remote-%02X%02X", mac[4], mac[5]);

    memcpy(cfg.ap.ssid, ssid, strlen(ssid));
    cfg.ap.ssid_len = (uint8_t)strlen(ssid);
    cfg.ap.channel = 1;
    cfg.ap.max_connection = 4;
    cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    /* 密码固定 12345678 —— 局域网内的一次性用途，AP 本身不加密也没意义 */
    strcpy((char *)cfg.ap.password, "12345678");

    if (esp_wifi_set_mode(WIFI_MODE_AP) != ESP_OK) {
        return;
    }
    if (esp_wifi_set_config(WIFI_IF_AP, &cfg) != ESP_OK) {
        return;
    }
    if (esp_wifi_start() != ESP_OK) {
        return;
    }
    s_state = 2;
    snprintf(s_ip, sizeof(s_ip), "192.168.4.1");
    ESP_LOGW(TAG, "已降级为热点 %s，地址 %s", ssid, s_ip);
    report_state();
}

/* ------------------------------------------------------------------ 接口 */

void br_wifi_init(void)
{
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();

    load_credentials();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    /*
     * ★ 给板子起个在路由器里认得出的名字 ★
     *   路由器 DHCP 列表默认显示芯片派生名（常常就是一串 MAC），
     *   用户根本看不出哪个是 ESP32 → 只能照着 IP 一个个试，
     *   手机端就会填错地址、报「连不上电脑」。
     *   设了名字之后，路由器列表里能直接看到 ESP32-Remote，IP 就在旁边。
     *
     *   ★ 注意用字符串 key ★
     *     esp_netif_get_handle_from_ifkey() 要的是 if_key 字符串
     *     （"WIFI_STA_DEF"），传枚举 WIFI_IF_STA（整数 0）会被当指针解引用
     *     → LoadProhibited，板子一开机就崩（踩过，别改回去）。
     */
    esp_netif_t *sta_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (sta_netif) {
        esp_netif_set_hostname(sta_netif, "ESP32-Remote");
    }

    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    /*
     * 重连定时器：断线后退避重连（不能在 WiFi 事件回调里睡）。
     */
    if (!s_retry_timer) {
        esp_timer_create_args_t targs = {
            .callback = on_retry_timer,
            .name = "wifi_retry",
            .dispatch_method = ESP_TIMER_TASK,
        };
        ESP_ERROR_CHECK(esp_timer_create(&targs, &s_retry_timer));
    }
    /*
     * ★ 关掉 WiFi 省电 ★
     *   DTIM 休眠会让 WiFi 错过 beacon，与 BLE 的抢占窗口叠在一起
     *   → 表现为「连上了又断」。这个板子上 WiFi 只做局域网服务，
     *   一直醒着没有代价。
     */
    /*
         * ★ WiFi 省电必须开 ★
         *
         *   原来写的是 WIFI_PS_NONE —— WiFi 射频**常开**。
         *   而 ESP32-S3 的 WiFi 和 BLE 共用一根天线：
         *   WiFi 常开时 BLE 只能抢到很少的时隙，
         *   **两条 BLE 链路就维持不住**（实测第二台连上后，
         *   第一台的链路报 LL Response Timeout 被断掉 ——
         *   用户看到的就是「配了新的，原来那台就按键没反应了」）。
         *
         *   MIN_MODEM：空闲时 WiFi 休眠、有流量时全速，
         *   对手机端 HTTP 的影响很小，但能给 BLE 腾出射频。
         */
        esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    /*
     * ★ 同射频共存时优先 WiFi ★
     *   S3 只有一根天线，BLE 与 WiFi 抢射频。
     *   默认策略偏向 BLE，WiFi 会被挤掉 —— 实测就是
     *   「Wi-Fi connect fail, apply reconnect coex policy」。
     *   手机端是这台板子的主要用途，让 WiFi 优先。
     */
    esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
    /*
     * ★ IP_EVENT 必须也交给 on_wifi_event ★
     *   「拿到 IP」才是真正连上的标志（s_state=1 / s_ip 都在这儿赋值）。
     *   以前 IP_EVENT 注册到了另一个只打日志的回调上 ——
     *   板子明明连着路由器，PC 端却一直显示「未连接」、手机地址是空的。
     */
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL, NULL));

    if (s_have_creds) {
        wifi_config_t wc = { 0 };
        snprintf((char *)wc.sta.ssid, sizeof(wc.sta.ssid), "%s", s_ssid);
        snprintf((char *)wc.sta.password, sizeof(wc.sta.password), "%s", s_pass);
        /* 路由器可能同时开着 WPA3/WPA2 —— 放宽到 WPA/WPA2 全兼容 */
        wc.sta.threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK;
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
        ESP_ERROR_CHECK(esp_wifi_start());
        ESP_LOGI(TAG, "已保存的 WiFi %s，开始连接", s_ssid);
    } else {
        start_ap();
    }
}

bool br_wifi_set_credentials(const char *ssid, const char *pass)
{
    if (!ssid || !*ssid) {
        return false;
    }
    /* 存进「记住的网络」列表：同名改密码，新的追加（最多 6 个） */
    int idx = nets_add(ssid, pass);
    if (idx < 0) {
        return false;
    }
    s_net_cur = idx;
    snprintf(s_ssid, sizeof(s_ssid), "%s", ssid);
    snprintf(s_pass, sizeof(s_pass), "%s", pass ? pass : "");
    s_have_creds = true;
    s_retry = 0;

    /* 从热点切回 STA */
    esp_wifi_stop();
    {
        wifi_config_t wc = { 0 };
        snprintf((char *)wc.sta.ssid, sizeof(wc.sta.ssid), "%s", s_ssid);
        snprintf((char *)wc.sta.password, sizeof(wc.sta.password), "%s", s_pass);
        /* 路由器可能同时开着 WPA3/WPA2 —— 放宽到 WPA/WPA2 全兼容 */
        wc.sta.threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK;
        esp_wifi_set_mode(WIFI_MODE_STA);
        esp_wifi_set_config(WIFI_IF_STA, &wc);
    }
    if (esp_wifi_start() != ESP_OK) {
        ESP_LOGE(TAG, "WiFi 启动失败");
        return false;
    }
    ESP_LOGI(TAG, "开始连接 %s", s_ssid);
    return true;
}

void br_wifi_disconnect(void)
{
    esp_wifi_disconnect();
}

const char *br_wifi_ip(void)
{
    return s_ip;
}

const char *br_wifi_ssid(void)
{
    return s_ssid;
}

int br_wifi_rssi(void)
{
    return s_rssi;
}

int br_wifi_state(void)
{
    return s_state;
}

void br_wifi_report(void)
{
    br_kv_t kv;

    br_kv_init(&kv);
    br_kv_add(&kv, "state", s_state == 1 ? "sta" : (s_state == 2 ? "ap" : "off"));
    br_kv_add(&kv, "ip", s_ip);
    br_kv_add(&kv, "ssid", s_ssid);
    br_kv_add_int(&kv, "rssi", s_rssi);
    /* ★ 把失败原因也报上去 ★
       只报「未连接」用户根本不知道该改名字还是改密码。
       201 = 找不到这个 AP（名字/大小写/5GHz），202/204 = 密码错。 */
    br_kv_add_int(&kv, "reason", s_fail_reason);
    br_kv_add(&kv, "why", wifi_fail_hint(s_fail_reason));
    br_proto_send_ev("WIFI", &kv);
}

void br_wifi_list_nets(void)
{
    for (int i = 0; i < s_net_count; i++) {
        br_kv_t kv;
        br_kv_init(&kv);
        br_kv_add_int(&kv, "idx", i);
        br_kv_add(&kv, "ssid", s_nets[i].ssid);
        br_kv_add_int(&kv, "cur", i == s_net_cur ? 1 : 0);
        br_proto_send_dt("WIFINET", &kv);
    }
    br_proto_send_ok("WIFILIST", NULL);
}

bool br_wifi_delete_net(int idx)
{
    if (idx < 0 || idx >= s_net_count) {
        return false;
    }
    bool was_cur = (idx == s_net_cur);
    nets_del(idx);

    if (s_net_count == 0) {
        s_net_cur = 0;
        s_ssid[0] = '\0';
        s_pass[0] = '\0';
        s_have_creds = false;
        save_credentials("", "");
        return true;
    }
    if (s_net_cur >= s_net_count) {
        s_net_cur = s_net_count - 1;
    }
    snprintf(s_ssid, sizeof(s_ssid), "%s", s_nets[s_net_cur].ssid);
    snprintf(s_pass, sizeof(s_pass), "%s", s_nets[s_net_cur].pass);
    save_credentials(s_ssid, s_pass);
    /* 删掉的正是当前在用的那个 —— 立刻换一个记住的网络 */
    if (was_cur) {
        s_retry = 0;
        net_begin(s_net_cur);
    }
    return true;
}
