/*
 * br_ckeys - 手机「自定义快捷键页」的按键清单
 *
 * PC 端「按键」页是唯一真相：增删改名后按 CKBEGIN / CKADD / CKCOMMIT
 * 下发，这里落 NVS 并推给手机（EV ck / ckdone）。
 * 板子只存「id + 名字」——动作在映射表和 PC 配置里，不在这里。
 */
#include "br_ckeys.h"

#include "br_ble_gatt.h"
#include "br_types.h"

#include <string.h>
#include "esp_log.h"
#include "nvs.h"
#include "esp_timer.h"

static const char *TAG = "br_ckeys";

#define CKEYS_NS    "ckeys"
#define CKEYS_KEY   "list"
#define CKEYS_MAGIC 0x434B0001u
#define CKEYS_VER   3
#define CKEYS_NAME  24

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  version;
    uint8_t  count;
    uint8_t  ids[BR_PHU_CUSTOM_MAX];
    uint8_t  cols[BR_PHU_CUSTOM_MAX];
    uint8_t  rows[BR_PHU_CUSTOM_MAX];
    char     names[BR_PHU_CUSTOM_MAX][CKEYS_NAME];
} ckeys_blob_t;

static ckeys_blob_t s_list;      /* 已提交的清单 */
static esp_timer_handle_t s_tx_timer;
static volatile int s_tx_idx;
static void tx_tick(void *arg);
static ckeys_blob_t s_stage;     /* 正在追加的清单 */
static bool s_staging;

static void blob_reset(ckeys_blob_t *b)
{
    memset(b, 0, sizeof(*b));
    b->magic = CKEYS_MAGIC;
    b->version = CKEYS_VER;
}

esp_err_t br_ckeys_init(void)
{
    blob_reset(&s_list);
    nvs_handle_t h;
    if (nvs_open(CKEYS_NS, NVS_READONLY, &h) == ESP_OK) {
        ckeys_blob_t tmp;
        size_t size = sizeof(tmp);
        if (nvs_get_blob(h, CKEYS_KEY, &tmp, &size) == ESP_OK &&
            size == sizeof(tmp) && tmp.magic == CKEYS_MAGIC &&
            tmp.version == CKEYS_VER && tmp.count <= BR_PHU_CUSTOM_MAX) {
            s_list = tmp;
        }
        nvs_close(h);
    }
    ESP_LOGI(TAG, "自定义键 %u 个", (unsigned)s_list.count);
    if (!s_tx_timer) {
        const esp_timer_create_args_t args = {
            .callback = tx_tick,
            .name = "ck_tx",
        };
        esp_timer_create(&args, &s_tx_timer);
    }
    return ESP_OK;
}

bool br_ckeys_begin(int count)
{
    if (count < 0 || count > (int)BR_PHU_CUSTOM_MAX) {
        return false;
    }
    blob_reset(&s_stage);
    s_staging = true;
    return true;
}

bool br_ckeys_add(int id, int col, int row, const char *name)
{
    if (!s_staging || id < 1 || id > (int)BR_PHU_CUSTOM_MAX) {
        return false;
    }
    if (col < 0 || col > 2 || row < 0 || row > 63) {
        return false;
    }
    for (int i = 0; i < s_stage.count; i++) {
        if (s_stage.ids[i] == (uint8_t)id) {
            return false;   /* 重复 id */
        }
    }
    int slot = s_stage.count;
    s_stage.ids[slot] = (uint8_t)id;
    s_stage.cols[slot] = (uint8_t)col;
    s_stage.rows[slot] = (uint8_t)row;
    /* 名字里的 '_' 是 PC 端为了按空格分词的转义，这里还原 */
    const char *src = name ? name : "";
    int n = 0;
    for (; *src && n < CKEYS_NAME - 1; src++) {
        s_stage.names[slot][n++] = (*src == '_') ? ' ' : *src;
    }
    s_stage.names[slot][n] = 0;
    s_stage.count++;
    return true;
}

bool br_ckeys_commit(void)
{
    if (!s_staging) {
        return false;
    }
    s_staging = false;
    s_list = s_stage;

    nvs_handle_t h;
    if (nvs_open(CKEYS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    esp_err_t err = nvs_set_blob(h, CKEYS_KEY, &s_list, sizeof(s_list));
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "保存失败 %d", (int)err);
        return false;
    }
    ESP_LOGI(TAG, "已保存自定义键 %u 个", (unsigned)s_list.count);
    return true;
}

int br_ckeys_count(void)
{
    return (int)s_list.count;
}

/* 前向声明（init 里要用） */
static void tx_tick(void *arg);

/*
 * ★ 逐条节流发送 ★
 *   以前在 BLE 回调里一口气 notify 二十多条，Controller 的 mbuf 供不上，
 *   实测超过 15 条就被丢掉（手机只显示前 15 个键）。
 *   现在用 esp_timer 每 10ms 发一条 —— 不用建任务：内部 RAM 紧张时
 *   xTaskCreate 会静默失败（本项目踩过），定时器回调跑在 timer 任务上，更稳。
 */
static void tx_tick(void *arg)
{
    (void)arg;
    if (!br_ble_gatt_phone_connected()) {
        return;
    }
    char buf[96];
    if (s_tx_idx >= (int)s_list.count) {
        snprintf(buf, sizeof(buf), "{\"n\":%d}", (int)s_list.count);
        br_ble_gatt_phone_send("ckdone", buf);
        return;
    }
    int i = s_tx_idx;
    char esc[CKEYS_NAME * 2 + 1];
    int n = 0;
    for (const char *q = s_list.names[i]; *q && n < (int)sizeof(esc) - 2; q++) {
        if (*q == '"' || *q == '\\') {
            esc[n++] = '\\';
        }
        esc[n++] = *q;
    }
    esc[n] = 0;
    snprintf(buf, sizeof(buf),
             "{\"id\":%u,\"c\":%u,\"r\":%u,\"name\":\"%s\"}",
             (unsigned)s_list.ids[i], (unsigned)s_list.cols[i],
             (unsigned)s_list.rows[i], esc);
    if (!br_ble_gatt_phone_send("ck", buf)) {
        esp_timer_start_once(s_tx_timer, 30000);   /* 没发出去，稍后重试同一条 */
        return;
    }
    s_tx_idx = i + 1;
    esp_timer_start_once(s_tx_timer, 10000);
}

void br_ckeys_report(void)
{
    if (!s_tx_timer) {
        return;
    }
    s_tx_idx = 0;
    esp_timer_stop(s_tx_timer);
    esp_timer_start_once(s_tx_timer, 1000);
}
