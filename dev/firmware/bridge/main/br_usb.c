/*
 * bridge_usb - USB 复合设备实现
 * ---------------------------------------------------------------------------
 * 接口分配：
 *   Interface 0: HID 键盘       (boot keyboard, 8 字节报告)
 *   Interface 1: HID 消费者控制  (16 位 usage)
 *   Interface 2: CDC 串口       (协议通道)
 */
#include "br_usb.h"
#include "br_uart.h"
#include "br_voice.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <esp_log.h>
#include <esp_log_write.h>
#include <esp_mac.h>
#include <esp_timer.h>
#include <tinyusb.h>
#include "tinyusb_default_config.h"
#include "class/hid/hid_device.h"
#include "class/audio/audio_device.h"
#include "class/cdc/cdc_device.h"
#include "tinyusb_cdc_acm.h"

static const char *TAG = "br_usb";

/* ---------------------------------------------------------------- 描述符 */

/*
 * HID 报告描述符。
 *
 * 每个 HID 接口用**自己独立**的报告描述符，不共用一份含多个 report id 的描述符。
 * 原因：Windows 会比对每个接口的 GET_DESCRIPTOR(REPORT) 返回内容，
 * 两个接口返回同一份描述符会被判为冲突，表现为 CDC 接口 ProblemCode 10。
 *
 * 接口 2 = 键盘（boot protocol），接口 3 = 消费者控制。
 */
/*
 * 接口与端点分配
 * ---------------------------------------------------------------------------
 * 接口编号（顺序很重要）：
 *   0 (+1) = CDC 通信接口 + CDC 数据接口（由 TUD_CDC_DESCRIPTOR 带出）
 *   2      = HID 键盘
 *   3      = HID 消费者控制
 *
 * 为什么 CDC 放最前：
 *   设备级 bDeviceClass = 0xEF(Misc) + IAD 的组合是给「CDC 主导」的复合设备用的。
 *   Windows 解析 IAD 时会把从 IAD 起始接口号开始的连续接口都归到该功能组，
 *   所以 CDC 的 IAD 必须放在接口 0，否则它后面的 HID 接口会被误归类，
 *   结果是 HID 正常但 CDC 起不来（ProblemCode 10）。
 *
 * 端点分配（ESP32-S3 硬件限制：5 个 IN/OUT 端点 + 1 个 IN）：
 *
 *   ★ 关键约束：ESP32-S3 的 DWC2 用**端点号**索引共享的 FIFO，
 *     同一个功能的 IN 和 OUT **必须用同一个端点号**。
 *     之前 CDC 数据端点写成 OUT 0x04 / IN 0x85（端点号不同），
 *     结果：CDC 能枚举（COM 口出现）、RX 方向也正常（命令能收到），
 *     但 TX 方向永远发不出去 ——
 *     固件日志显示 write_queue 成功、write_flush 超时（ESP_ERR_TIMEOUT），
 *     主机侧永远收不到应答。
 *
 *   修正后（与 esp_tinyusb 官方默认描述符一致）：
 *     CDC 通知   IN  0x81   (EP1)
 *     CDC 数据   OUT 0x82 / IN 0x82   (EP2，同号)
 *     HID 键盘   IN  0x83   (EP3)
 *     HID 消费者 IN  0x84   (EP4)
 */
#define TUD_AUDIO10_DESC_IAD_LEN 8
#define TUSB_DESC_TOTAL_LEN  (TUD_CONFIG_DESC_LEN + TUD_HID_DESC_LEN + TUD_CDC_DESC_LEN + TUD_AUDIO10_DESC_IAD_LEN + TUD_AUDIO10_MIC_ONE_CH_DESC_LEN(1))

/**
 * HID 报告描述符。
 *
 * 两个 HID 接口**共享同一份**报告描述符（内含两个 report id：
 * 1 = 键盘，2 = 消费者控制），接口描述符里声明的长度都是这份总长。
 *
 * 这与 Arduino-ESP32 的做法一致（CMCC 用的就是它）：
 *   cores/esp32/USBHID.cpp 的 tud_hid_descriptor_report_cb() 返回
 *   的是所有 HID 设备拼起来的**总表**，而不是每个接口一份。
 */
static const uint8_t s_hid_report_desc[] = {
    TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(1)),
    TUD_HID_REPORT_DESC_CONSUMER(HID_REPORT_ID(2)),
};

static const uint8_t s_config_descriptor[] = {
    /* 配置：1 配置 / 3 接口 / 字符串 0 / 总长 / 属性 / 电流
     *
     * ★ 接口数 = 3，不是 2！
     *   接口 0     = HID（键盘 + 消费者控制，report id 区分）
     *   接口 1     = CDC 通信接口（控制）
     *   接口 2     = CDC 数据接口
     *   TUD_CDC_DESCRIPTOR 一个宏就带出 2 个接口，所以总数是 1 + 2 = 3。
     *   写错成 2 会让 Windows 认为描述符长度与接口数不符 → 直接 Code 10。
     */
    TUD_CONFIG_DESCRIPTOR(1, 5, 0x07, TUSB_DESC_TOTAL_LEN,
                          TUSB_DESC_CONFIG_ATT_SELF_POWERED, 500),

    /* ★ 接口 0：单一 HID 接口，键盘与消费者控制用 report id 区分
     *
     * ★★★ 这是 CMCC 的做法，也是 CDC 能工作的关键：
     *
     *   实测 CMCC 官方固件（PID 1001）在 Windows 上的设备实例：
     *     MI_00 = HID 键盘 + 消费者控制（Keyboard + 符合 HID 标准的用户控制设备）
     *     MI_01 = CDC 串口 (COM11)
     *   **只有 2 个 USB 接口**，而不是 4 个。
     *
     *   我原来做 4 个接口（HID键盘 / HID消费者 / CDC控制 / CDC数据）时：
     *   - 多了 1 个 IN 端点 → 256 字节的 USB FIFO 更紧张
     *   - CDC 数据 IN 端点只能被挤到 EP5 → 超出 dieptxf_regs[4] → TX 发不出去
     *
     *   合并成 1 个 HID 接口后，IN 端点只剩 2 个（HID + CDC数据），
     *   CDC 数据 IN 端点可以放在 EP2/EP3 这种有硬件 FIFO 的低编号上。
     *
     *   报告描述符里用两个 report id 区分（与 Arduino-ESP32 相同）：
     *     report id 1 = 键盘，report id 2 = 消费者控制
     *   键盘报告 8 字节（1 report id + 1 修饰键 + 6 键码），
     *   消费者报告 3 字节（1 report id + 2 字节 usage），都塞得进 8 字节端点。
     */
    TUD_HID_DESCRIPTOR(0, 4, HID_ITF_PROTOCOL_KEYBOARD,
                       sizeof(s_hid_report_desc), 0x81, 16, 10),

    /* 接口 1(+2): CDC —— 通知 EP 0x83(IN,8B), 数据 OUT 0x02 / IN 0x82
     *
     * 端点分配：
     *   IN 端点总数 = HID(EP1) + CDC数据(EP2) = 2 个（EP0 之外）
     *   → EP1, EP2 都在 dieptxf_regs[4] 范围内 ✅
     *   OUT 端点 = EP2（与 IN 同号，符合 DWC2 的端点号索引模型）
     *
     *   FIFO 核算（ESP32-S3 otg_dfifo_depth = 256，4 字节 1 word）：
     *     EP0 IN 64B → 256→240
     *     HID 0x81 IN 8B → 240→238
     *     CDC IN 0x82 64B → 238→222（dieptxf[1] = EP2 ✅）
     *     RX FIFO = calc_device_grxfsiz(64, 7) = 62 → 222 ≥ 16+62 ✅ 充裕
     */
    TUD_CDC_DESCRIPTOR(1, 6, 0x83, 8, 0x02, 0x82, 64),

    /* Group UAC1 control/stream interfaces so Windows binds one audio function. */
    TUD_AUDIO10_DESC_IAD_LEN, TUSB_DESC_INTERFACE_ASSOCIATION,
    3, 2, TUSB_CLASS_AUDIO, AUDIO_SUBCLASS_CONTROL, AUDIO_INT_PROTOCOL_CODE_V1, 0,

    /* UAC1 mono microphone, 16 kHz / signed 16-bit PCM on interface 3-4. */
    TUD_AUDIO10_MIC_ONE_CH_DESCRIPTOR(3, 0, 2, 16, 0x84, 64, 16000),
};

static uint32_t s_audio_sample_rate = 16000;
static int16_t s_audio_packet[16];
static uint8_t s_audio_mute[2] = {0, 0};
static int16_t s_audio_volume[2] = {0, 0};

bool tud_audio_set_req_ep_cb(uint8_t rhport,
                             tusb_control_request_t const *request,
                             uint8_t *buffer)
{
    (void)rhport;
    if (TU_U16_HIGH(request->wValue) != AUDIO10_EP_CTRL_SAMPLING_FREQ ||
        request->bRequest != AUDIO10_CS_REQ_SET_CUR || request->wLength != 3) {
        return false;
    }
    uint32_t rate = (uint32_t)buffer[0] |
                    ((uint32_t)buffer[1] << 8) |
                    ((uint32_t)buffer[2] << 16);
    if (rate != 16000) return false;
    s_audio_sample_rate = rate;
    return true;
}

bool tud_audio_get_req_ep_cb(uint8_t rhport,
                             tusb_control_request_t const *request)
{
    if (TU_U16_HIGH(request->wValue) != AUDIO10_EP_CTRL_SAMPLING_FREQ ||
        request->bRequest != AUDIO10_CS_REQ_GET_CUR) {
        return false;
    }
    static uint8_t rate[3];
    rate[0] = (uint8_t)s_audio_sample_rate;
    rate[1] = (uint8_t)(s_audio_sample_rate >> 8);
    rate[2] = (uint8_t)(s_audio_sample_rate >> 16);
    return tud_audio_buffer_and_schedule_control_xfer(rhport, request,
                                                       rate, sizeof(rate));
}

bool tud_audio_set_req_entity_cb(uint8_t rhport,
                                 tusb_control_request_t const *request,
                                 uint8_t *buffer)
{
    (void)rhport;
    if (TU_U16_HIGH(request->wIndex) != 2) return false;
    uint8_t channel = TU_U16_LOW(request->wValue);
    uint8_t selector = TU_U16_HIGH(request->wValue);
    if (channel > 1 || request->bRequest != AUDIO10_CS_REQ_SET_CUR) return false;
    if (selector == AUDIO10_FU_CTRL_MUTE && request->wLength == 1) {
        s_audio_mute[channel] = buffer[0] ? 1 : 0;
        return true;
    }
    if (selector == AUDIO10_FU_CTRL_VOLUME && request->wLength == 2) {
        s_audio_volume[channel] = (int16_t)tu_unaligned_read16(buffer);
        return true;
    }
    return false;
}

bool tud_audio_get_req_entity_cb(uint8_t rhport,
                                 tusb_control_request_t const *request)
{
    if (TU_U16_HIGH(request->wIndex) != 2) return false;
    uint8_t channel = TU_U16_LOW(request->wValue);
    uint8_t selector = TU_U16_HIGH(request->wValue);
    if (channel > 1) return false;
    if (selector == AUDIO10_FU_CTRL_MUTE &&
        request->bRequest == AUDIO10_CS_REQ_GET_CUR) {
        return tud_audio_buffer_and_schedule_control_xfer(
            rhport, request, &s_audio_mute[channel], sizeof(s_audio_mute[channel]));
    }
    if (selector != AUDIO10_FU_CTRL_VOLUME) return false;
    int16_t value;
    switch (request->bRequest) {
    case AUDIO10_CS_REQ_GET_CUR: value = s_audio_volume[channel]; break;
    case AUDIO10_CS_REQ_GET_MIN: value = -90 * 256; break;
    case AUDIO10_CS_REQ_GET_MAX: value = 30 * 256; break;
    case AUDIO10_CS_REQ_GET_RES: value = 256; break;
    default: return false;
    }
    static int16_t response;
    response = value;
    return tud_audio_buffer_and_schedule_control_xfer(
        rhport, request, &response, sizeof(response));
}

static void audio_tx_task(void *arg)
{
    (void)arg;
    TickType_t wake = xTaskGetTickCount();
    for (;;) {
        memset(s_audio_packet, 0, sizeof(s_audio_packet));
        if (br_voice_streaming()) {
            (void)br_voice_read_pcm(s_audio_packet, sizeof(s_audio_packet));
        } else {
            int16_t discard[32];
            while (br_voice_read_pcm(discard, sizeof(discard)) != 0) { }
        }
        if (tud_audio_mounted()) {
            (void)tud_audio_write(s_audio_packet, sizeof(s_audio_packet));
        }
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(1));
    }
}

/*
 * 设备描述符。
 *
 * 用 esp_tinyusb 的默认值即可（bDeviceClass=0xEF Misc + IAD）——
 * 那是「CDC 主导」复合设备的标准写法，Windows 认。
 *
 * 曾试过自定义 bDeviceClass=0x00（表示「类信息在接口描述符里」），
 * 但配置描述符里有 IAD，两者冲突 —— Windows 直接报
 * 「未知 USB 设备(设备描述符请求失败)」，整个设备都枚举不出来。
 *
 * ★ 接口顺序：HID(0) -> CDC 控制(1) -> CDC 数据(2)
 *   CMCC 官方固件在 Windows 上也是这样（MI_00=HID, MI_01=CDC）。
 *   HID 在前是因为 Windows 的 HID 驱动要能识别第一个接口，
 *   而 CMCC 证明这个顺序在 Windows 上工作正常。
 *
 *   注意：CDC 的 IAD 在接口 1 而不是 0 —— 这与"CDC 主导"的经典布局不同，
 *   但因为 HID 在接口 0，设备级 class 仍是 0xEF(Misc)+IAD 的组合，
 *   实测 CMCC 这样能枚举，本固件照抄。
 */

/**
 * 字符串描述符表。
 *
 * 索引必须与配置描述符里的 stridx 严格对应：
 *   0 语言 / 1 厂商 / 2 产品 / 3 序列号 / 4 HID 键盘 / 5 HID 消费者 / 6 CDC
 *
 * 注意：esp_tinyusb 有自己的默认表（索引 4 是 CDC），但它只在
 * `tusb_cfg.descriptor.string` 为 NULL 时才用。
 * 这里显式提供自己的表，所以索引以本表为准。
 */
/**
 * 序列号：用芯片 MAC 派生。
 *
 * esp_tinyusb 的默认序列号是编译期常量（Kconfig），所有烧同一份固件的
 * 设备序列号完全相同。Windows 用 VID/PID/序列号生成设备实例 ID，
 * 序列号相同会导致多台设备共用同一个设备节点 —— 后插的那台会复用
 * 先前那台的**缓存状态**（包括缓存的失败状态），枚举永远起不来。
 */
static char s_serial_str[16];

static void build_serial_str(void)
{
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        snprintf(s_serial_str, sizeof(s_serial_str), "%02X%02X%02X%02X%02X%02X",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    } else {
        snprintf(s_serial_str, sizeof(s_serial_str), "ESP32S3");
    }
}

static const char *s_string_descriptor[] = {
    (char[]){ 0x09, 0x04 },   /* 0: English (0x0409) */
    "Espressif",             /* 1: Manufacturer */
    "BLE HID Bridge",        /* 2: Product */
    s_serial_str,            /* 3: Serial（运行时用 MAC 填充） */
    "HID Keyboard",          /* 4: HID 键盘接口 */
    "HID Consumer",          /* 5: HID 消费者接口 */
    "Config Channel",        /* 6: CDC 通信接口 */
    "BLE HID Bridge",        /* 7: 设备名（配置描述符引用） */
    NULL,                    /* 结束标记 */
};

/* ---------------------------------------------------------------- 状态 */
static void usb_tx_task(void *arg);

#define TXQ_LEN 64

static QueueHandle_t s_txq = NULL;


static QueueHandle_t s_cdc_queue = NULL;

/* CDC ACM 接口句柄（由 tinyusb_cdcacm_init 返回）。 */
static tinyusb_cdcacm_itf_t s_cdc_itf = TINYUSB_CDC_ACM_MAX;

/* TX 缓冲：交给 TinyUSB 的 CDC FIFO 之前先在这里攒一下，
 * 保证「入队 + 冲刷」是一个原子操作（不被打断）。
 * 真正的发送由 tinyusb_cdcacm_write_flush 完成。 */
#define CDC_TX_BUF_SIZE 2048
static uint8_t *s_cdc_tx_buf = NULL;

/* CDC 接收缓冲 */
#define CDC_RX_BUF_SIZE 512
#define CDC_LINE_MAX   256

/**
 * ★ 当前按着的键盘键状态 ★
 *
 * USB HID 键盘报告表达的是「**当前**按着哪些键」，没有独立的抬起事件。
 * 所以抬起某个键时必须发一个「不含该键」的报告 ——
 * 抬起时发全零会把同时按着的其它键也松开，
 * 抬起时发「含该键」（之前的实现）则该键永远松不开。
 *
 * 这里维护完整状态，按下时加入、抬起时移除，再据此组报告。
 */
static uint8_t s_kbd_keys[6];      /* 当前按着的普通键（usage） */
static uint8_t s_kbd_count = 0;     /* s_kbd_keys 的有效长度 */
static uint8_t s_kbd_modifier = 0;  /* 当前按着的修饰键位图 */

/*
 * ★ 按住多久就强制抬起 ★
 *
 *   板子替 PC 按键时，只要有一次「抬起」丢了，
 *   那个键就永远按在 PC 上 —— 表现为 PC 自己的键盘突然失灵
 *   （每个键都变成了 Ctrl+某键），或者「按下返回键，文字一下全被删掉」。
 *
 *   不猜是哪条路会漏，直接兜底：
 *   任何按下的键/修饰键超过 BR_HID_HOLD_MAX_MS 就强制抬起。
 *   ★ 板子绝不可能把 PC 的键盘按死 ★
 */
#define BR_HID_HOLD_MAX_MS 3000
/*
 * ★ 例外：语音热键允许长按 ★
 *   「按住说话」要按住几十秒 —— 3 秒的卡键保护会把它当成卡住的键，
 *   每 3 秒清一次 Ctrl+Win，输入法录到一半就断（用户实测 3~4 秒被切断）。
 */
static volatile bool s_hold_exempt = false;

void bridge_usb_set_hold_exempt(bool on)
{
    s_hold_exempt = on;
}

static uint32_t s_kbd_stamp[6];     /* 每个普通键的按下时刻（ms） */
static uint32_t s_mod_stamp[8];     /* 每个修饰键位的按下时刻（ms） */

/*
 * ★ 报告发不出去时先记着，USB ready 了再补发 ★
 *
 *   实测（用户）：板子的日志把 CDC 写队列塞满
 *   （"cdc write_queue wrote=0/9 (fifo full)"），
 *   USB 复合设备就卡住了 → `tud_hid_ready()` 返回假 →
 *   以前 `bridge_usb_release_keyboard()` 开头直接 return，
 *   **抬起被整个丢掉** → 那个键永远按在 PC 上
 *   → 系统自动重复 → 输入框的文字被一下删光。
 *
 *   所以：**逻辑状态（哪些键按着）必须先改，报告能不能发出去是另一回事。**
 */
static bool s_report_pending = false;

/* 消费者控制：当前按着的 usage（0 = 没有） */
static uint16_t s_cons_usage = 0;

typedef struct {
    size_t len;
    char   data[CDC_LINE_MAX];
} cdc_msg_t;

/**
 * CDC 状态诊断。
 *
 * 一次性把 TinyUSB 侧的 CDC 状态全部打出来，用于排查「能收不能发」：
 *   - tud_cdc_ready()        : 设备已挂载且 CDC 类就绪
 *   - tud_cdc_n_connected()  : 主机置了 DTR（Windows 打开串口时）
 *   - tud_cdc_n_get_line_state(): DTR/RTS 原始位
 *   - write_available / write_occupied: TX FIFO 余量
 *
 * 通过协议命令 "CDCSTAT" 触发。
 */
static void cdc_dump_state(void)
{
    char buf[192];
    int n = snprintf(buf, sizeof(buf),
                     "# CDCSTAT ready=%d connected=%d line_state=0x%02X "
                     "w_avail=%u w_occupied=%u rx_avail=%u mounted=%d\n",
                     (int)tud_cdc_ready(),
                     (int)tud_cdc_n_connected(0),
                     (unsigned)tud_cdc_n_get_line_state(0),
                     (unsigned)tud_cdc_n_write_available(0),
                     (unsigned)(CFG_TUD_CDC_TX_BUFSIZE - tud_cdc_n_write_available(0)),
                     (unsigned)tud_cdc_available(),
                     (int)tud_mounted());
    if (n > 0) {
        br_uart_write(buf, (size_t)n);
    }
}

void bridge_usb_dump_cdc_state(void)
{
    cdc_dump_state();
}

/**
 * CDC 收数据回调（esp_tinyusb 的 CDC ACM 封装）。
 *
 * 为什么用 esp_tinyusb 的封装而不是直接写 tud_cdc_rx_cb：
 * esp_tinyusb 的 tinyusb_cdcacm_init() 内部会调用 tinyusb_cdc_init()
 * 完成 TinyUSB CDC 类的初始化。绕过它直接用底层 API，
 * 类驱动的状态机没被正确初始化 —— 症状是固件侧
 * `tud_cdc_write` 返回成功（wrote=130）、但主机侧永远收不到数据。
 */
static void cdcacm_rx_cb(int itf, cdcacm_event_t *event)
{
    (void)itf;
    (void)event;

    static char buf[CDC_RX_BUF_SIZE];
    static size_t pos = 0;

    uint32_t avail = tud_cdc_available();
    uint8_t chunk[128];
    while (avail > 0) {
        uint32_t n = avail > sizeof(chunk) ? sizeof(chunk) : avail;
        uint32_t got = tud_cdc_read(chunk, n);
        if (got == 0) {
            break;
        }
        avail -= got;
        for (uint32_t i = 0; i < got; i++) {
            char c = (char)chunk[i];
            if (c == '\n' || c == '\r') {
                if (pos > 0) {
                    cdc_msg_t msg;
                    msg.len = pos;
                    memcpy(msg.data, buf, pos);
                    msg.data[pos] = '\0';
                    xQueueSend(s_cdc_queue, &msg, 0);
                    pos = 0;
                }
                continue;
            }
            if (pos < sizeof(buf) - 1) {
                buf[pos++] = c;
            }
        }
    }
}

/*
 * tud_cdc_line_coding_cb / tud_cdc_line_state_cb 由 esp_tinyusb 的
 * tinyusb_cdc_acm.c 提供（弱符号默认实现），这里不再重复定义，
 * 否则链接期 multiple definition。
 *
 * 注意：tud_mount_cb / tud_umount_cb 由 esp_tinyusb 组件自己实现，
 * 这里不再重复定义（否则链接期 multiple definition）。
 * 挂载状态改用 tud_cdc_ready() 判断。
 */

/* ---------------------------------------------------------------- HID 回调 */

uint8_t const *tud_hid_descriptor_report_cb(uint8_t instance)
{
    /* 两个 HID 接口共享同一份报告描述符（与 Arduino-ESP32 一致） */
    (void)instance;
    return s_hid_report_desc;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id,
                                hid_report_type_t report_type,
                                uint8_t *buffer, uint16_t reqlen)
{
    (void)instance;
    (void)report_id;
    (void)report_type;
    (void)buffer;
    (void)reqlen;
    return 0;
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id,
                            hid_report_type_t report_type,
                            uint8_t const *buffer, uint16_t bufsize)
{
    (void)instance;
    (void)report_id;
    (void)report_type;
    (void)buffer;
    (void)bufsize;
}

/* ---------------------------------------------------------------- 输出 */

/**
 * HID 报告发送。
 *
 * 现在只有**一个** HID 接口（instance 0），键盘与消费者控制靠 report id 区分
 * —— 与 CMCC（Arduino-ESP32）的做法一致。
 *
 * TinyUSB 的 tud_hid_report(report_id, payload, length)：第一个参数是报告 ID，
 * payload 不包含报告 ID 字节。键盘使用 TinyUSB 的标准 keyboard_report API（8 字节），Consumer payload 为 2 字节。
 */
static bool send_hid_keyboard_checked(uint8_t modifier, const uint8_t keycode[6])
{
    bool ok = tud_hid_keyboard_report(1, modifier, keycode);
    ESP_LOGI(TAG, "HIDTX keyboard id=1 mod=%02X key0=%02X ok=%u",
             modifier, keycode ? keycode[0] : 0, ok);
    /*
     * ★ 发送失败也要能补发 ★
     *   `tud_hid_ready()` 说 ready、真正发的时候却失败（端点被占/主机关了轮询），
     *   以前只记一行日志 —— 那个抬起就永远丢了 → 键一直按在 PC 上。
     */
    return ok;
}
void bridge_usb_send_keyboard(uint8_t modifier, const uint8_t *keys, uint8_t count)
{
    if (!tud_hid_ready()) {
        return;
    }
    if (count > 6) {
        count = 6;
    }

    /* 键盘报告：report id(1) + 修饰键(1) + 6 键码 = 8 字节，正好等于端点包长 */
    uint8_t keycode[6] = {0};
    memcpy(keycode, keys, count);
    send_hid_keyboard_checked(modifier, keycode);

    ESP_LOGD(TAG, "kbd out mod=%02X keys=%02X %02X %02X %02X",
             modifier, keys[0], count > 1 ? keys[1] : 0,
             count > 2 ? keys[2] : 0, count > 3 ? keys[3] : 0);
}

/**
 * 键盘按键按下：把 keys 加进「当前按着」集合并发报告。
 */
void bridge_usb_press_keyboard(uint8_t modifier, const uint8_t *keys, uint8_t count)
{
    if (count > 6) {
        count = 6;
    }

    /* 先把普通键并进状态（已按着的不重复加） */
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    for (uint8_t i = 0; i < count; i++) {
        uint8_t usage = keys[i];
        if (usage >= 0xE0 && usage <= 0xE7) {
            uint8_t bit = (uint8_t)(usage - 0xE0);
            s_kbd_modifier |= (uint8_t)(1 << bit);
            s_mod_stamp[bit] = now;
            continue;
        }
        bool exists = false;
        for (uint8_t k = 0; k < s_kbd_count; k++) {
            if (s_kbd_keys[k] == usage) { exists = true; break; }
        }
        if (!exists && s_kbd_count < 6) {
            s_kbd_stamp[s_kbd_count] = now;   /* 看门狗用 */
            s_kbd_keys[s_kbd_count++] = usage;
        }
    }
    s_kbd_modifier |= modifier;
    for (uint8_t bit = 0; bit < 8; bit++) {
        if (modifier & (1 << bit)) {
            s_mod_stamp[bit] = now;
        }
    }

    uint8_t keycode[6] = {0};
    memcpy(keycode, s_kbd_keys, s_kbd_count);
    /*
     * ★ 状态已经记下了，报告发不出去就等一会儿补发 ★
     *   （之前是「USB 不 ready 就整个 return」，抬起事件就这么丢了）
     */
    if (!tud_hid_ready()) {
        s_report_pending = true;
        ESP_LOGW(TAG, "HID 暂不可用，按下先记着，等会儿补发");
        return;
    }
    /* ★ 发失败也要补发 ★（ready 说了算，真发的时候仍可能失败） */
    s_report_pending = !send_hid_keyboard_checked(s_kbd_modifier, keycode);
    ESP_LOGI(TAG, "kbd press mod=%02X count=%u", s_kbd_modifier, s_kbd_count);
}

/**
 * 键盘按键抬起：从「当前按着」集合里移除 keys 并发报告。
 *
 * ★ 只移除被抬起的键，其它还按着的键必须保留 ——
 *   发全零报告会把组合键的其它部分也松开。
 */
void bridge_usb_release_keyboard(uint8_t modifier, const uint8_t *keys, uint8_t count)
{
    /*
     * ★ 这里**不能**因为 USB 暂不可用就整个 return ★
     *   抬起报告发不出去，那个键就永远按在 PC 上 ——
     *   系统自动重复 → 输入框里的文字被一下删光（用户实测）。
     *   所以：先把「这个键不再按着」落实到状态里，
     *   报告等 USB ready 了再补发。
     */
    for (uint8_t i = 0; i < count; i++) {
        uint8_t usage = keys[i];
        if (usage >= 0xE0 && usage <= 0xE7) {
            uint8_t bit = (uint8_t)(usage - 0xE0);
            s_kbd_modifier &= (uint8_t)~(1 << bit);
            s_mod_stamp[bit] = 0;
            continue;
        }
        for (uint8_t k = 0; k < s_kbd_count; k++) {
            if (s_kbd_keys[k] == usage) {
                /* 前移覆盖，保持顺序（时刻也要一起挪） */
                for (uint8_t j = k; j < s_kbd_count - 1; j++) {
                    s_kbd_keys[j] = s_kbd_keys[j + 1];
                    s_kbd_stamp[j] = s_kbd_stamp[j + 1];
                }
                s_kbd_stamp[s_kbd_count - 1] = 0;
                s_kbd_count--;
                break;
            }
        }
    }
    s_kbd_modifier &= (uint8_t)~modifier;
    for (uint8_t bit = 0; bit < 8; bit++) {
        if (modifier & (1 << bit)) {
            s_mod_stamp[bit] = 0;
        }
    }

    uint8_t keycode[6] = {0};
    memcpy(keycode, s_kbd_keys, s_kbd_count);
    if (!tud_hid_ready()) {
        s_report_pending = true;
        ESP_LOGW(TAG, "HID 暂不可用，抬起先记着，等会儿补发");
        return;
    }
    /* ★ 发失败也要补发 ★（ready 说了算，真发的时候仍可能失败） */
    s_report_pending = !send_hid_keyboard_checked(s_kbd_modifier, keycode);
    ESP_LOGI(TAG, "kbd release mod=%02X count=%u", s_kbd_modifier, s_kbd_count);
}

/**
 * ★ 按键释放看门狗 ★
 *
 *   把按住超时的键/修饰键强制抬起并发一个干净报告。
 *   主循环每 200 ms 调一次 —— 板子绝不可能把 PC 的键盘按死。
 */
void bridge_usb_hid_watchdog(void)
{
    if (!tud_hid_ready()) {
        return;
    }
    /* ★ 之前发不出去的报告，USB 恢复了就补发 ★ */
    if (s_report_pending) {
        uint8_t keycode[6] = {0};
        memcpy(keycode, s_kbd_keys, s_kbd_count);
        if (send_hid_keyboard_checked(s_kbd_modifier, keycode)) {
            s_report_pending = false;
            ESP_LOGW(TAG, "补发之前发不出去的 HID 报告");
        }
    }
    if (s_hold_exempt) {
        return;      /* 语音热键按着的时候不判「按住超时」 */
    }
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    bool changed = false;

    for (int b = 0; b < 8; b++) {
        if (s_mod_stamp[b] && (now - s_mod_stamp[b]) > BR_HID_HOLD_MAX_MS) {
            s_mod_stamp[b] = 0;
            s_kbd_modifier &= (uint8_t)~(1 << b);
            changed = true;
        }
    }

    int w = 0;
    for (int k = 0; k < s_kbd_count; k++) {
        if (s_kbd_stamp[k] && (now - s_kbd_stamp[k]) > BR_HID_HOLD_MAX_MS) {
            changed = true;      /* 这个键按住太久了，丢掉 */
            continue;
        }
        s_kbd_keys[w] = s_kbd_keys[k];
        s_kbd_stamp[w] = s_kbd_stamp[k];
        w++;
    }
    if (w != s_kbd_count) {
        s_kbd_count = w;
        changed = true;
    }
    if (!changed) {
        return;
    }

    uint8_t keycode[6] = {0};
    memcpy(keycode, s_kbd_keys, s_kbd_count);
    s_report_pending = !send_hid_keyboard_checked(s_kbd_modifier, keycode);
    ESP_LOGW(TAG, "HID 看门狗：强制抬起按住超时的键");
}

void bridge_usb_send_consumer(uint16_t usage)
{
    if (!tud_hid_ready()) {
        return;
    }
    /* 消费者报告：report id(1) + 16 位 usage(2) = 3 字节 */
    uint8_t buf[2];
    buf[0] = (uint8_t)(usage & 0xFF);
    buf[1] = (uint8_t)((usage >> 8) & 0xFF);
    bool ok = tud_hid_report(2, buf, sizeof(buf));
    ESP_LOGI(TAG, "HIDTX consumer id=2 len=%u ok=%u", (unsigned)sizeof(buf), ok);

    ESP_LOGD(TAG, "consumer out usage=%04X", usage);
}

/**
 * 消费者键按下。
 */
void bridge_usb_press_consumer(uint16_t usage)
{
    if (!tud_hid_ready()) {
        return;
    }
    s_cons_usage = usage;

    uint8_t buf[2];
    buf[0] = (uint8_t)(usage & 0xFF);
    buf[1] = (uint8_t)((usage >> 8) & 0xFF);
    bool ok = tud_hid_report(2, buf, sizeof(buf));
    ESP_LOGI(TAG, "HIDTX consumer id=2 len=%u ok=%u", (unsigned)sizeof(buf), ok);

    ESP_LOGD(TAG, "consumer press usage=%04X", usage);
}

/**
 * 消费者键抬起。
 *
 * ★ 必须发 usage=0 的报告 —— 消费者控制报告没有独立的抬起事件，
 *   全零表示「当前没有按任何消费者键」。
 */
void bridge_usb_release_consumer(uint16_t usage)
{
    (void)usage;
    if (!tud_hid_ready()) {
        return;
    }
    s_cons_usage = 0;

    uint8_t buf[2] = {0, 0};
    bool ok = tud_hid_report(2, buf, sizeof(buf));
    ESP_LOGI(TAG, "HIDTX consumer id=2 len=%u ok=%u", (unsigned)sizeof(buf), ok);

    ESP_LOGD(TAG, "consumer release");
}

/* ---------------------------------------------------------------- 初始化 */

esp_err_t bridge_usb_init(void)
{
    s_cdc_queue = xQueueCreate(16, sizeof(cdc_msg_t));
    if (!s_cdc_tx_buf) {
        s_cdc_tx_buf = heap_caps_malloc(CDC_TX_BUF_SIZE, MALLOC_CAP_DMA);
    }
    if (!s_cdc_queue) {
        ESP_LOGE(TAG, "cdc queue alloc failed");
        return ESP_ERR_NO_MEM;
    }
    if (!s_cdc_tx_buf) {
        ESP_LOGE(TAG, "cdc tx buf alloc failed");
        return ESP_ERR_NO_MEM;
    }

    /* 发送队列（保证 bridge_usb_write 不阻塞 BLE 回调） */
    if (!s_txq) {
        s_txq = xQueueCreate(TXQ_LEN, sizeof(char *));
        if (!s_txq) {
            ESP_LOGE(TAG, "tx queue alloc failed");
            return ESP_ERR_NO_MEM;
        }
        if (xTaskCreate(usb_tx_task, "usb_tx", 4096, NULL, 4, NULL) != pdPASS) {
            ESP_LOGE(TAG, "usb tx task create failed");
            return ESP_ERR_NO_MEM;
        }
    }

    tinyusb_config_t tusb_cfg = TINYUSB_DEFAULT_CONFIG();
    /*
     * 自定义设备描述符，PID 用 0x0002。
     *
     * 0x0002 是 Espressif 在 Arduino-ESP32 里用的通用 PID（cores/esp32/USB.cpp：
     * `#define USB_PID 0x0002`）。CMCC 的固件就是用它，Windows 上 CDC 串口正常。
     * 而 0x4009 是 Arduino 的 HID 专用 PID，Windows 对它的 CDC 接口匹配
     * 没有那么成熟的规则 —— 这可能就是 CDC 枚举失败（ProblemCode 10）的原因。
     *
     * 设备级 class 必须保持 TUSB_CLASS_MISC / MISC_SUBCLASS_COMMON /
     * MISC_PROTOCOL_IAD（esp_tinyusb 文档明确要求），与 IAD 配套。
     */
    static const tusb_desc_device_t s_device_desc = {
        .bLength            = sizeof(tusb_desc_device_t),
        .bDescriptorType    = TUSB_DESC_DEVICE,
        .bcdUSB             = 0x0200,
        .bDeviceClass       = 0xEF,   /* TUSB_CLASS_MISC */
        .bDeviceSubClass    = 0x02,   /* MISC_SUBCLASS_COMMON */
        .bDeviceProtocol    = 0x01,   /* MISC_PROTOCOL_IAD */
        .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
        .idVendor           = 0x303A, /* Espressif */
        .idProduct          = 0x0002,
        .bcdDevice          = 0x0100,
        .iManufacturer      = 0x01,
        .iProduct           = 0x02,
        .iSerialNumber      = 0x03,
        .bNumConfigurations = 0x01,
    };
    tusb_cfg.descriptor.device = &s_device_desc;

    /* 字符串表用我们自己的（索引与配置描述符的 stridx 对应）。
     * string_count 不含末尾的 NULL 结束标记。 */
    build_serial_str();
    tusb_cfg.descriptor.string = s_string_descriptor;
    tusb_cfg.descriptor.string_count =
        (int)(sizeof(s_string_descriptor) / sizeof(s_string_descriptor[0])) - 1;

    tusb_cfg.descriptor.full_speed_config = s_config_descriptor;
#if (TUD_OPT_HIGH_SPEED)
    tusb_cfg.descriptor.high_speed_config = s_config_descriptor;
#endif

    esp_err_t err = tinyusb_driver_install(&tusb_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb install failed: %s", esp_err_to_name(err));
        return err;
    }

    /*
     * 注册 CDC ACM 接口。
     *
     * 这一步**不能省**：esp_tinyusb 的 tinyusb_cdcacm_init() 内部会调用
     * tinyusb_cdc_init() 完成 TinyUSB CDC 类的初始化。
     * 只用底层 tud_cdc_write / tud_cdc_read 而不做这个初始化，
     * 类驱动的状态机没就绪 —— 症状是固件侧 tud_cdc_write() 返回成功
     * （诊断显示 wrote=130），但主机侧永远收不到数据。
     */
    const tinyusb_config_cdcacm_t cdc_cfg = {
        .cdc_port = 0,
        .callback_rx = cdcacm_rx_cb,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL,
    };
    err = tinyusb_cdcacm_init(&cdc_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cdcacm init failed: %s", esp_err_to_name(err));
        return err;
    }
    s_cdc_itf = TINYUSB_CDC_ACM_0;   /* esp_tinyusb 单接口，句柄为 0 */

    ESP_LOGI(TAG, "USB composite device (HID kb + HID consumer + CDC) ready");
    xTaskCreate(audio_tx_task, "usb_audio_tx", 3072, NULL, 5, NULL);
    return ESP_OK;
}

bool bridge_usb_read_line(char *buf, size_t cap, uint32_t timeout_ms)
{
    if (!s_cdc_queue) {
        return false;
    }
    cdc_msg_t msg;
    if (xQueueReceive(s_cdc_queue, &msg, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        return false;
    }
    size_t n = msg.len < cap - 1 ? msg.len : cap - 1;
    memcpy(buf, msg.data, n);
    buf[n] = '\0';
    return true;
}

bool bridge_usb_cdc_connected(void)
{
    return tud_cdc_ready();
}

bool bridge_usb_hid_ready(void)
{
    return tud_hid_ready();
}

/**
 * 把 ESP-IDF 日志重定向到 USB CDC。
 *
 * 为什么要做：ESP-IDF 的日志默认走 stdout（UART0），
 * 而本项目的 UART0 是**协议通道**，日志混进去会破坏协议解析。
 * 所以把日志重定向到 CDC，UART0 专供协议。
 *
 * 注意：CDC 还没连上时日志会写进 TinyUSB 的 TX 缓冲（满了就丢），
 * 这不影响功能——此时可以用 USB-Serial-JTAG 看早期日志。
 */
/**
 * 日志输出钩子。
 *
 * 日志走 UART0（stdout 原生行为），协议应答走 CDC + UART。
 * 客户端解析时会忽略不是协议格式的行（日志行），所以两者可以共存。
 *
 * 之前把日志也重定向到 CDC 有两个问题：
 *   1. CDC 没被主机打开时日志全部丢失，调试时什么都看不到
 *   2. 日志量比协议大得多，会把 CDC 缓冲冲爆
 */
static int bridge_usb_log_vprintf(const char *fmt, va_list args)
{
    (void)fmt;
    (void)args;
    return 0;   /* 不接管，日志走 stdout(UART0) */
}

void bridge_usb_install_log_hook(void)
{
    /*
     * ★ 故意**不接管** ESP_LOG ★
     *
     * 之前的实现把 esp_log_set_vprintf() 指向一个什么都不做的函数，
     * 结果所有 ESP_LOG 输出被吞掉 —— 现场出问题连一行日志都看不到。
     * 现在保持默认行为：日志走 UART0（CH343），永远能抓得到。
     */
    ESP_LOGI(TAG, "log output stays on UART0 (CH343)");
}

/**
 * CDC 写数据函数。
 *
 * CDC 是协议应答主通道，保证**实时性**：收到主机指令后立刻应答。
 *
 * 实现要点（踩坑记录）：
 *
 * 1. 必须用 esp_tinyusb 的 CDC ACM 封装（tinyusb_cdcacm_write_queue +
 *    tinyusb_cdcacm_write_flush），裸用 tud_cdc_write 数据进不了端点。
 *
 * 2. write_flush 的 timeout **不能传 0**。timeout=0 是非阻塞模式：
 *    TinyUSB 调 tud_cdc_n_write_flush() 后立刻返回，如果此时 endpoint 还忙
 *    （上一包还没发完），数据就留在 FIFO 里等下一次 flush ——
 *    而应答是"发完就结束"的一次性事件，不会再有下一次 flush，
 *    结果就是主机永远收不到应答（现象：固件日志显示 wrote=OK，主机侧 TIMEOUT）。
 *    传 timeout_ticks（>=1）会让 esp_tinyusb 循环 flush + vTaskDelay(1)，
 *    直到 FIFO 排空或超时。
 *
 * 3. ESP_ERR_NOT_FINISHED(268) 不是错误，只表示"还没发完"。
 *    之前把它当失败处理，直接把应答丢给 UART，导致 CDC 通道实际从未发送过数据。
 *
 * 4. TinyUSB 的注释写明 "data is dropped if terminal is not connected"：
 *    主机没置 DTR 时数据会被丢弃。所以固件侧必须保留 UART 冗余通道。
 */
/*
 * ★★★ 发送队列 ★★★
 *
 * bridge_usb_write() 只做入队，**绝不阻塞**。
 *
 * 原因：BLE 的 notify 回调和扫描回调里都会调 bridge_usb_write()。
 * 而 write_flush(带 timeout) 最长要等 200ms ——
 * 在 BLE 回调里阻塞这么久会把协议栈卡住：
 *   - ATT 确认延迟，遥控器判定拥塞主动断链（CMCC 实测）
 *   - 极端情况下 USB 栈也会被拖垮
 *
 * 真正的发送动作全部搬到 usb_tx_task 里做。
 */
void bridge_usb_write(const char *data, size_t len)
{
    if (!data || len == 0) {
        return;
    }

    if (!s_txq) {
        /* 队列还没建起来（初始化早期）—— 走 UART0 兜底 */
        br_uart_write(data, len);
        return;
    }

    char *buf = (char *)malloc(len + 1);
    if (!buf) {
        br_uart_write(data, len);
        return;
    }
    memcpy(buf, data, len);
    buf[len] = '\0';

    if (xQueueSend(s_txq, &buf, 0) != pdTRUE) {
        /* 队列满了：丢这一条，并把计数报出来 */
        free(buf);
        br_uart_write(data, len);
        return;
    }
}

/** 发送任务：真正把数据推给主机 */
static void usb_tx_task(void *arg)
{
    (void)arg;
    char *buf = NULL;

    for (;;) {
        if (xQueueReceive(s_txq, &buf, pdMS_TO_TICKS(100)) != pdTRUE) {
            continue;
        }

        /* UART 冗余：CDC 没连上时也能看到协议 */
        br_uart_write(buf, strlen(buf));

        if (s_cdc_tx_buf && s_cdc_itf != TINYUSB_CDC_ACM_MAX) {
            size_t wrote = tinyusb_cdcacm_write_queue(s_cdc_itf,
                                                      (const uint8_t *)buf,
                                                      strlen(buf));
            if (wrote == strlen(buf)) {
                /* 带超时的阻塞冲刷：给 TinyUSB 时间把 FIFO 推给主机。
                 * ★ 5 ticks @ 100Hz = 50ms ★
                 *   以前是 20 ticks = **200ms** —— 一条日志能把发送任务卡住 200ms，
                 *   协议应答跟着一起延迟，客户端的请求就超时了。
                 *   而且「fifo 满」的告警本身也要进这条队列 → 自我循环。
                 */
                esp_err_t err = tinyusb_cdcacm_write_flush(s_cdc_itf, 5);
                if (err != ESP_OK && err != ESP_ERR_NOT_FINISHED) {
                    ESP_LOGD(TAG, "cdc write_flush failed (%d)", err);
                }
            } else {
                /* ★ 别把「队列满了」当日志报出去 ★
                 *   这条告警自己也要进同一条队列 → 越堵越堵。
                 *   静默丢掉就好，丢日志不影响协议。 */
                static uint32_t s_dropped = 0;
                s_dropped++;
                if ((s_dropped & 0x3F) == 0) {
                    ESP_LOGD(TAG, "cdc 队列满，丢掉 %u 条日志", s_dropped);
                }
            }
        }

        free(buf);
        buf = NULL;
    }
}


/**
 * CDC 发送缓冲补发。由主循环周期调用。
 *
 * 作用：把上一轮 write_queue 没发完的残留数据继续推出去。
 * 正常情况下 write_flush(带 timeout) 已经把数据发完了，这里是兜底。
 */
void bridge_usb_flush_cdc(void)
{
    if (!s_cdc_tx_buf || s_cdc_itf == TINYUSB_CDC_ACM_MAX) {
        return;
    }
    if (!tud_cdc_ready()) {
        return;     /* 主机未连接，flush 无意义 */
    }

    /* 非阻塞冲刷：把 FIFO 里的残留推出去。
     * 不检查返回值：ESP_ERR_NOT_FINISHED 表示"还没完"，下一轮继续。 */
    tinyusb_cdcacm_write_flush(s_cdc_itf, 0);
}
