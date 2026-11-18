/*
 * app_main - 新固件入口
 * ---------------------------------------------------------------------------
 * 当前阶段（P0 探针）：
 *   - USB CDC 收命令
 *   - BLE 扫描 / 连接 / GATT 全量转储
 *   - 目的是回答一个 go/no-go 问题：
 *     **Chromecast 遥控器的 ATVV 服务（AB5E0001）在 ESP32 侧能不能看见？**
 *
 * 协议：SP-v2（见 br_proto.h）
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include <esp_log.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>

#include "br_types.h"
#include "br_proto.h"
#include "br_ble_core.h"
#include "br_hid.h"
#include "br_mapping.h"
#include "br_usb.h"
#include "br_led.h"
#include "br_uart.h"
#include "br_voice.h"
#include "br_gesture.h"
#include "br_logbuf.h"

static const char *TAG = "app";

#define FW_VERSION "1.0.0"

/* ------------------------------------------------------------------ 输出 */

static void usb_write(const char *data, size_t len)
{
    bridge_usb_write(data, len);
}

/* ------------------------------------------------------------ 命令处理 */

/*
 * 地址解析统一用 br_ble_addr_parse（br_ble_core.c:addr_to_str 旁边）。
 * 那里记录了为什么必须 out[5-i]=v[i]（NimBLE 地址是小端）——
 * 正着填会把目标整体反成另一台设备，配对必然 status=13。
 * 不要再在本文件里另写一份。
 */

static void cmd_info(void)
{
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add(&kv, "fw", FW_VERSION);
    br_kv_add_int(&kv, "proto", 2);
    br_kv_add_int(&kv, "peers", br_ble_peer_count());
    br_kv_add_int(&kv, "conn", br_ble_connected_count());
    br_kv_add_int(&kv, "cands", br_ble_candidate_count());
    br_proto_send_ok("INFO", &kv);
}

/**
 * 解析并执行一条命令。
 */
static void handle_command(const char *line)
{
    br_parsed_t p;
    int argc = br_proto_parse((char *)line, &p);
    if (argc == 0) {
        return;
    }

    const char *verb = p.argv[0];

    if (strcmp(verb, "INFO") == 0) {
        cmd_info();

    } else if (strcmp(verb, "SCAN") == 0) {
        long mode = 1;
        if (!br_proto_arg_int(&p, 1, &mode)) {
            mode = 1;
        }
        if (mode == 0) {
            br_ble_scan_stop();
            br_proto_send_ok("SCAN", NULL);
        } else {
            /* PC 端点「扫描」= 开始一个扫描会话 */
            br_ble_scan_session_start(mode >= 2);
            br_proto_send_ok("SCAN", NULL);
        }

    } else if (strcmp(verb, "AUTO") == 0) {
        long on = 0;
        br_proto_arg_int(&p, 1, &on);
        br_ble_set_auto(on != 0);
        br_ble_set_auto_dump(on != 0);
        br_proto_send_ok("AUTO", NULL);

    } else if (strcmp(verb, "TARGET") == 0) {
        br_ble_set_target_name(argc > 1 ? p.argv[1] : NULL);
        br_proto_send_ok("TARGET", NULL);

    } else if (strcmp(verb, "CONNECT") == 0) {
        /* CONNECT AA:BB:CC:DD:EE:FF [type] */
        if (argc < 2) {
            br_proto_send_err("CONNECT", "args", NULL);
            return;
        }
        uint8_t addr[6];
        if (!br_ble_addr_parse(p.argv[1], addr)) {
            br_proto_send_err("CONNECT", "addr", NULL);
            return;
        }
        long type = 0;
        br_proto_arg_int(&p, 2, &type);
        br_ble_connect_addr(addr, (uint8_t)type, "manual");
        br_proto_send_ok("CONNECT", NULL);

    } else if (strcmp(verb, "REMOTES") == 0) {
        br_ble_list_remotes();

    } else if (strcmp(verb, "LEARN") == 0) {
        long idx = 0;
        long on = 1;
        br_proto_arg_int(&p, 1, &idx);
        if (!br_proto_arg_int(&p, 2, &on)) {
            on = 1;
        }
        br_ble_set_learn((int)idx, on != 0);
        br_proto_send_ok("LEARN", NULL);

    } else if (strcmp(verb, "PAIR") == 0) {
        /* PAIR <addr> <type> <name> */
        if (argc < 2) {
            br_proto_send_err("PAIR", "args", NULL);
            return;
        }
        uint8_t addr[6];
        if (!br_ble_addr_parse(p.argv[1], addr)) {
            br_proto_send_err("PAIR", "addr", NULL);
            return;
        }
        long type = 0;
        br_proto_arg_int(&p, 2, &type);
        /* 名字可能带空格（客户端会转义成 %20），这里反转义 */
        char name[BR_MAX_NAME_LEN] = { 0 };
        if (argc > 3) {
            br_proto_unescape(name, sizeof(name), p.argv[3]);
        }
        if (!br_ble_pair(addr, (uint8_t)type, name)) {
            br_proto_send_err("PAIR", "busy", NULL);
            return;
        }
        br_proto_send_ok("PAIR", NULL);

    } else if (strcmp(verb, "UNPAIR") == 0) {
        long idx = 0;
        if (!br_proto_arg_int(&p, 1, &idx)) {
            br_proto_send_err("UNPAIR", "args", NULL);
            return;
        }
        if (!br_ble_unpair((int)idx)) {
            br_proto_send_err("UNPAIR", "failed", NULL);
            return;
        }
        br_proto_send_ok("UNPAIR", NULL);

    } else if (strcmp(verb, "BONDED") == 0) {
        br_ble_list_bonded();
        br_proto_send_ok("BONDED", NULL);

    } else if (strcmp(verb, "BCONNECT") == 0) {
        long idx = 0;
        br_proto_arg_int(&p, 1, &idx);
        if (!br_ble_connect_bonded((int)idx)) {
            br_proto_send_err("BCONNECT", "no-bond", NULL);
            return;
        }
        br_proto_send_ok("BCONNECT", NULL);

    } else if (strcmp(verb, "DUMP") == 0) {
        long idx = 0;
        br_proto_arg_int(&p, 1, &idx);
        br_ble_gatt_dump((int)idx);
        br_proto_send_ok("DUMP", NULL);

    } else if (strcmp(verb, "HIDREPORT") == 0) {
        long idx = 0;
        br_proto_arg_int(&p, 1, &idx);
        if (!br_hid_dump_reports((int)idx)) {
            br_proto_send_err("HIDREPORT", "peer", NULL);
            return;
        }
        br_proto_send_ok("HIDREPORT", NULL);

    } else if (strcmp(verb, "MAPBEGIN") == 0) {
        if (argc < 2 || !br_mapping_begin(p.argv[1])) {
            br_proto_send_err("MAPBEGIN", "addr", NULL);
            return;
        }
        br_proto_send_ok("MAPBEGIN", NULL);

    } else if (strcmp(verb, "MAPSET") == 0) {
        long key, trig, out, mods, count;
        uint8_t keys[BR_MAX_KEYS];
        if (argc < 6 || !br_proto_arg_int(&p, 1, &key) ||
            !br_proto_arg_int(&p, 2, &trig) ||
            !br_proto_arg_int(&p, 3, &out) ||
            !br_proto_arg_int(&p, 4, &mods) ||
            !br_proto_arg_int(&p, 5, &count) || count < 0 ||
            count > BR_MAX_KEYS || argc != 6 + count) {
            br_proto_send_err("MAPSET", "args", NULL);
            return;
        }
        for (int i = 0; i < count; i++) {
            long value;
            if (!br_proto_arg_int(&p, 6 + i, &value) || value < 0 || value > 255) {
                br_proto_send_err("MAPSET", "key", NULL);
                return;
            }
            keys[i] = (uint8_t)value;
        }
        if (key < 0 || key > 0xFFFF || trig < 0 || trig >= BR_TRIG_COUNT ||
            out < 0 || out > BR_OUT_CONSUMER || mods < 0 || mods > 255 ||
            !br_mapping_set((uint32_t)key, (uint8_t)trig, (uint8_t)out,
                            (uint8_t)mods, keys, (uint8_t)count)) {
            br_proto_send_err("MAPSET", "invalid", NULL);
            return;
        }
        br_proto_send_ok("MAPSET", NULL);

    } else if (strcmp(verb, "MAPCOMMIT") == 0) {
        if (!br_mapping_commit()) {
            br_proto_send_err("MAPCOMMIT", "nvs", NULL);
            return;
        }
        br_proto_send_ok("MAPCOMMIT", NULL);

    } else if (strcmp(verb, "MAPGET") == 0) {
        if (argc < 2 || !br_mapping_dump(p.argv[1])) {
            br_proto_send_err("MAPGET", "addr", NULL);
            return;
        }

    } else if (strcmp(verb, "VOICEKEY") == 0) {
        /*
         * 不带参数 = 读回当前语音快捷键；
         * 带参数 = 写（老行为不变）。
         */
        if (argc < 2) {
            uint8_t mods = 0, count = 0;
            uint8_t keys[6] = {0};
            br_voice_get_hotkey(&mods, keys, &count);
            br_kv_t kv; br_kv_init(&kv);
            br_kv_add_int(&kv, "mods", mods);
            br_kv_add_int(&kv, "count", count);
            br_kv_add_hex(&kv, "keys", keys, count);
            br_proto_send_ok("VOICEKEY", &kv);
            return;
        }
        long mods, count;
        uint8_t keys[6];
        if (argc < 3 || !br_proto_arg_int(&p, 1, &mods) ||
            !br_proto_arg_int(&p, 2, &count) || mods < 0 || mods > 255 ||
            count < 0 || count > 6 || argc != 3 + count) {
            br_proto_send_err("VOICEKEY", "args", NULL);
            return;
        }
        for (int i = 0; i < count; i++) {
            long value;
            if (!br_proto_arg_int(&p, 3 + i, &value) || value < 1 || value > 255) {
                br_proto_send_err("VOICEKEY", "key", NULL);
                return;
            }
            keys[i] = (uint8_t)value;
        }
        if (!br_voice_set_hotkey((uint8_t)mods, keys, (uint8_t)count)) {
            br_proto_send_err("VOICEKEY", "nvs-or-invalid", NULL);
            return;
        }
        br_proto_send_ok("VOICEKEY", NULL);

    } else if (strcmp(verb, "VOICE") == 0) {
        /*
         * 语音通道开关。
         * ★ 以前 PC 端发这个命令，固件里根本没有 → 回 ERR VOICE unknown，
         *   界面上的开关是假的。现在真的接上：
         *   关掉以后板子不再替 PC 按语音快捷键。
         */
        long v = 1;
        if (argc >= 2 && !br_proto_arg_int(&p, 1, &v)) {
            br_proto_send_err("VOICE", "args", NULL);
            return;
        }
        bool on = (v != 0);
        br_voice_set_enabled(on);
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "on", on ? 1 : 0);
        br_proto_send_ok("VOICE", &kv);

    } else if (strcmp(verb, "LOGREAD") == 0) {
        /* LOGREAD [n] —— 回传板端滚动日志缓冲 */
        long want = BR_LOGBUF_LINES;
        if (argc > 1 && !br_proto_arg_int(&p, 1, &want)) {
            want = BR_LOGBUF_LINES;
        }
        if (want <= 0 || want > BR_LOGBUF_LINES) {
            want = BR_LOGBUF_LINES;
        }
        int total = br_logbuf_count();
        int first = total - (int)want;
        if (first < 0) {
            first = 0;
        }
        for (int i = first; i < total; i++) {
            char level[BR_LOGBUF_LEVEL_MAX];
            char tag[BR_LOGBUF_TAG_MAX];
            char msg[BR_LOGBUF_MSG_MAX];
            if (br_logbuf_get(i, level, sizeof(level), tag, sizeof(tag),
                               msg, sizeof(msg)) < 0) {
                continue;
            }
            br_kv_t kv; br_kv_init(&kv);
            br_kv_add(&kv, "level", level);
            br_kv_add(&kv, "tag", tag);
            br_kv_add(&kv, "msg", msg);
            br_proto_send_dt("LOG", &kv);
        }
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "count", total);
        br_kv_add_int(&kv, "returned", total - first);
        br_proto_send_ok("LOGREAD", &kv);

    } else if (strcmp(verb, "LOGLIVE") == 0) {
        long on = 0;
        br_proto_arg_int(&p, 1, &on);
        br_logbuf_set_live(on != 0);
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "live", on != 0 ? 1 : 0);
        br_proto_send_ok("LOGLIVE", &kv);

    } else if (strcmp(verb, "PAIRWINDOW") == 0) {
        /*
         * PAIRWINDOW <秒>：开一个配对窗口。
         * 窗口内扫到没登记过的遥控器就自动收下，PC 端只要等着收 EV PAIR。
         */
        long sec = 120;
        if (argc > 1 && !br_proto_arg_int(&p, 1, &sec)) {
            sec = 120;
        }
        if (sec < 0) {
            sec = 0;
        }
        br_ble_pair_window_start((int)sec);
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "remaining", br_ble_pair_window_remaining());
        br_kv_add_int(&kv, "ok", 1);
        br_proto_send_ok("PAIRWINDOW", &kv);

    } else if (strcmp(verb, "GETPAIRWINDOW") == 0) {
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "remaining", br_ble_pair_window_remaining());
        /* auto=1：板子一个遥控器都没配，正在自己等人来配对 */
        br_kv_add_int(&kv, "auto", br_ble_pair_window_auto() ? 1 : 0);
        br_proto_send_ok("GETPAIRWINDOW", &kv);

    } else if (strcmp(verb, "STATE") == 0) {
        br_ble_dump_state();
        br_proto_send_ok("STATE", NULL);

    } else if (strcmp(verb, "TEST") == 0) {
        /*
         * ★ 硬件自检 ★
         *   造一个假的按键事件：板子的灯会亮，PC 侧也会真的学到一个键。
         *   用来把「按键→灯→PC」这条链路和「遥控器发不发通知」分开验证。
         */
        br_hid_test_key();
        br_proto_send_ok("TEST", NULL);

    } else if (strcmp(verb, "TESTGESTURE") == 0) {
        /*
         * ★ 手势引擎自检 ★
         *   合成按键序列验证单击 / 双击 / 长按的判定。
         *   不需要遥控器在场 —— 现场没法拿遥控器做双击时也能验。
         */
        br_gesture_test_t t;
        bool ok = br_gesture_self_test(&t);
        br_kv_t kv; br_kv_init(&kv);
        br_kv_add_int(&kv, "single", t.single_fired ? 1 : 0);
        br_kv_add_int(&kv, "double", t.double_fired ? 1 : 0);
        br_kv_add_int(&kv, "long", t.long_fired ? 1 : 0);
        br_kv_add_int(&kv, "extra_single_db", t.single_after_double ? 1 : 0);
        br_kv_add_int(&kv, "extra_single_lp", t.single_after_long ? 1 : 0);
        br_kv_add_int(&kv, "pass", ok ? 1 : 0);
        br_proto_send_ok("TESTGESTURE", &kv);

    } else if (strcmp(verb, "REBOOT") == 0) {
        br_proto_send_ok("REBOOT", NULL);
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();

    } else {
        br_proto_send_err(verb, "unknown", NULL);
    }
}

/* ------------------------------------------------------------ CDC 任务 */

static void cdc_task(void *arg)
{
    (void)arg;
    char line[BR_LINE_MAX];

    for (;;) {
        if (bridge_usb_read_line(line, sizeof(line), 20)) {
            handle_command(line);
        }
        bridge_usb_flush_cdc();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

/* ------------------------------------------------------------------ 入口 */

/**
 * 板端日志 → PC 的实时推送。
 *
 * 协议层的 LG 行本来就是给客户端的日志通道，这里把板端滚动缓冲里的
 * 新行喂给它。限流在 br_logbuf 里做（20 行/秒）。
 */
static void log_live_sink(const char *level, const char *tag, const char *msg)
{
    char line[BR_LOGBUF_MSG_MAX + BR_LOGBUF_TAG_MAX + 4];
    snprintf(line, sizeof(line), "%s: %s", tag, msg);
    br_proto_send_lg(level, line);
}

void app_main(void)
{
    /*
     * ★ 日志噪音要压下来 ★
     *
     *   NimBLE 每秒刷一堆 "GATT procedure initiated"，全往 CDC 那条队列塞
     *   → 队列一满 → USB 复合设备卡住 → `tud_hid_ready()` 变假
     *   → **抬起报告发不出去** → 那个键永远按在 PC 上
     *   → 系统自动重复 → 输入框里的文字被一下删光（用户实测）。
     *
     *   我们自己的日志保持 INFO（诊断要看），
     *   NimBLE / bt 的压到 WARN。
     */
    esp_log_level_set("NimBLE", ESP_LOG_WARN);
    esp_log_level_set("bt", ESP_LOG_WARN);

    ESP_LOGI(TAG, "ESP32-S3 Remote Bridge %s", FW_VERSION);

    /* NVS */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /*
     * 日志缓冲要**最先**初始化 ——
     * 之后的每一条 ESP_LOG 都会进这个缓冲。
     */
    br_logbuf_init();

    br_mapping_init();

    /* USB 复合设备 */
    ESP_ERROR_CHECK(bridge_usb_init());
    bridge_usb_install_log_hook();
    ESP_ERROR_CHECK(br_voice_init());

    /* 协议层 */
    br_proto_init(usb_write);

    /* 板端日志实时推送给 PC（限流在 br_logbuf 里做） */
    br_logbuf_set_sink(log_live_sink);

    /* LED */
    bridge_led_init();
    xTaskCreate(bridge_led_task, "led", 3072, NULL, 3, NULL);

    /* BLE */
    ESP_ERROR_CHECK(br_ble_init());

    /* CDC 命令任务 */
    /*
     * cdc 任务帧里有 char line[512]，再叠上 handle_command 的
     * br_parsed_t + br_kv_t，6144 也就刚够；给足余量，
     * 免得再出现 uart_rx 那种「发条命令就重启」。
     */
    xTaskCreate(cdc_task, "cdc", 8192, NULL, 4, NULL);

    /* UART0 走同一套命令（调试备份通道） */
    br_uart_set_line_handler(handle_command);
    br_uart_init();

    br_proto_send_ok("HELLO", NULL);
    ESP_LOGI(TAG, "ready");
}
