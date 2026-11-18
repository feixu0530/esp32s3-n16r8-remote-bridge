#include "br_voice.h"

#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "br_usb.h"

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
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);

    if ((now - at) > BR_VOICE_ABSOLUTE_MAX_MS) {
        ESP_LOGW(TAG, "语音热键按住超过 10 分钟，兜底释放");
        voice_hotkey_release();
        s_streaming = false;
        s_last_audio_ms = 0;
        return;
    }

    if (s_streaming) {
        uint32_t last = s_last_audio_ms ? s_last_audio_ms : at;
        if ((now - last) > BR_VOICE_STREAM_IDLE_MS) {
            ESP_LOGW(TAG, "语音流 %u ms 没有音频，视为结束并释放热键",
                     (unsigned)BR_VOICE_STREAM_IDLE_MS);
            voice_hotkey_release();
            s_streaming = false;
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
    if (!s_pcm_rb || !dst || !max_bytes) return 0;
    size_t got = 0;
    void *item = xRingbufferReceiveUpTo(s_pcm_rb, &got, 0, max_bytes);
    if (!item) return 0;
    memcpy(dst, item, got);
    vRingbufferReturnItem(s_pcm_rb, item);
    return got;
}

bool br_voice_streaming(void)
{
    return s_streaming;
}

