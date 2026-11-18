/*
 * bridge_uart - UART0 协议通道（备份通道）
 * ---------------------------------------------------------------------------
 * 为什么要两条通道：
 *   ESP32-S3 的原生 USB 被 TinyUSB 占用后，USB-Serial-JTAG 就不可用了。
 *   复合设备（HID + CDC）在部分 Windows 版本上枚举有兼容性问题，
 *   这时 CDC 接口可能起不来（ProblemCode 10）。
 *
 *   UART0 接板上的 CH343 芯片（COM 口），是独立于 USB 的物理通道：
 *     - 永远可用，不受 TinyUSB 描述符影响
 *     - 也是固件烧录通道（esptool 走 UART）
 *     - 缺点：占用一个物理串口，且波特率受限
 *
 * 所以：CDC 与 UART 同时提供同样的协议，客户端任选其一。
 *
 * 协议与 CDC 完全一致（见 bridge_proto.h）。
 *
 * 注意：ESP-IDF 的日志默认也走 UART0（stdout），会与协议混在一起。
 * 固件启动时把日志重定向到 USB CDC（见 bridge_usb.c 的 br_usb_install_log_hook），
 * 让 UART0 专供协议。若 CDC 不可用，日志会丢在 CDC 缓冲里，
 * 这时用 USB-Serial-JTAG 或临时改回 UART 日志即可。
 */
#include "br_uart.h"
#include "br_proto.h"

#include <string.h>
#include <stdio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <driver/uart.h>
#include <esp_log.h>

static const char *TAG = "br_uart";

#define UART_NUM UART_NUM_0
#define UART_BAUD 115200
#define RX_BUF_SIZE 1024

static QueueHandle_t s_rx_queue = NULL;
static bool s_ready = false;

static void uart_event_task(void *arg)
{
    (void)arg;
    uint8_t buf[128];
    static char line[256];
    static size_t pos = 0;

    while (1) {
        int len = uart_read_bytes(UART_NUM, buf, sizeof(buf), pdMS_TO_TICKS(100));
        if (len <= 0) {
            continue;
        }
        for (int i = 0; i < len; i++) {
            char c = (char)buf[i];
            if (c == '\n' || c == '\r') {
                if (pos > 0) {
                    line[pos] = '\0';
                    br_uart_push_line(line);
                    pos = 0;
                }
                continue;
            }
            if (pos < sizeof(line) - 1) {
                line[pos++] = c;
            }
        }
    }
}

void br_uart_init(void)
{
    const uart_config_t cfg = {
        .baud_rate = UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    esp_err_t err = uart_driver_install(UART_NUM, RX_BUF_SIZE * 2, RX_BUF_SIZE * 2,
                                        0, NULL, 0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "uart_driver_install failed: %s", esp_err_to_name(err));
        return;
    }
    err = uart_param_config(UART_NUM, &cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart_param_config failed: %s", esp_err_to_name(err));
        return;
    }
    err = uart_set_pin(UART_NUM, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                        UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "uart_set_pin failed: %s", esp_err_to_name(err));
    }

    s_ready = true;

    /* 接收任务 */
    /*
     * ★ 栈不能太小 ★
     *   3072 会炸：往 UART 发一条 BONDED 命令，固件打印完
     *   「bonded[0] 7E:A2:99:61:45:FE」立刻
     *     ***ERROR*** A stack overflow in task uart_rx has been detected
     *   然后整个板子重启。
     *   原因：handle_command 帧里 br_parsed_t(约 650B) + br_kv_t(512B)
     *   再加上 ESP_LOG 的格式化缓冲，3072 不够。
     */
    if (xTaskCreate(uart_event_task, "uart_rx", 6144, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "uart task create failed");
        s_ready = false;
        return;
    }
    ESP_LOGI(TAG, "UART protocol channel ready @ %d baud", UART_BAUD);
}

bool br_uart_connected(void)
{
    return s_ready;
}

static br_uart_line_fn s_line_handler;

void br_uart_set_line_handler(br_uart_line_fn fn)
{
    s_line_handler = fn;
}

/** 供其它模块把一行文本交给协议层处理（CDC 与 UART 共用协议层） */
void br_uart_push_line(const char *line)
{
    if (s_line_handler) {
        s_line_handler(line);
    }
}

void br_uart_write(const char *text, size_t len)
{
    if (!s_ready) {
        return;
    }
    int written = uart_write_bytes(UART_NUM, text, len);
    if (written != (int)len) {
        ESP_LOGW(TAG, "uart write short: %d/%u", written, (unsigned)len);
    }
}
