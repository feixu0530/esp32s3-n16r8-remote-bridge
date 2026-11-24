/*
 * bridge_led - 板载 WS2812 RGB 灯
 * ---------------------------------------------------------------------------
 * 用途：直观反馈「板子是否收到了遥控器的按键指令」。
 *
 * 行为约定：
 *   - 收到遥控器按键 -> 亮淡黄色
 *   - 短按（< 400ms）-> 闪一下，约 250ms 后熄灭
 *   - 长按（>= 400ms）-> 持续常亮，直到松手才熄灭
 *   - 语音通道工作 -> 红色常亮（优先级最高，用于区分「在录音」）
 *   - 固件启动 -> 青色快闪 3 次作为自检
 *
 * 硬件：ESP32-S3-N16R8 板载 WS2812 接在 GPIO48，通道顺序为 GRB（标准 WS2812）。
 * 用 RMT 外设产生 800kHz 时序，不做 bit-bang（见 VL 的注释：±150ns 容差，
 * 软件翻转做不到）。
 */
#include "br_led.h"
#include <string.h>
#include "esp_timer.h"
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <esp_log.h>
#include "driver/rmt_tx.h"

static const char *TAG = "br_led";

/* 灯珠引脚：N16R8 板载 WS2812 */
#define LED_GPIO_NUM       48

/* RMT 编码参数：10MHz -> 1 tick = 0.1us */
#define LED_RMT_RESOLUTION_HZ  (10 * 1000 * 1000)
#define LED_T0H_TICKS          3      /* 0.30us */
#define LED_T0L_TICKS          9      /* 0.90us */
#define LED_T1H_TICKS          9      /* 0.90us */
#define LED_T1L_TICKS          3      /* 0.30us */
#define LED_RESET_TICKS        500    /* 50us 低电平 */

/* ESP32-S3 每个 RMT 通道 48 个符号，单帧 24bit + 1 reset = 25，取 48 是上限 */
#define LED_RMT_MEM_SYMBOLS    48

/* 短按判定阈值：超过此值视为长按 */
#define LED_LONG_PRESS_MS      400
/* 短按闪亮持续时长 */
#define LED_FLASH_MS           250
/* 语音通道红色常亮的判定：超过此时长没有新帧则认为语音已停 */
#define LED_VOICE_IDLE_MS      800

typedef enum {
    LED_MODE_OFF = 0,
    LED_MODE_KEY,        /* 按键指示（蓝） */
    LED_MODE_VOICE,      /* 语音通道（红） */
    LED_MODE_CONNECTING, /* 正在找遥控器（蓝，呼吸） */
} led_mode_t;

static rmt_channel_handle_t  s_chan;
static rmt_encoder_handle_t s_encoder;
static bool                 s_ready = false;

/* 编码回调（定义在下方，但 init 里要用，先声明） */
static size_t led_encode_cb(const void *data, size_t data_size,
                             size_t symbols_written, size_t symbols_free,
                             rmt_symbol_word_t *symbols, bool *done, void *arg);

static led_mode_t s_mode = LED_MODE_OFF;
/* 手机（BLE App）在线也算「有链路」—— 否则手机能用时灯还在蓝呼吸 */
static bool s_phone_linked;
/* 按键指示的状态机 */
static bool     s_key_held = false;       /* 键是否仍按住 */
static uint32_t s_key_press_at = 0;       /* 按下时刻 */
static uint32_t s_key_flash_until = 0;    /* 短按熄灭时刻 */
static bool     s_key_long = false;       /* 是否已进入长按 */

static uint32_t s_last_voice_ms = 0;

/* BLE 连接状态：false = 正在找遥控器（呼吸），true = 已连上（等按键） */
static bool s_connected = false;
static bool s_pairing = false;   /* 配对窗口开着 → 快闪蓝 */
static int64_t s_pair_ok_until = 0;  /* 配对成功提示（绿灯快闪） */
static int64_t s_phone_act_until = 0;   /* 手机输入反馈（青灯亮一小会儿） */
static uint32_t s_act_count = 0;        /* 累计触发次数（排障用） */
static int64_t s_phone_act_last = 0;    /* 上次触发时刻（限流用） */

void bridge_led_init(void)
{
    rmt_tx_channel_config_t tx_cfg = {
        .gpio_num          = LED_GPIO_NUM,
        .clk_src           = RMT_CLK_SRC_DEFAULT,
        .resolution_hz     = LED_RMT_RESOLUTION_HZ,
        .mem_block_symbols = LED_RMT_MEM_SYMBOLS,
        .trans_queue_depth = 4,
        .intr_priority     = 0,
        .flags = {
            .invert_out  = 0,
            .with_dma    = 0,
            .allow_pd    = 0,
            .init_level  = 0,
        },
    };
    esp_err_t err = rmt_new_tx_channel(&tx_cfg, &s_chan);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "rmt_new_tx_channel failed: %s", esp_err_to_name(err));
        return;
    }

    rmt_simple_encoder_config_t enc_cfg = {
        .callback       = led_encode_cb,
        .arg            = NULL,
        .min_chunk_size = LED_RMT_MEM_SYMBOLS,
    };
    err = rmt_new_simple_encoder(&enc_cfg, &s_encoder);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "rmt_new_simple_encoder failed: %s", esp_err_to_name(err));
        return;
    }

    err = rmt_enable(s_chan);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "rmt_enable failed: %s", esp_err_to_name(err));
        return;
    }
    s_ready = true;

    /* 启动自检：青色快闪 3 次 */
    for (int i = 0; i < 3; i++) {
        bridge_led_raw(0, 40, 40);
        vTaskDelay(pdMS_TO_TICKS(60));
        bridge_led_raw(0, 0, 0);
        vTaskDelay(pdMS_TO_TICKS(60));
    }
    ESP_LOGI(TAG, "LED ready on GPIO%d", LED_GPIO_NUM);
}

static size_t led_encode_cb(const void *data, size_t data_size,
                             size_t symbols_written, size_t symbols_free,
                             rmt_symbol_word_t *symbols, bool *done, void *arg)
{
    (void)arg;
    const uint8_t *bytes = (const uint8_t *)data;
    const size_t bits_total = data_size * 8;
    const size_t total = bits_total + 1;   /* +1 = reset 符号 */

    rmt_symbol_word_t b0, b1, rst;
    b0.level0 = 1; b0.duration0 = LED_T0H_TICKS;
    b0.level1 = 0; b0.duration1 = LED_T0L_TICKS;
    b1.level0 = 1; b1.duration0 = LED_T1H_TICKS;
    b1.level1 = 0; b1.duration1 = LED_T1L_TICKS;
    rst.level0 = 0; rst.duration0 = LED_RESET_TICKS;
    rst.level1 = 0; rst.duration1 = LED_RESET_TICKS;

    size_t written = 0;
    size_t idx = symbols_written;

    while (written < symbols_free && idx < total) {
        if (idx == bits_total) {
            symbols[written] = rst;
        } else {
            const uint8_t byte = bytes[idx >> 3];
            const bool one = (byte >> (7 - (idx & 7))) & 0x01;
            symbols[written] = one ? b1 : b0;
        }
        written++;
        idx++;
    }
    if (idx >= total) {
        *done = true;
    }
    return written;
}

/* ★ 整体亮度 ★
 *   WS2812 满亮度很刺眼（尤其夜里），统一压到 55%：
 *   所有状态（按键蓝、语音粉、呼吸、配对闪）都按这个比例走，
 *   改一处即可全局调暗。 */
#define LED_BRIGHTNESS_NUM 38
#define LED_BRIGHTNESS_DEN 100

void bridge_led_raw(uint8_t r, uint8_t g, uint8_t b)
{
    if (!s_ready) {
        return;
    }
    r = (uint8_t)((uint16_t)r * LED_BRIGHTNESS_NUM / LED_BRIGHTNESS_DEN);
    g = (uint8_t)((uint16_t)g * LED_BRIGHTNESS_NUM / LED_BRIGHTNESS_DEN);
    b = (uint8_t)((uint16_t)b * LED_BRIGHTNESS_NUM / LED_BRIGHTNESS_DEN);
    /* WS2812 数据顺序 GRB（标准） */
    uint8_t bytes[3] = { g, r, b };
    rmt_transmit_config_t tc = {
        .loop_count = 0,
        .flags = { .eot_level = 0, .queue_nonblocking = 0 },
    };
    esp_err_t err = rmt_transmit(s_chan, s_encoder, bytes, sizeof(bytes), &tc);
    if (err != ESP_OK) {
        return;
    }
    rmt_tx_wait_all_done(s_chan, 30);
}

/**
 * 按键指示色：**蓝色**。
 *
 * 2026-11-05 用户要求：
 *   「我按过按键了，但是我并不知道效果怎么样。可以增加一下灯效，
 *     在我按任意键的时候，RGB 灯都亮暗一些的蓝色。这样可以知道按键是否
 *     被开发板接收到。」
 *   「注意 RGB，我不希望常亮，因为那容易发热。我只想在按键的时候亮就可以。
 *     短按就亮一次，长按不放，就长亮。」
 *
 * 所以行为是：
 *   按下       -> 亮蓝 (0, 90, 230)
 *   松开       -> 熄灭 (0, 0, 0)
 *   短按       -> 亮一下就灭
 *   长按不放   -> 一直亮着
 *
 * ★ 绝不常亮 ★：WS2812 常亮既发热又费电，用完必须灭。
 */
static void led_set_key(void)
{
    bridge_led_raw(0, 90, 230);
}

void bridge_led_key_down(void)
{
    uint32_t now = xTaskGetTickCount() * portTICK_PERIOD_MS;
    s_key_held = true;
    s_key_press_at = now;
    s_key_long = false;
    s_key_flash_until = 0;
    s_mode = LED_MODE_KEY;
    led_set_key();
}

void bridge_led_key_up(void)
{
    (void)xTaskGetTickCount();
    s_key_held = false;
    s_key_flash_until = 0;
    /* 松手即灭 —— 不留常亮底色 */
    s_mode = LED_MODE_OFF;
    bridge_led_raw(0, 0, 0);
}

void bridge_led_voice_off(void)
{
    if (s_mode == LED_MODE_VOICE) {
        s_mode = LED_MODE_OFF;
        bridge_led_raw(0, 0, 0);
    }
}

void bridge_led_voice_active(void)
{
    s_last_voice_ms = xTaskGetTickCount() * portTICK_PERIOD_MS;
    if (s_mode != LED_MODE_VOICE) {
        s_mode = LED_MODE_VOICE;
        bridge_led_raw(220, 70, 140);   /* 粉色：语音收音中 */
    }
}

void bridge_led_voice_idle(void)
{
    s_last_voice_ms = xTaskGetTickCount() * portTICK_PERIOD_MS;
}

/**
 * 设置 BLE 连接状态。
 *
 * 用户问「怎么知道板子在不在找遥控器」—— 这个状态灯就是答案：
 *   未连接 -> 蓝色呼吸（1.2 秒一个来回）
 *   已连接 -> 熄灭（等按键时再亮）
 */
void bridge_led_set_connected(bool on)
{
    s_connected = on;
}

void bridge_led_set_phone_linked(bool on)
{
    s_phone_linked = on;
}

void bridge_led_set_pairing(bool on)
{
    s_pairing = on;
}

void bridge_led_pair_ok(void)
{
    s_pair_ok_until = (int64_t)(xTaskGetTickCount() * portTICK_PERIOD_MS) + 1500;
}

void bridge_led_connecting(void)
{
    s_connected = false;
}

void bridge_led_idle(void)
{
    /* 真正的「空闲」由连接状态决定，这里只清掉呼吸 */
    if (s_mode != LED_MODE_KEY && s_mode != LED_MODE_VOICE) {
        s_mode = LED_MODE_OFF;
    }
}

uint32_t bridge_led_activity_count(void)
{
    return s_act_count;
}

void bridge_led_phone_activity(void)
{
    int64_t now = esp_timer_get_time();
    /* 60ms 限流：30Hz 的 move 也能变成「常亮」，不会闪成一条 */
    if (now - s_phone_act_last < 60000) {
        return;
    }
    s_phone_act_last = now;
    s_phone_act_until = now + 150000;   /* 停手 150ms 后灭 */
    s_act_count++;
}

void bridge_led_task(void *arg)
{
    (void)arg;
    uint32_t last_voice = xTaskGetTickCount() * portTICK_PERIOD_MS;

    while (1) {
        uint32_t now = xTaskGetTickCount() * portTICK_PERIOD_MS;

        /*
         * 状态灯优先级：按键 > 语音 > 连接呼吸。
         *
         *   未连接 -> 蓝色呼吸（1.2 秒一个来回），告诉用户「板子在找遥控器」
         *   已连接 -> 熄灭，等按键
         */
        /*
         * ★ 配对窗口：快速闪蓝 ★
         *   优先级最高（除了按键本身）——
         *   「未连接」的慢呼吸太温和，用户根本注意不到。
         */
        /* 配对成功提示优先：绿灯快闪 1.5 秒 */
        if (now < (uint32_t)s_pair_ok_until && s_mode != LED_MODE_KEY) {
            uint32_t phase = now % 200;
            uint8_t level = (phase < 100) ? 100 : 0;
            bridge_led_raw(0, level, 0);
        } else if (esp_timer_get_time() < s_phone_act_until && s_mode != LED_MODE_KEY) {
            /* 手机有输入：青色（用户指定；比白灯柔和，和配对蓝/成功绿也区分开） */
            bridge_led_raw(0, 150, 150);
        } else if (s_pairing && s_mode != LED_MODE_KEY) {
            uint32_t phase = now % 400;
            uint8_t level = (phase < 200) ? 90 : 0;
            bridge_led_raw(0, 0, level);
        } else if (s_mode != LED_MODE_KEY && s_mode != LED_MODE_VOICE) {
            if (!s_connected && !s_phone_linked) {
                uint32_t phase = now % 1200;
                uint8_t level;
                if (phase < 600) {
                    level = (uint8_t)(phase / 6);           /* 0 -> 100 */
                } else {
                    level = (uint8_t)((1200 - phase) / 6); /* 100 -> 0 */
                }
                bridge_led_raw(0, (uint8_t)(level / 3), (uint8_t)level);
            } else {
                bridge_led_raw(0, 0, 0);
            }
        }

        /* 语音超时 -> 回到按键模式 */
        if (s_mode == LED_MODE_VOICE &&
            (now - s_last_voice_ms) > LED_VOICE_IDLE_MS) {
            s_mode = LED_MODE_OFF;
            bridge_led_raw(0, 0, 0);
        }

        /* 短按闪烁到期 -> 熄灭 */
        if (s_mode == LED_MODE_KEY && !s_key_held &&
            s_key_flash_until != 0 && now >= s_key_flash_until) {
            s_key_flash_until = 0;
            s_mode = LED_MODE_OFF;
            bridge_led_raw(0, 0, 0);
        }

        /* 按住超过阈值 -> 视为长按，保持常亮（颜色不变，只是状态标记） */
        if (s_mode == LED_MODE_KEY && s_key_held && !s_key_long) {
            if ((now - s_key_press_at) >= LED_LONG_PRESS_MS) {
                s_key_long = true;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
