#include "br_voice.h"

#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "br_usb.h"
#include "br_led.h"

static const char *TAG = "br_voice";
#define ATVV_DEFAULT_FRAME_SIZE 120
#define ATVV_MAX_FRAME_SIZE 256
#define PCM_RING_BYTES 32768

static const int s_step_table[89] = {
    7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,
    34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,
    157,173,190,209,230,253,279,307,337,371,408,449,494,544,
    598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,
    1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,
    5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,
    13899,15289,16818,18500,20350,22385,24623,27086,29794,32767
};
static const int8_t s_index_table[8] = {-1,-1,-1,-1,2,4,6,8};

static RingbufHandle_t s_pcm_rb;
/*
 * 手机语音的 PCM 缓冲（与 ATVV 分开）。
 *
 * ★ 必须分开 ★
 *   UAC1 端点只有一路 PCM。两路混在一个缓冲里 → 音频交织 → 撕裂/爆音。
 *   分开之后由 s_phone_active 决定 audio_tx_task 从哪个缓冲取。
 */
static RingbufHandle_t s_phone_rb;
static volatile bool s_phone_active;
/*
 * ★ voice_start 之前先到的 PCM（两个 HTTP 请求并发，谁先到不定）★
 *
 * ★ 为什么只有 4 KB ★
 *   这是 .bss，吃的是板子那 512KB 内部 RAM —— 而内部 RAM 正处于紧张状态：
 *   协议任务（cdc，8KB 栈）就是因为内存不够**创建不出来**，
 *   结果 PC 端发什么命令板子都不回（表现为「读不到固件版本」）。
 *   以前这里占 16KB，现在手机语音走 ADPCM（一包才 1600 字节），
 *   而且页面是**先发 voice_start 再发音频**，预卷几乎用不上 —— 4KB 足够。
 */
#define PHONE_PREROLL_BYTES 4096
static uint8_t s_preroll[PHONE_PREROLL_BYTES];
static size_t s_preroll_len;
/*
 * ★ 预卷缓冲必须加锁 ★
 *   HTTP 会话任务是并发的（最多 8 个 socket），
 *   两个 PCM 请求同时进来就会同时改 s_preroll / s_preroll_len ——
 *   数据竞争 + memcpy 重叠 → 内存踩坏，板子直接死机（现象：突然 ping 不通）。
 */
static SemaphoreHandle_t s_preroll_lock;
static uint16_t s_frame_size = ATVV_DEFAULT_FRAME_SIZE;
static uint8_t s_pending[ATVV_MAX_FRAME_SIZE];
static uint16_t s_pending_len;
static int32_t s_predictor;
static int s_step_index;
static bool s_streaming;
static bool s_decoder_synced;
static float s_hp_x, s_hp_y;
static bool s_hp_initialized;

typedef struct {
    uint8_t modifier;
    uint8_t count;
    uint8_t keys[6];
} voice_hotkey_t;

static voice_hotkey_t s_hotkey = {.modifier = 0x09}; /* Left Ctrl + Left GUI */
static bool s_enabled = true;   /* PC 端的「语音通道」开关 */
static voice_hotkey_t s_pressed_hotkey;
static bool s_hotkey_down;
static uint32_t s_pressed_at = 0;   /* 热键按下的时刻（ms），看门狗用 */
static volatile uint32_t s_last_audio_ms = 0;  /* 最近一帧语音的时间（ms） */
static portMUX_TYPE s_hotkey_mux = portMUX_INITIALIZER_UNLOCKED;

static bool save_hotkey(const voice_hotkey_t *hotkey)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open("br_voice", NVS_READWRITE, &nvs);
    if (err != ESP_OK) return false;
    err = nvs_set_blob(nvs, "hotkey", hotkey, sizeof(*hotkey));
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    return err == ESP_OK;
}

static bool voice_hotkey_press(void)
{
    if (!s_enabled) {
        /* ★ 语音通道被关了 → 热键根本没按出去 ★
           以前这里静默 return，事件照样发「触发了」——
           用户看日志以为板子按了，其实输入法根本没收到。 */
        return false;
    }
    portENTER_CRITICAL(&s_hotkey_mux);
    if (s_hotkey_down) {
        portEXIT_CRITICAL(&s_hotkey_mux);
        return true;    /* 已经按着了，热键还按着 */
    }
    s_pressed_hotkey = s_hotkey;
    s_hotkey_down = true;
    s_pressed_at = (uint32_t)(esp_timer_get_time() / 1000);
    portEXIT_CRITICAL(&s_hotkey_mux);
    /* 语音热键是「按住说话」，要豁免 HID 的 3 秒卡键保护 */
    bridge_usb_set_hold_exempt(true);
    /* 语音期间灯常亮（红色），松开才灭 —— 用户判断「正在收音」的第一反馈 */
    bridge_led_voice_active();
    bridge_usb_press_keyboard(s_pressed_hotkey.modifier,
                              s_pressed_hotkey.keys,
                              s_pressed_hotkey.count);
    return true;
}

static void voice_hotkey_release(void)
{
    voice_hotkey_t pressed;
    portENTER_CRITICAL(&s_hotkey_mux);
    if (!s_hotkey_down) {
        portEXIT_CRITICAL(&s_hotkey_mux);
        return;
    }
    pressed = s_pressed_hotkey;
    s_hotkey_down = false;
    portEXIT_CRITICAL(&s_hotkey_mux);
    bridge_usb_release_keyboard(pressed.modifier, pressed.keys, pressed.count);
    bridge_usb_set_hold_exempt(false);
    bridge_led_voice_off();
}

/*
 * ★ 语音热键看门狗（只处理「异常」，不限制正常说话时长）★
 *
 *   语音是「按住说话」：输入法要热键一直按着才收音。
 *   所以只要遥控器还在送音频，热键就必须一直按着 ——
 *   以前 2 秒就强制松开，说长句直接被截断（用户实测）。
 *
 *   这里只在下面三种异常时释放：
 *     1) 流开着，但连续 3 秒没有音频 → 遥控器停了却没发 AUDIO_STOP
 *     2) 热键按着，但根本没进入流状态 → 1.5 秒后放掉
 *     3) 极端兜底：按了 10 分钟还没结束
 *   正常结束时由 AUDIO_STOP 释放，与说话时长无关。
 */
#define BR_VOICE_STREAM_IDLE_MS      3000u
/* 手机语音走 WiFi + HTTP，包与包之间可能有秒级空档，判定要宽松 */
#define BR_VOICE_PHONE_IDLE_MS      10000u
#define BR_VOICE_NO_STREAM_GRACE_MS  1500u
#define BR_VOICE_ABSOLUTE_MAX_MS     600000u   /* 10 分钟硬上限 */

void br_voice_watchdog(void)
{
    bool down;
    uint32_t at;
    portENTER_CRITICAL(&s_hotkey_mux);
    down = s_hotkey_down;
    at = s_pressed_at;
    portEXIT_CRITICAL(&s_hotkey_mux);
    if (!down) {
        return;
    }
    bridge_led_voice_active();   /* 按住期间续期，灯不会因 800ms 空闲超时熄灭 */
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);

    if ((now - at) > BR_VOICE_ABSOLUTE_MAX_MS) {
        ESP_LOGW(TAG, "语音热键按住超过 10 分钟，兜底释放");
        voice_hotkey_release();
        s_streaming = false;
        s_phone_active = false;
        s_last_audio_ms = 0;
        return;
    }

    if (s_streaming) {
        uint32_t last = s_last_audio_ms ? s_last_audio_ms : at;
        /*
         * ★ 手机路径的空闲判定要宽松得多 ★
         *   遥控器（ATVV/BLE）的音频是连续不断流，3 秒没音频就是它停了。
         *   手机走的是 WiFi + HTTP：每个 PCM 包都要等 TCP 把最后一段送到
         *   （实测单包 250ms~5s），WiFi 一抖动就可能空档好几秒 ——
         *   用同一个 3 秒判定会把会话直接掐掉，语音永远起不来。
         *   手机路径给 10 秒，遥控器路径保持 3 秒。
         */
        uint32_t idle_limit = s_phone_active ? BR_VOICE_PHONE_IDLE_MS
                                              : BR_VOICE_STREAM_IDLE_MS;
        if ((now - last) > idle_limit) {
            ESP_LOGW(TAG, "语音流 %u ms 没有音频，视为结束并释放热键",
                     (unsigned)idle_limit);
            voice_hotkey_release();
            s_streaming = false;
            /* 手机语音也要一起停 —— 否则会一直占着 UAC 端点不供给 ATVV */
            s_phone_active = false;
            s_last_audio_ms = 0;
        }
        return;      /* 还在收音 → 绝不释放 */
    }

    if ((now - at) > BR_VOICE_NO_STREAM_GRACE_MS) {
        ESP_LOGW(TAG, "热键按着但没有语音流，释放");
        voice_hotkey_release();
    }
}

static int16_t decode_nibble(uint8_t nibble)
{
    int step = s_step_table[s_step_index];
    int diff = step >> 3;
    if (nibble & 1) diff += step >> 2;
    if (nibble & 2) diff += step >> 1;
    if (nibble & 4) diff += step;
    s_predictor += (nibble & 8) ? -diff : diff;
    if (s_predictor > 32767) s_predictor = 32767;
    if (s_predictor < -32768) s_predictor = -32768;
    s_step_index += s_index_table[nibble & 7];
    if (s_step_index < 0) s_step_index = 0;
    if (s_step_index > 88) s_step_index = 88;
    return (int16_t)s_predictor;
}

static void reset_decoder(void)
{
    s_predictor = 0;
    s_step_index = 0;
    s_pending_len = 0;
    s_hp_x = 0;
    s_hp_y = 0;
    s_hp_initialized = false;
    s_decoder_synced = true;
}

static void clear_pcm(void)
{
    if (!s_pcm_rb) return;
    size_t n = 0;
    void *item;
    while ((item = xRingbufferReceiveUpTo(s_pcm_rb, &n, 0, PCM_RING_BYTES)) != NULL) {
        vRingbufferReturnItem(s_pcm_rb, item);
    }
}

static void decode_frame(const uint8_t *frame, size_t len)
{
    int16_t pcm[ATVV_MAX_FRAME_SIZE * 2];
    float alpha = expf(-2.0f * 3.14159265f * 20.0f / 16000.0f);
    size_t samples = 0;
    for (size_t i = 0; i < len; i++) {
        int16_t pair[2] = {
            decode_nibble((uint8_t)(frame[i] >> 4)),
            decode_nibble((uint8_t)(frame[i] & 0x0f))
        };
        for (int j = 0; j < 2; j++) {
            float x = pair[j];
            if (!s_hp_initialized) {
                s_hp_x = x;
                s_hp_initialized = true;
            }
            float y = x - s_hp_x + alpha * s_hp_y;
            s_hp_x = x;
            s_hp_y = y;
            int32_t amplified = (int32_t)lrintf(y * 3.16227766f); /* +10 dB */
            if (amplified > 32767) amplified = 32767;
            if (amplified < -32768) amplified = -32768;
            pcm[samples++] = (int16_t)amplified;
        }
    }
    if (samples && s_pcm_rb) {
        xRingbufferSend(s_pcm_rb, pcm, samples * sizeof(pcm[0]), 0);
    }
}

esp_err_t br_voice_init(void)
{
    if (!s_pcm_rb) s_pcm_rb = xRingbufferCreate(PCM_RING_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (!s_pcm_rb) return ESP_ERR_NO_MEM;
    /* 预卷缓冲的锁（HTTP 会话任务是并发的） */
    if (!s_preroll_lock) {
        s_preroll_lock = xSemaphoreCreateMutex();
        if (!s_preroll_lock) return ESP_ERR_NO_MEM;
    }
    reset_decoder();
    s_streaming = false;
    nvs_handle_t nvs;
    esp_err_t err = nvs_open("br_voice", NVS_READWRITE, &nvs);
    if (err != ESP_OK) return err;
    size_t size = sizeof(s_hotkey);
    err = nvs_get_blob(nvs, "hotkey", &s_hotkey, &size);
    /*
     * ★ 语音通道开关也要持久化 ★
     *   以前只在 RAM 里，板子一重启就回默认值 ——
     *   而 PC 那边记着 config.json 的 voice_enabled，
     *   两边从此对不上，界面显示「已开启」但热键永远按不出去。
     *   ★ 用**独立的 key** ★：hotkey 那个 blob 已经在 NVS 里了，
     *   改结构体大小会让老数据读歪。
     */
    uint8_t enabled = 1;
    if (nvs_get_u8(nvs, "enabled", &enabled) == ESP_OK) {
        s_enabled = enabled != 0;
    }
    nvs_close(nvs);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        s_hotkey = (voice_hotkey_t){.modifier = 0x09};
        if (!save_hotkey(&s_hotkey)) return ESP_FAIL;
    } else if (err != ESP_OK || size != sizeof(s_hotkey) || s_hotkey.count > 6) {
        ESP_LOGW(TAG, "invalid persisted voice shortcut; restoring Ctrl+Win");
        s_hotkey = (voice_hotkey_t){.modifier = 0x09};
        if (!save_hotkey(&s_hotkey)) return ESP_FAIL;
    }
    return ESP_OK;
}

bool br_voice_set_hotkey(uint8_t modifier, const uint8_t *keys, uint8_t count)
{
    if (count > 6 || (count && !keys)) return false;
    voice_hotkey_t next = {.modifier = modifier, .count = count};
    for (uint8_t i = 0; i < count; i++) {
        if (!keys[i] || (keys[i] >= 0xE0 && keys[i] <= 0xE7)) return false;
        next.keys[i] = keys[i];
    }
    if (!save_hotkey(&next)) return false;
    portENTER_CRITICAL(&s_hotkey_mux);
    s_hotkey = next;
    portEXIT_CRITICAL(&s_hotkey_mux);
    return true;
}

bool br_voice_get_hotkey(uint8_t *modifier, uint8_t *keys, uint8_t *count)
{
    portENTER_CRITICAL(&s_hotkey_mux);
    if (modifier) *modifier = s_hotkey.modifier;
    if (count) *count = s_hotkey.count;
    if (keys && s_hotkey.count) {
        memcpy(keys, s_hotkey.keys, s_hotkey.count);
    }
    portEXIT_CRITICAL(&s_hotkey_mux);
    return true;
}

void br_voice_on_disconnect(void)
{
    if (s_hotkey_down) {
        ESP_LOGW(TAG, "链路断开，释放语音热键");
        voice_hotkey_release();
    }
    s_streaming = false;
    s_last_audio_ms = 0;
    s_pending_len = 0;
}

void br_voice_set_enabled(bool on)
{
    s_enabled = on;
    /* ★ 存进 NVS：板子重启后还是这个状态 ★ */
    nvs_handle_t nvs;
    if (nvs_open("br_voice", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_u8(nvs, "enabled", on ? 1 : 0);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
    if (!on) {
        voice_hotkey_release();
    }
}

bool br_voice_enabled(void)
{
    return s_enabled;
}

bool br_voice_control(const uint8_t *data, size_t len)
{
    bool pressed = false;
    if (!data || !len) return false;
    switch (data[0]) {
    case 0x0B: /* CAPS: v1.0 version, codec bits, interaction, frame length */
        if (len >= 7) {
            uint16_t frame = (uint16_t)(((uint16_t)data[5] << 8) | data[6]);
            if (frame > 0 && frame <= ATVV_MAX_FRAME_SIZE) {
                s_frame_size = frame;
                ESP_LOGI(TAG, "ATVV caps version=%u.%u codec=0x%02x frame=%u",
                         data[1], data[2], data[3], s_frame_size);
            }
        }
        break;
    case 0x04: /* AUDIO_START */
        clear_pcm();
        reset_decoder();
        pressed = voice_hotkey_press();
        s_streaming = true;
        s_last_audio_ms = (uint32_t)(esp_timer_get_time() / 1000);
        ESP_LOGI(TAG, "AUDIO_START reason=%u codec=%u session=%u frame=%u",
                 len > 1 ? data[1] : 0, len > 2 ? data[2] : 0,
                 len > 3 ? data[3] : 0, s_frame_size);
        break;
    case 0x00: /* AUDIO_STOP */
        voice_hotkey_release();
        s_streaming = false;
        s_last_audio_ms = 0;
        s_pending_len = 0;
        ESP_LOGI(TAG, "AUDIO_STOP reason=%u", len > 1 ? data[1] : 0);
        break;
    case 0x0A: /* AUDIO_SYNC predictor and step index */
        if (len >= 7) {
            s_predictor = (int16_t)(((uint16_t)data[4] << 8) | data[5]);
            s_step_index = data[6] > 88 ? 88 : data[6];
            s_hp_initialized = false;
            s_decoder_synced = true;
        }
        break;
    default:
        break;
    }
    /* ★ 有没有真的把语音热键按出去 ★（事件要靠它说实话） */
    return pressed;
}

void br_voice_audio(const uint8_t *data, size_t len)
{
    if (!s_streaming || !data || !len || s_frame_size == 0) return;
    s_last_audio_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (!s_decoder_synced) reset_decoder();
    for (size_t i = 0; i < len; i++) {
        s_pending[s_pending_len++] = data[i];
        if (s_pending_len == s_frame_size) {
            decode_frame(s_pending, s_pending_len);
            s_pending_len = 0;
        }
    }
}

size_t br_voice_read_pcm(void *dst, size_t max_bytes)
{
    RingbufHandle_t rb = s_phone_active ? s_phone_rb : s_pcm_rb;
    size_t got = 0;
    void *item;

    if (!rb || !dst || !max_bytes) return 0;
    item = xRingbufferReceiveUpTo(rb, &got, 0, max_bytes);
    if (!item) return 0;
    memcpy(dst, item, got);
    vRingbufferReturnItem(rb, item);
    return got;
}

bool br_voice_streaming(void)
{
    return s_streaming || s_phone_active;
}

/* ------------------------------------------------------------ 手机语音 */

void br_voice_phone_start(void)
{
    if (s_phone_active) {
        return;
    }
    if (!s_phone_rb) {
        s_phone_rb = xRingbufferCreate(PCM_RING_BYTES, RINGBUF_TYPE_BYTEBUF);
        if (!s_phone_rb) {
            ESP_LOGE(TAG, "手机语音缓冲分配失败");
            return;
        }
    }
    /* 清掉上一次的残留 */
    {
        size_t n = 0;
        void *item;
        while ((item = xRingbufferReceiveUpTo(s_phone_rb, &n, 0, PCM_RING_BYTES)) != NULL) {
            vRingbufferReturnItem(s_phone_rb, item);
        }
    }

    /*
     * ★ 仲裁：手机是主动方，ATVV 让位 ★
     *   手机按住说话时如果遥控器也在收音，两路会交织 → 撕裂。
     *   这里让 ATVV 停掉。
     */
    if (s_streaming) {
        s_streaming = false;
        clear_pcm();
        ESP_LOGI(TAG, "手机语音开始，ATVV 语音让位");
    }

    s_phone_active = true;
    s_streaming = true;
    s_last_audio_ms = (uint32_t)(esp_timer_get_time() / 1000);
    /* 补上 voice_start 之前先到的那段，别把开头半句话丢了 */
    if (s_preroll_lock) {
        xSemaphoreTake(s_preroll_lock, portMAX_DELAY);
    }
    if (s_preroll_len && s_phone_rb) {
        xRingbufferSend(s_phone_rb, s_preroll, s_preroll_len, 0);
        ESP_LOGI(TAG, "补进预卷音频 %u 字节", (unsigned)s_preroll_len);
        s_preroll_len = 0;
    }
    if (s_preroll_lock) {
        xSemaphoreGive(s_preroll_lock);
    }
    voice_hotkey_press();
    ESP_LOGI(TAG, "手机语音开始（热键已按下，等待 PC 输入法收音）");
}

void br_voice_phone_end(void)
{
    if (!s_phone_active) {
        return;
    }
    s_phone_active = false;
    s_streaming = false;
    s_last_audio_ms = 0;
    voice_hotkey_release();
    ESP_LOGI(TAG, "手机语音结束");
}

void br_voice_push_phone_pcm(const uint8_t *pcm, size_t len)
{
    static uint32_t s_total;
    static uint32_t s_last_log;
    if (!s_phone_active) {
        /*
         * ★ voice_start 还没到就先存着 ★
         *   voice_start 与 PCM 是两个并发的 HTTP 请求，板子不一定先处理
         *   voice_start —— PCM 先到就会被扔掉，开头那半句话就没了。
         *   这里留一段预卷，voice_start 到了再补进缓冲。
         */
        if (pcm && len) {
            if (s_preroll_lock) {
                xSemaphoreTake(s_preroll_lock, portMAX_DELAY);
            }
            if (len >= sizeof(s_preroll)) {
                memcpy(s_preroll, pcm, sizeof(s_preroll));
                s_preroll_len = sizeof(s_preroll);
            } else {
                if (s_preroll_len + len > sizeof(s_preroll)) {
                    size_t drop = s_preroll_len + len - sizeof(s_preroll);
                    memmove(s_preroll, s_preroll + drop,
                            s_preroll_len - drop);
                    s_preroll_len -= drop;
                }
                memcpy(s_preroll + s_preroll_len, pcm, len);
                s_preroll_len += len;
            }
            if (s_preroll_lock) {
                xSemaphoreGive(s_preroll_lock);
            }
        }
        return;
    }
    if (!pcm || !len || !s_phone_rb) {
        return;
    }
    s_last_audio_ms = (uint32_t)(esp_timer_get_time() / 1000);
    /* 缓冲满了就丢弃这一段 —— 宁可丢音频，也不能阻塞 WiFi 收包的线程 */
    xRingbufferSend(s_phone_rb, pcm, len, 0);

    /* ★ 取证：每秒一行累计字节与峰值 ★ */
    s_total += (uint32_t)len;
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    if (now - s_last_log > 1000) {
        s_last_log = now;
        int32_t peak = 0;
        for (size_t i = 0; i < len; i += 2) {
            int16_t v = (int16_t)((uint16_t)pcm[i] | ((uint16_t)pcm[i + 1] << 8));
            if (v < 0) v = -v;
            if (v > peak) peak = v;
        }
        ESP_LOGI(TAG, "手机PCM 累计 %u 字节 峰值 %d", (unsigned)s_total, (int)peak);
    }
}

/* ------------------------------------------------------------ 手机 ADPCM */

/*
 * ★ 手机语音和遥控器语音走同一套 ADPCM 解码 ★
 *   步长表/索引表就是上面 ATVV 用的那两张（标准 IMA ADPCM，4bit/样本）。
 *   手机端在页面里用同一套算法编码，板子这边解码后送进同一条 UAC 麦克风端点 ——
 *   所以手机和遥控器在 PC 那边是**一模一样**的麦克风输入。
 *
 *   为什么非要压成 ADPCM：
 *   手机 → 板子走 WiFi，实测一次请求固定开销 ~90ms、带宽 ~15KB/s。
 *   16kHz 裸 PCM 要 32KB/s，这条链路送不到实时（音频只剩一半）。
 *   ADPCM 压 4 倍（8KB/s）才有实时余量。
 */
static int32_t s_pad_pred;      /* 手机 ADPCM 解码器状态（预测值） */
static int s_pad_idx;           /* 手机 ADPCM 解码器状态（步长索引） */

static int16_t pad_decode_nibble(uint8_t nibble)
{
    int step = s_step_table[s_pad_idx];
    int diff = step >> 3;
    if (nibble & 1) diff += step >> 2;
    if (nibble & 2) diff += step >> 1;
    if (nibble & 4) diff += step;
    s_pad_pred += (nibble & 8) ? -diff : diff;
    if (s_pad_pred > 32767) s_pad_pred = 32767;
    if (s_pad_pred < -32768) s_pad_pred = -32768;
    s_pad_idx += s_index_table[nibble & 7];
    if (s_pad_idx < 0) s_pad_idx = 0;
    if (s_pad_idx > 88) s_pad_idx = 88;
    return (int16_t)s_pad_pred;
}

void br_voice_push_phone_adpcm(const uint8_t *data, size_t len, bool reset)
{
    if (!data || !len) {
        return;
    }
    if (reset) {
        /* 本次录音的第一包：解码器状态从头对齐（手机端也是新建编码器） */
        s_pad_pred = 0;
        s_pad_idx = 0;
    }
    /*
     * 一个 ADPCM 字节 = 两个采样（高半字节在前，和 ATVV 的解码顺序一致）。
     * 解出来的 PCM 立刻交给手机 PCM 那条路（活跃时进环形缓冲，
     * 否则进预卷）—— 和裸 PCM 完全同一条通路。
     */
    int16_t pcm[128 * 2];
    size_t n = 0;
    for (size_t i = 0; i < len; i++) {
        pcm[n++] = pad_decode_nibble((uint8_t)(data[i] >> 4));
        pcm[n++] = pad_decode_nibble((uint8_t)(data[i] & 0x0f));
        if (n >= sizeof(pcm) / sizeof(pcm[0])) {
            br_voice_push_phone_pcm((const uint8_t *)pcm, n * sizeof(pcm[0]));
            n = 0;
        }
    }
    if (n) {
        br_voice_push_phone_pcm((const uint8_t *)pcm, n * sizeof(pcm[0]));
    }
}

