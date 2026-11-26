#include "br_mapping.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include <esp_log.h>
#include <nvs.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "br_proto.h"
#include "br_ble_core.h"
#include "br_usb.h"
#include "br_gesture.h"

#define MAP_MAGIC 0x50414D42u /* BMAP */
#define MAP_VERSION 1u
#define MAP_CACHE_COUNT 8

typedef struct {
    uint32_t magic;
    uint16_t version;
    uint16_t count;
    br_bind_t binds[BR_MAX_BINDS];
} map_blob_t;

typedef struct {
    bool valid;
    uint8_t addr[6];
    map_blob_t blob;
} map_cache_t;

static const char *TAG = "br_mapping";
static map_blob_t s_stage;
static uint8_t s_stage_addr[6];
static bool s_stage_active;
static map_cache_t s_cache[MAP_CACHE_COUNT];
static SemaphoreHandle_t s_lock;

/*
 * ★ NVS 命名空间的专用锁 ★
 *   "keymap" 这个命名空间会被两处打开：
 *     · cache_get()   —— 读（手势任务每 10 ms 会读一次）
 *     · br_mapping_commit() —— 写（CDC 任务）
 *   两个任务并发打开同一个命名空间时，NVS 会偶发返回
 *   ESP_ERR_NVS_INVALID_STATE —— 表现就是「保存映射失败」。
 *   用这把锁把命名空间的开关都串起来，问题就没了。
 */
static SemaphoreHandle_t s_nvs_lock;

static bool mapping_has_trig(void *ctx, const uint8_t addr[6], uint32_t key_id,
                             uint8_t trig);
static void mapping_fire(void *ctx, const uint8_t addr[6], uint32_t key_id,
                         uint8_t trig, bool press);

static bool parse_addr(const char *s, uint8_t out[6])
{
    return br_ble_addr_parse(s, out);
}

static void map_nvs_key(const uint8_t addr[6], char key[15])
{
    snprintf(key, 15, "m%02X%02X%02X%02X%02X%02X",
             addr[0], addr[1], addr[2], addr[3], addr[4], addr[5]);
}

static map_cache_t *cache_get(const uint8_t addr[6])
{
    map_cache_t *free_slot = NULL;
    for (int i = 0; i < MAP_CACHE_COUNT; i++) {
        if (s_cache[i].valid && memcmp(s_cache[i].addr, addr, 6) == 0) {
            return &s_cache[i];
        }
        if (!s_cache[i].valid && !free_slot) free_slot = &s_cache[i];
    }
    if (!free_slot) free_slot = &s_cache[0];
    memset(free_slot, 0, sizeof(*free_slot));
    memcpy(free_slot->addr, addr, 6);
    free_slot->valid = true;
    free_slot->blob.magic = MAP_MAGIC;
    free_slot->blob.version = MAP_VERSION;

    nvs_handle_t h;
    if (s_nvs_lock) xSemaphoreTake(s_nvs_lock, portMAX_DELAY);
    bool opened = (nvs_open("keymap", NVS_READONLY, &h) == ESP_OK);
    if (opened) {
        char key[15]; size_t size = sizeof(free_slot->blob);
        map_nvs_key(addr, key);
        if (nvs_get_blob(h, key, &free_slot->blob, &size) != ESP_OK ||
            size != sizeof(free_slot->blob) ||
            free_slot->blob.magic != MAP_MAGIC ||
            free_slot->blob.version != MAP_VERSION ||
            free_slot->blob.count > BR_MAX_BINDS) {
            free_slot->blob.magic = MAP_MAGIC;
            free_slot->blob.version = MAP_VERSION;
            free_slot->blob.count = 0;
        }
        nvs_close(h);
    }
    if (s_nvs_lock) xSemaphoreGive(s_nvs_lock);
    return free_slot;
}

void br_mapping_init(void)
{
    if (!s_lock) s_lock = xSemaphoreCreateMutex();
    if (!s_nvs_lock) s_nvs_lock = xSemaphoreCreateMutex();
    memset(s_cache, 0, sizeof(s_cache));
    memset(&s_stage, 0, sizeof(s_stage));
    s_stage_active = false;
    br_gesture_init(mapping_has_trig, mapping_fire, NULL);
}

bool br_mapping_begin(const char *addr)
{
    if (!parse_addr(addr, s_stage_addr)) return false;
    memset(&s_stage, 0, sizeof(s_stage));
    s_stage.magic = MAP_MAGIC;
    s_stage.version = MAP_VERSION;
    s_stage_active = true;
    return true;
}

bool br_mapping_set(uint32_t key_id, uint8_t trigger, uint8_t output,
                    uint8_t mods, const uint8_t *keys, uint8_t nkeys)
{
    if (!s_stage_active || trigger >= BR_TRIG_COUNT ||
        output > BR_OUT_CONSUMER || nkeys > BR_MAX_KEYS ||
        (nkeys && !keys) || (output == BR_OUT_NONE && nkeys)) return false;
    int slot = -1;
    for (int i = 0; i < s_stage.count; i++) {
        if (s_stage.binds[i].key_id == key_id &&
            s_stage.binds[i].trig == trigger) { slot = i; break; }
    }
    if (output == BR_OUT_NONE) {
        if (slot >= 0) {
            for (int i = slot; i + 1 < s_stage.count; i++)
                s_stage.binds[i] = s_stage.binds[i + 1];
            memset(&s_stage.binds[--s_stage.count], 0, sizeof(br_bind_t));
        }
        return true;
    }
    if (slot < 0) {
        if (s_stage.count >= BR_MAX_BINDS) return false;
        slot = s_stage.count++;
    }
    br_bind_t *b = &s_stage.binds[slot];
    memset(b, 0, sizeof(*b));
    b->key_id = key_id; b->trig = trigger; b->out = output;
    b->mods = mods; b->nkeys = nkeys;
    if (nkeys) memcpy(b->keys, keys, nkeys);
    return true;
}

bool br_mapping_commit(void)
{
    if (!s_stage_active) return false;

    char key[15];
    map_nvs_key(s_stage_addr, key);

    esp_err_t rc = ESP_FAIL;
    /*
     * NVS 的命名空间开关必须串行（见 s_nvs_lock 的说明）。
     * 这里再加重试：万一碰上别的瞬时错误，别让用户的映射白存。
     */
    for (int attempt = 0; attempt < 3; attempt++) {
        nvs_handle_t h;
        if (s_nvs_lock) xSemaphoreTake(s_nvs_lock, portMAX_DELAY);
        if (nvs_open("keymap", NVS_READWRITE, &h) != ESP_OK) {
            if (s_nvs_lock) xSemaphoreGive(s_nvs_lock);
            rc = ESP_FAIL;
        } else {
            rc = nvs_set_blob(h, key, &s_stage, sizeof(s_stage));
            if (rc == ESP_OK) rc = nvs_commit(h);
            nvs_close(h);
            if (s_nvs_lock) xSemaphoreGive(s_nvs_lock);
            if (rc == ESP_OK) break;
        }
        ESP_LOGW(TAG, "commit attempt %d failed: %s", attempt + 1,
                 esp_err_to_name(rc));
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (rc != ESP_OK) return false;

    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    map_cache_t *c = cache_get(s_stage_addr);
    c->blob = s_stage;
    if (s_lock) xSemaphoreGive(s_lock);
    s_stage_active = false;
    ESP_LOGI(TAG, "saved %u mappings", s_stage.count);
    return true;
}

/**
 * 这台遥控器在板子上配了几条映射。
 * 设备页要显示「已配 N 条映射」—— 以前那里写死 0，看着像没配过。
 */
int br_mapping_count(const uint8_t addr[6])
{
    if (!addr) {
        return 0;
    }
    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    map_cache_t *c = cache_get(addr);
    int count = (int)c->blob.count;
    if (s_lock) xSemaphoreGive(s_lock);
    return count;
}

/**
 * 删掉这台遥控器的全部映射。
 *
 * ★ 解绑时必须一起删 ★
 *   否则「删掉 → 重新配对」时板子上还留着旧映射，
 *   br_mapping_apply_defaults 看到「已经有映射」就不铺出厂默认了 ——
 *   用户重配完还是不能用，必须再点一次「恢复内置默认」。
 */
bool br_mapping_clear(const uint8_t addr[6])
{
    if (!addr) {
        return false;
    }
    if (s_stage_active) {
        return false;           /* PC 正在写 —— 稍后再来 */
    }
    memset(&s_stage, 0, sizeof(s_stage));
    s_stage.magic = MAP_MAGIC;
    s_stage.version = MAP_VERSION;
    s_stage.count = 0;
    memcpy(s_stage_addr, addr, 6);
    s_stage_active = true;
    bool ok = br_mapping_commit();
    if (!ok) {
        s_stage_active = false;
    }
    ESP_LOGI(TAG, "映射已清空 ok=%d", ok ? 1 : 0);
    return ok;
}

/* ---------------------------------------------------------------- 出厂默认 */

/*
 * ★ 出厂默认映射 —— 按遥控器型号分档 ★
 *
 * 不同型号的遥控器键位不一样，默认映射也不该一样。
 * 现在只有一档：谷歌遥控器。以后要支持别的遥控器，
 * 就加一张 binds 表 + 在 k_default_profiles 里加一行。
 *
 * ★ 为什么名字对不上就不铺 ★
 *   给一台不认识的遥控器铺一整套谷歌键位，它按下去全是错的动作，
 *   比「什么都不配」更让人困惑 —— 所以对不上档案就干脆不铺。
 *
 * 只在「这台遥控器在板子上一条映射都没有」时铺，**用户配过就绝不覆盖**。
 */

typedef struct {
    uint32_t key_id;
    uint8_t  trig;
    uint8_t  out;
    uint8_t  mods;
    uint8_t  nkeys;
    uint8_t  keys[BR_MAX_KEYS];
} br_default_bind_t;

typedef struct {
    const char              *id;           /* 档案 id（记在板子的配对表里） */
    const char              *name_match;   /* 广播名里含这个串就命中（忽略大小写） */
    const char              *label;        /* 给人看的名字 */
    const br_default_bind_t *binds;
    int                      count;
} br_default_profile_t;

/*
 * 谷歌遥控器（Chromecast Media Remote）
 *   方向键、确定=回车、返回=退格、主页=显示桌面(Win+D)、
 *   音量+/-/静音、YouTube=应用切换、电源=Esc、输入源=回车。
 * （PowerShell/终端这类**只在 PC 上做**的动作不下发到板子 ——
 *   板子只会敲键，不会开程序。）
 */
static const br_default_bind_t k_binds_chromecast[] = {
    { 8739u, 0, 1, 0x08, 1, { 0x07 } },     /* 主页   → Win+D 显示桌面 */
    { 8258u, 0, 1, 0x00, 1, { 0x52 } },     /* 上     → ↑ */
    { 8260u, 0, 1, 0x00, 1, { 0x50 } },     /* 左     → ← */
    { 8261u, 0, 1, 0x00, 1, { 0x4F } },     /* 右     → → */
    { 8259u, 0, 1, 0x00, 1, { 0x51 } },     /* 下     → ↓ */
    { 8425u, 0, 2, 0x00, 1, { 0xE9 } },     /* 音量+  → 消费者音量+ */
    { 8426u, 0, 2, 0x00, 1, { 0xEA } },     /* 音量-  → 消费者音量- */
    { 8418u, 0, 2, 0x00, 1, { 0xE2 } },     /* 静音   → 消费者静音 */
    { 8311u, 0, 1, 0x04, 1, { 0x2B } },     /* YouTube→ Alt+Tab 应用切换 */
    { 8606u, 0, 1, 0x00, 1, { 0x29 } },     /* 电源   → Esc */
    { 8585u, 0, 1, 0x00, 1, { 0x28 } },     /* 输入源 → Enter */
    { 8257u, 0, 1, 0x00, 1, { 0x28 } },     /* 确定   → Enter */
    { 8740u, 0, 1, 0x00, 1, { 0x2A } },     /* 返回   → Backspace */
    { 8740u, 2, 1, 0x00, 1, { 0x2A } },     /* 返回 长按 → Backspace（连着删） */
};

/*
 * 手机（移动端）的快捷键栏
 *
 *   只有触控板页快捷键栏那三个键（esc / 音量+ / 音量-）走映射表 ——
 *   键盘页敲的字是字面输入，直接发 HID 键盘报告，不进映射表。
 *
 * key_id = (BR_SRC_PHONE << 12) | usage，与遥控器的 key_id 空间完全隔离。
 */
static const br_default_bind_t k_binds_phone[] = {
    { BR_KEY_MAKE(BR_SRC_PHONE, BR_PHU_ESC),         0, 1, 0x00, 1, { 0x29 } },  /* Esc → ESC */
    { BR_KEY_MAKE(BR_SRC_PHONE, BR_PHU_VOLUME_UP),   0, 2, 0x00, 1, { 0xE9 } },  /* 音量+ → 消费者音量+ */
    { BR_KEY_MAKE(BR_SRC_PHONE, BR_PHU_VOLUME_DOWN), 0, 2, 0x00, 1, { 0xEA } },  /* 音量- → 消费者音量- */
    { BR_KEY_MAKE(BR_SRC_PHONE, BR_PHU_MUTE),        0, 2, 0x00, 1, { 0xE2 } },  /* 静音 → 消费者静音 */
    { BR_KEY_MAKE(BR_SRC_PHONE, BR_PHU_PLAY_PAUSE),  0, 2, 0x00, 1, { 0xCD } },  /* 播放 → 消费者播放/暂停 */
};

/*
 * 小米蓝牙语音遥控器一代（广播名 "MI RC" / "小米蓝牙语音遥控器"）
 *
 * ★ 实测（2026-11-24，证据见 dev/docs/xiaomi-gen1-capture.md）★
 *   它的按键走**键盘页**（Usage Page 0x07），8 字节标准键盘报告：
 *     byte0=修饰键, byte1=保留, byte2..7=键码
 *   所以 key_id = (BR_SRC_KEYBOARD << 12) | usage ——
 *   与谷歌遥控器的消费者页（src=2）空间天然隔离，互不覆盖。
 *
 *   语音键（usage 0x3E）不进映射表：它是语音通道的触发，不是普通按键。
 */
static const br_default_bind_t k_binds_xiaomi[] = {
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x52), 0, 1, 0x00, 1, { 0x52 } },  /* 上   → ↑ */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x51), 0, 1, 0x00, 1, { 0x51 } },  /* 下   → ↓ */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x50), 0, 1, 0x00, 1, { 0x50 } },  /* 左   → ← */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x4F), 0, 1, 0x00, 1, { 0x4F } },  /* 右   → → */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x28), 0, 1, 0x00, 1, { 0x28 } },  /* 确定 → Enter */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x4A), 0, 1, 0x08, 1, { 0x07 } },  /* 主页 → Win+D 显示桌面 */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0xF1), 0, 1, 0x00, 1, { 0x2A } },  /* 返回 → Backspace */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0xF1), 2, 1, 0x00, 1, { 0x2A } },  /* 返回 长按 → Backspace */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x65), 0, 1, 0x02, 1, { 0x43 } },  /* 菜单 → Shift+F10 右键菜单 */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x80), 0, 2, 0x00, 1, { 0xE9 } },  /* 音量+ → 消费者音量+ */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x81), 0, 2, 0x00, 1, { 0xEA } },  /* 音量- → 消费者音量- */
    { BR_KEY_MAKE(BR_SRC_KEYBOARD, 0x66), 0, 1, 0x00, 1, { 0x29 } },  /* 电源 → Esc */
};

/*
 * 型号档案表。
 *
 * name_match 里可以用 '|' 分隔多个匹配串（大小写不敏感）：
 *   同一个型号在不同固件/不同场景下的广播名不一样
 *   （小米这台实测见过 "MI RC"，Windows 侧还见过 "小米蓝牙语音遥控器"）。
 */
static const br_default_profile_t k_default_profiles[] = {
    { "chromecast", "chromecast", "谷歌遥控器", k_binds_chromecast,
      (int)(sizeof(k_binds_chromecast) / sizeof(k_binds_chromecast[0])) },
    { "xiaomi", "mi rc|小米蓝牙语音遥控器|xiaomi remote|xiaomi bluetooth remote",
      "小米遥控器", k_binds_xiaomi,
      (int)(sizeof(k_binds_xiaomi) / sizeof(k_binds_xiaomi[0])) },
};

/** 名字里是否含有 needle（忽略大小写） */
static bool name_contains(const char *hay, const char *needle)
{
    if (!hay || !needle || !*needle) {
        return false;
    }
    size_t nl = strlen(needle);
    for (const char *p = hay; *p; p++) {
        size_t i = 0;
        while (i < nl && p[i] &&
               tolower((unsigned char)p[i]) == tolower((unsigned char)needle[i])) {
            i++;
        }
        if (i == nl) {
            return true;
        }
    }
    return false;
}

/** 按广播名找默认档案。找不到返回 NULL（不认识就不乱配）。 */
/**
 * 档案的 name_match 里可以有多个匹配串（'|' 分隔），命中任意一个就算。
 *
 * 同一个型号在不同固件下广播名不一样
 * （小米这台实测广播名是 "MI RC"；Windows 侧还见过 "小米蓝牙语音遥控器"）。
 */
static bool profile_name_matches(const br_default_profile_t *p, const char *name)
{
    if (!p || !p->name_match || !name || !name[0]) {
        return false;
    }
    /* name_match 是一串 '|' 分隔的匹配串，命中任意一个就算 */
    const char *cur = p->name_match;
    while (cur && *cur) {
        const char *sep = strchr(cur, '|');
        size_t len = sep ? (size_t)(sep - cur) : strlen(cur);
        if (len > 0) {
            char one[64];
            if (len >= sizeof(one)) {
                len = sizeof(one) - 1;
            }
            memcpy(one, cur, len);
            one[len] = '\0';
            if (name_contains(name, one)) {
                return true;
            }
        }
        cur = sep ? sep + 1 : NULL;
    }
    return false;
}

static const br_default_profile_t *profile_for_name(const char *name)
{
    if (!name || !name[0]) {
        return NULL;
    }
    int n = (int)(sizeof(k_default_profiles) / sizeof(k_default_profiles[0]));
    for (int i = 0; i < n; i++) {
        if (profile_name_matches(&k_default_profiles[i], name)) {
            return &k_default_profiles[i];
        }
    }
    return NULL;
}

/**
 * 按遥控器名字（或指定的档案 id）找默认档案。
 * 找不到返回 NULL —— 不认识就不铺，绝不乱配键位。
 */
const char *br_mapping_profile_id_for_name(const char *name)
{
    const br_default_profile_t *p = profile_for_name(name);
    return p ? p->id : NULL;
}

static const br_default_profile_t *profile_by_id(const char *id)
{
    if (!id || !id[0]) {
        return NULL;
    }
    int n = (int)(sizeof(k_default_profiles) / sizeof(k_default_profiles[0]));
    for (int i = 0; i < n; i++) {
        if (strcmp(k_default_profiles[i].id, id) == 0) {
            return &k_default_profiles[i];
        }
    }
    return NULL;
}

int br_mapping_apply_defaults(const char *addr_str, const char *name,
                              const char *profile_in, char *out_profile)
{
    if (!addr_str || !addr_str[0]) {
        return 0;
    }
    uint8_t addr[6];
    if (!parse_addr(addr_str, addr)) {
        return 0;
    }
    /*
     * ★ 档案按「型号 + MAC」定 ★
     *   同一台重配 → 配对表里存着上次用的档案 id → 用回同一套；
     *   新的台 → 按名字匹配，匹配到的 id 由调用方记到配对表里。
     */
    const br_default_profile_t *prof = profile_by_id(profile_in);
    if (!prof) {
        prof = profile_for_name(name);
    }
    if (!prof) {
        ESP_LOGI(TAG, "没有匹配的默认档案（%s）→ 不铺默认",
                 (name && name[0]) ? name : "无名");
        return 0;
    }
    if (out_profile) {
        snprintf(out_profile, 16, "%s", prof->id);
    }
    if (br_mapping_count(addr) > 0) {
        return 0;               /* 用户配过 —— 绝不覆盖 */
    }
    if (s_stage_active) {
        return -1;              /* PC 正在写 —— 等一下再来 */
    }

    int n = prof->count;
    if (n > BR_MAX_BINDS) {
        n = BR_MAX_BINDS;
    }
    memset(&s_stage, 0, sizeof(s_stage));
    s_stage.magic = MAP_MAGIC;
    s_stage.version = MAP_VERSION;
    s_stage.count = (uint16_t)n;
    for (int i = 0; i < n; i++) {
        br_bind_t *b = &s_stage.binds[i];
        b->key_id = prof->binds[i].key_id;
        b->trig = prof->binds[i].trig;
        b->out = prof->binds[i].out;
        b->mods = prof->binds[i].mods;
        b->nkeys = prof->binds[i].nkeys;
        memcpy(b->keys, prof->binds[i].keys, sizeof(b->keys));
    }
    memcpy(s_stage_addr, addr, 6);
    s_stage_active = true;
    bool ok = br_mapping_commit();
    if (!ok) {
        s_stage_active = false; /* 别把暂存区卡住 */
    }
    ESP_LOGI(TAG, "出厂默认映射已铺好：%s %d 条 ok=%d", prof->label, n, ok ? 1 : 0);
    return ok ? n : 0;
}

/**
 * 给「手机」铺出厂默认映射。
 *
 * 手机不是 BLE 设备，没有广播名可匹配档案，所以单独开一个入口。
 * 用的是同一个暂存区/提交机制，与遥控器共用一套存储与下发路径。
 *
 * ★ 用户配过就绝不覆盖 ★（与遥控器同样的规矩）
 */
int br_mapping_apply_phone_defaults(void)
{
    static const uint8_t phone_addr[6] = BR_PHONE_ADDR;
    int n = (int)(sizeof(k_binds_phone) / sizeof(k_binds_phone[0]));

    if (br_mapping_count(phone_addr) > 0) {
        return 0;               /* 用户配过 —— 绝不覆盖 */
    }
    if (s_stage_active) {
        return -1;              /* PC 正在写 —— 等一下再来 */
    }
    if (n > BR_MAX_BINDS) {
        n = BR_MAX_BINDS;
    }
    memset(&s_stage, 0, sizeof(s_stage));
    s_stage.magic = MAP_MAGIC;
    s_stage.version = MAP_VERSION;
    s_stage.count = (uint16_t)n;
    for (int i = 0; i < n; i++) {
        br_bind_t *b = &s_stage.binds[i];
        b->key_id = k_binds_phone[i].key_id;
        b->trig = k_binds_phone[i].trig;
        b->out = k_binds_phone[i].out;
        b->mods = k_binds_phone[i].mods;
        b->nkeys = k_binds_phone[i].nkeys;
        memcpy(b->keys, k_binds_phone[i].keys, sizeof(b->keys));
    }
    memcpy(s_stage_addr, phone_addr, 6);
    s_stage_active = true;
    bool ok = br_mapping_commit();
    if (!ok) {
        s_stage_active = false;
    }
    ESP_LOGI(TAG, "手机出厂默认映射已铺好：%d 条 ok=%d", n, ok ? 1 : 0);
    return ok ? n : 0;
}

bool br_mapping_dump(const char *addr)
{
    uint8_t parsed[6];
    if (!parse_addr(addr, parsed)) return false;
    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    map_cache_t *c = cache_get(parsed);
    map_blob_t snapshot = c->blob;
    if (s_lock) xSemaphoreGive(s_lock);
    for (int i = 0; i < snapshot.count; i++) {
        br_bind_t *b = &snapshot.binds[i];
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "key", b->key_id);
        br_kv_add_int(&kv, "trig", b->trig);
        br_kv_add_int(&kv, "out", b->out);
        br_kv_add_int(&kv, "mods", b->mods);
        br_kv_add_hex(&kv, "keys", b->keys, b->nkeys);
        br_proto_send_dt("MAP", &kv);
    }
    br_kv_t kv; br_kv_init(&kv);
    br_kv_add_int(&kv, "count", snapshot.count);
    br_proto_send_ok("MAPGET", &kv);
    return true;
}

/* ------------------------------------------------------------------ 手势层 */

/*
 * ★ 双击 / 长按的判定在板子上做 ★
 *   理由：用户要求「按键规则保留在开发板，PC 端只是配置页面」。
 *   拔掉 PC，板子也得能区分单击 / 双击 / 长按。
 *
 *   br_gesture 只管时序，**不知道映射内容**；映射内容在这里通过
 *   has/fire 两个回调喂给它。职责分开后手势引擎可以独立单测。
 */

/* ------------------------------------------------ 按下记录（抬起兜底） */

/*
 * ★ 抬起不能只靠「重新查表」★
 *   按住期间映射可能被 PC 改写（MAPCLEAR/MAPSET）或缓存被重载 ——
 *   抬起时查不到绑定就静默返回，那个键会永远按在 PC 上，
 *   系统自动重复 → 输入框文字被一次删光（用户实测过）。
 *   所以按下成功就把「实际发出去的键」记下来，抬起优先按记录释放。
 */
#define FIRE_REC_SLOTS 8

typedef struct {
    bool     used;
    uint8_t  addr[6];
    uint32_t key_id;
    uint8_t  trig;
    uint8_t  out;
    uint8_t  mods;
    uint8_t  nkeys;
    uint8_t  keys[BR_MAX_KEYS];
} fire_rec_t;

static fire_rec_t s_fire_rec[FIRE_REC_SLOTS];
static portMUX_TYPE s_fire_mux = portMUX_INITIALIZER_UNLOCKED;

static void fire_record(const uint8_t addr[6], uint32_t key_id, uint8_t trig,
                        const br_bind_t *b)
{
    taskENTER_CRITICAL(&s_fire_mux);
    fire_rec_t *slot = NULL;
    for (int i = 0; i < FIRE_REC_SLOTS; i++) {
        if (s_fire_rec[i].used && s_fire_rec[i].key_id == key_id &&
            s_fire_rec[i].trig == trig &&
            memcmp(s_fire_rec[i].addr, addr, 6) == 0) {
            slot = &s_fire_rec[i];
            break;
        }
    }
    if (!slot) {
        for (int i = 0; i < FIRE_REC_SLOTS; i++) {
            if (!s_fire_rec[i].used) {
                slot = &s_fire_rec[i];
                break;
            }
        }
    }
    if (!slot) {
        slot = &s_fire_rec[0]; /* 理论上到不了；真到了就覆盖最旧的 */
    }
    slot->used = true;
    memcpy(slot->addr, addr, 6);
    slot->key_id = key_id;
    slot->trig = trig;
    slot->out = b->out;
    slot->mods = b->mods;
    slot->nkeys = b->nkeys > BR_MAX_KEYS ? BR_MAX_KEYS : b->nkeys;
    memset(slot->keys, 0, sizeof(slot->keys));
    memcpy(slot->keys, b->keys, slot->nkeys);
    taskEXIT_CRITICAL(&s_fire_mux);
}

/** 取出并清掉一条按下记录 */
static bool fire_take(const uint8_t addr[6], uint32_t key_id, uint8_t trig,
                      fire_rec_t *out)
{
    bool found = false;
    taskENTER_CRITICAL(&s_fire_mux);
    for (int i = 0; i < FIRE_REC_SLOTS; i++) {
        fire_rec_t *r = &s_fire_rec[i];
        if (r->used && r->key_id == key_id && r->trig == trig &&
            memcmp(r->addr, addr, 6) == 0) {
            *out = *r;
            r->used = false;
            found = true;
            break;
        }
    }
    taskEXIT_CRITICAL(&s_fire_mux);
    return found;
}

/** 找某个键某种触发的绑定。找到返回 true，绑定拷进 out。 */
static bool find_bind(const uint8_t addr[6], uint32_t key_id, uint8_t trig,
                      br_bind_t *out)
{
    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    map_cache_t *c = cache_get(addr);
    map_blob_t snapshot = c->blob;
    if (s_lock) xSemaphoreGive(s_lock);
    for (int i = 0; i < snapshot.count; i++) {
        const br_bind_t *b = &snapshot.binds[i];
        if (b->key_id == key_id && b->trig == trig) {
            if (out) {
                *out = *b;
            }
            return true;
        }
    }
    return false;
}

/** 手势引擎问：这个键配了这种触发吗？ */
static bool mapping_has_trig(void *ctx, const uint8_t addr[6], uint32_t key_id,
                             uint8_t trig)
{
    (void)ctx;
    return find_bind(addr, key_id, trig, NULL);
}

/**
 * 这个键配了映射吗（任意触发方式都算）。
 *
 * 手机端的快捷键要用它判断「用户配过没有」：
 * 配过 → 走映射表；没配 → 用出厂默认行为。
 */
bool br_mapping_has_key(const uint8_t addr[6], uint32_t key_id)
{
    if (!addr) {
        return false;
    }
    return mapping_has_trig(NULL, addr, key_id, BR_TRIG_SINGLE) ||
           mapping_has_trig(NULL, addr, key_id, BR_TRIG_DOUBLE) ||
           mapping_has_trig(NULL, addr, key_id, BR_TRIG_LONG);
}

/** 手势引擎说：这个触发的 press / release 动作，执行它。 */
static void mapping_fire(void *ctx, const uint8_t addr[6], uint32_t key_id,
                         uint8_t trig, bool press)
{
    (void)ctx;
    br_bind_t b;
    bool have_bind = find_bind(addr, key_id, trig, &b);

    if (press) {
        if (!have_bind) {
            return;
        }
        fire_record(addr, key_id, trig, &b);
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "key", key_id);
        br_kv_add_int(&kv, "trig", trig);
        br_kv_add_int(&kv, "out", b.out);
        br_kv_add_int(&kv, "hid_ready", bridge_usb_hid_ready());
        /*
         * ★ 把「转成了什么键」一起报上去 ★
         *   以前只有 key/trig/out，PC 端根本还原不出「按了上键变成 Ctrl+L」，
         *   日志里只能显示工程视角的十六进制。
         */
        char addr_s[18];
        /*
         * ★ 地址必须用 br_addr_to_str ★
         *   NimBLE 的地址是小端字节序，显示时要从 addr[5] 排到 addr[0]。
         *   这里以前手写成 addr[0]..addr[5] —— 和设备页显示的正好反过来，
         *   PC 端按地址串匹配遥控器 → 匹配不上 → 人话日志没有名字、MAC 还是反的。
         */
        br_addr_to_str(addr, addr_s, sizeof(addr_s));
        br_kv_add(&kv, "addr", addr_s);
        br_kv_add_int(&kv, "mods", b.mods);
        br_kv_add_hex(&kv, "keys", b.keys, b.nkeys);
        br_proto_send_ev("MAPOUT", &kv);
    } else if (!have_bind) {
        /*
         * ★ 按住期间映射被改也要抬得起来 ★
         *   以前这里直接 return —— 键永远按在 PC 上，系统自动重复删光文字。
         */
        fire_rec_t rec;
        if (!fire_take(addr, key_id, trig, &rec)) {
            ESP_LOGW(TAG, "抬起无绑定也无按下记录 key=%u trig=%u（忽略）",
                     (unsigned)key_id, (unsigned)trig);
            return;
        }
        ESP_LOGW(TAG, "映射已变，按按下记录抬起 key=%u trig=%u",
                 (unsigned)key_id, (unsigned)trig);
        if (rec.out == BR_OUT_KEYBOARD) {
            bridge_usb_release_keyboard(rec.mods, rec.keys, rec.nkeys);
        } else if (rec.out == BR_OUT_CONSUMER && rec.nkeys >= 1) {
            uint16_t usage = rec.keys[0];
            if (rec.nkeys > 1) usage |= (uint16_t)rec.keys[1] << 8;
            bridge_usb_release_consumer(usage);
        }
        return;
    } else {
        /* 正常抬起：顺手清掉按下记录，别占着槽位 */
        fire_rec_t rec;
        (void)fire_take(addr, key_id, trig, &rec);
    }
    if (b.out == BR_OUT_KEYBOARD) {
        if (press) bridge_usb_press_keyboard(b.mods, b.keys, b.nkeys);
        else bridge_usb_release_keyboard(b.mods, b.keys, b.nkeys);
    } else if (b.out == BR_OUT_CONSUMER && b.nkeys >= 1) {
        uint16_t usage = b.keys[0];
        if (b.nkeys > 1) usage |= (uint16_t)b.keys[1] << 8;
        if (press) bridge_usb_press_consumer(usage);
        else bridge_usb_release_consumer(usage);
    }
}

void br_mapping_key(const uint8_t addr[6], uint32_t key_id, bool down)
{
    if (down) {
        /* 完全没配的键报个 MISS，纯诊断用 */
        if (!mapping_has_trig(NULL, addr, key_id, BR_TRIG_SINGLE) &&
            !mapping_has_trig(NULL, addr, key_id, BR_TRIG_DOUBLE) &&
            !mapping_has_trig(NULL, addr, key_id, BR_TRIG_LONG)) {
            br_kv_t kv; br_kv_init(&kv);
            br_kv_add_int(&kv, "key", key_id);
            br_proto_send_ev("MAPMISS", &kv);
        }
        br_gesture_press(addr, key_id);
    } else {
        br_gesture_release(addr, key_id);
    }
}

