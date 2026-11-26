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
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>

#include "br_types.h"
#include "br_proto.h"
#include "br_ble_core.h"
#include "br_ble_gatt.h"
#include "br_hid.h"
#include "br_mapping.h"
#include "br_ckeys.h"
#include "br_usb.h"
#include "br_led.h"
#include "br_uart.h"
#include "br_voice.h"
#include "br_gesture.h"
#include "br_logbuf.h"
#include "br_json_min.h"
#include "br_phone.h"
#include "br_wifi.h"
#include "br_web.h"

static const char *TAG = "app";

#define FW_VERSION "3.0.0"

/*
 * ★ 链路选择（本轮架构调整的核心）★
 *   1 = 旧方案：手机连板子的 WiFi + HTTPS 页面
 *   0 = 新方案：手机走 BLE（GATT），板子完全不开 WiFi
 *
 *   为什么默认 0：ESP32-S3 的 WiFi 与 BLE 共用一根天线。
 *   WiFi 常开 + 遥控器/手机多条 BLE 链路时，射频被抢，
 *   手机链路实测每 5 秒就掉一次（HCI status=8 连接超时），
 *   表现为「用着用着就不响应」。BLE 独占射频后彻底消失。
 */
#ifndef BR_LINK_WIFI
#define BR_LINK_WIFI 0
#endif

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
    /* 客户端连上就问 INFO —— 顺手把手机身份补报一次，
       否则它错过连接瞬间那条 EV PHONE，界面上永远是空的。 */
    br_ble_gatt_report_phone();
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

    } else if (strcmp(verb, "HIDDATA") == 0) {
        /*
         * ★ 订阅「数据通道」报告（语音音频那种大包）★
         *
         * 默认不订（CMCC v3 实测订了会顶断链路）。
         * 小米一代的语音就装在数据通道报告里，取证时用这条命令临时打开。
         */
        long idx = 0, on = 0;
        if (!br_proto_arg_int(&p, 1, &idx) || !br_proto_arg_int(&p, 2, &on)) {
            br_proto_send_err("HIDDATA", "args", NULL);
            return;
        }
        br_hid_set_xiaomi((int)idx, on != 0);
        if (on != 0) {
            /* 重跑一遍订阅流程：这次的表里会带上数据通道 */
            if (!br_ble_hid_resubscribe((int)idx)) {
                br_proto_send_err("HIDDATA", "peer", NULL);
                return;
            }
        }
        br_proto_send_ok("HIDDATA", NULL);

    } else if (strcmp(verb, "HIDREAD") == 0) {
        /* HIDREAD <peer> <handle>：取证用，读一个特征的值 */
        long idx = 0, handle = 0;
        if (!br_proto_arg_int(&p, 1, &idx) || !br_proto_arg_int(&p, 2, &handle)) {
            br_proto_send_err("HIDREAD", "args", NULL);
            return;
        }
        if (!br_hid_probe_read((int)idx, (uint16_t)handle)) {
            br_proto_send_err("HIDREAD", "peer", NULL);
            return;
        }
        br_proto_send_ok("HIDREAD", NULL);

    } else if (strcmp(verb, "HIDWRITE") == 0) {
        /* HIDWRITE <peer> <handle> <hex>：取证用，往特征写数据 */
        long idx = 0, handle = 0;
        if (!br_proto_arg_int(&p, 1, &idx) || !br_proto_arg_int(&p, 2, &handle) ||
            argc < 4) {
            br_proto_send_err("HIDWRITE", "args", NULL);
            return;
        }
        uint8_t buf[64];
        uint16_t len = 0;
        const char *hex = p.argv[3];
        while (hex[0] && hex[1] && len < (uint16_t)sizeof(buf)) {
            char pair[3] = { hex[0], hex[1], 0 };
            buf[len++] = (uint8_t)strtoul(pair, NULL, 16);
            hex += 2;
        }
        if (!br_hid_probe_write((int)idx, (uint16_t)handle, buf, len)) {
            br_proto_send_err("HIDWRITE", "peer", NULL);
            return;
        }
        br_proto_send_ok("HIDWRITE", NULL);

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

    } else if (strcmp(verb, "CKBEGIN") == 0) {
        long n = 0;
        if (!br_proto_arg_int(&p, 1, &n) || !br_ckeys_begin((int)n)) {
            br_proto_send_err("CKBEGIN", "args", NULL);
        } else {
            br_proto_send_ok("CKBEGIN", NULL);
        }

    } else if (strcmp(verb, "CKADD") == 0) {
        /* CKADD <id> <col> <row> <name>；老格式 CKADD <id> <name> 也继续认 */
        long id = 0, col = 0, row = 0;
        const char *nm = "";
        bool ok = br_proto_arg_int(&p, 1, &id);
        if (ok && argc >= 5) {
            ok = br_proto_arg_int(&p, 2, &col) && br_proto_arg_int(&p, 3, &row);
            nm = p.argv[4];
        } else if (ok) {
            nm = argc > 2 ? p.argv[2] : "";
        }
        if (!ok || !br_ckeys_add((int)id, (int)col, (int)row, nm)) {
            br_proto_send_err("CKADD", "args", NULL);
        } else {
            br_proto_send_ok("CKADD", NULL);
        }

    } else if (strcmp(verb, "CKCOMMIT") == 0) {
        if (!br_ckeys_commit()) {
            br_proto_send_err("CKCOMMIT", "nvs", NULL);
        } else {
            br_proto_send_ok("CKCOMMIT", NULL);
            br_ckeys_report();
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

    } else if (strcmp(verb, "PHONEFORGET") == 0) {
        /* PC 删除手机：断开 + 拉黑，除非重新开配对窗口否则不再接受 */
        br_ble_gatt_forget_phone();
        br_proto_send_ok("PHONEFORGET", NULL);

    } else if (strcmp(verb, "PHONDROP") == 0) {
        /* PC 端点「删除这台手机」：断开手机链路（手机端会自动重连） */
        br_ble_gatt_drop_phone();
        br_proto_send_ok("PHONDROP", NULL);

    } else if (strcmp(verb, "WIFI") == 0) {
#if !BR_LINK_WIFI
        /* WiFi 链路已关（手机走 BLE）—— 别让客户端以为配网失败，直接说清楚 */
        br_proto_send_err("WIFI", "disabled", NULL);
#else
        /*
         * WIFI SSID=xxx PASS=yyy —— PC 客户端给板子配网。
         *
         * 板子把凭据存进 NVS 并连接，连上后用 EV WIFI 把 IP 上报，
         * PC 客户端显示给用户（手机要连的就是这个 IP ——
         * IDF 6.1 没有 mDNS 组件，没法用 xxx.local）。
         */
        const char *ssid = NULL;
        char ssid_buf[64] = {0};
        char pass_buf[64] = {0};
        for (int i = 1; i < argc; i++) {
            if (strncmp(p.argv[i], "SSID=", 5) == 0) {
                br_proto_unescape(ssid_buf, sizeof(ssid_buf), p.argv[i] + 5);
                ssid = ssid_buf;
            } else if (strncmp(p.argv[i], "PASS=", 5) == 0) {
                br_proto_unescape(pass_buf, sizeof(pass_buf), p.argv[i] + 5);
            }
        }
        if (!ssid || !*ssid) {
            br_proto_send_err("WIFI", "ssid", NULL);
            return;
        }
        if (br_wifi_set_credentials(ssid, pass_buf)) {
            br_proto_send_ok("WIFI", NULL);
        } else {
            br_proto_send_err("WIFI", "start", NULL);
        }

#endif
    } else if (strcmp(verb, "WIFIINFO") == 0) {
#if !BR_LINK_WIFI
        {
            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add(&kv, "state", "off");
            br_kv_add(&kv, "ip", "");
            br_kv_add(&kv, "ssid", "");
            br_kv_add(&kv, "why", "wifi-disabled");
            br_proto_send_ev("WIFI", &kv);
        }
#else
        /* 查询当前 WiFi 状态与 IP */
        br_wifi_report();

#endif
    } else if (strcmp(verb, "WIFISCAN") == 0) {
#if !BR_LINK_WIFI
        br_proto_send_ok("WIFISCAN", NULL);
#else
        /* 扫描附近 AP（板子只能看见 2.4GHz 的），结果以 DT WIFIAP 逐行上报 */
        br_wifi_scan();

#endif
    } else if (strcmp(verb, "WIFILIST") == 0) {
#if !BR_LINK_WIFI
        br_proto_send_ok("WIFILIST", NULL);
#else
        /* 板子记住的 WiFi 列表（DT WIFINET 逐行 + OK WIFILIST） */
        br_wifi_list_nets();

#endif
    } else if (strcmp(verb, "WIFIDEL") == 0) {
#if !BR_LINK_WIFI
        br_proto_send_err("WIFIDEL", "disabled", NULL);
#else
        /* WIFIDEL IDX=n —— 删掉第 n 个记住的网络，删完把新列表报回来 */
        int idx = -1;
        for (int i = 1; i < argc; i++) {
            if (strncmp(p.argv[i], "IDX=", 4) == 0) {
                idx = atoi(p.argv[i] + 4);
            }
        }
        if (br_wifi_delete_net(idx)) {
            br_wifi_list_nets();
        } else {
            br_proto_send_err("WIFIDEL", "idx", NULL);
        }

#endif
    } else {
        br_proto_send_err(verb, "unknown", NULL);
    }
}

/* ------------------------------------------------------------ CDC 任务 */

static TaskHandle_t s_cdc_task = NULL;

static void cdc_task(void *arg)
{
    (void)arg;
    /* ★ 512B 的行缓冲放静态区 ★
       这个任务的栈要跑 handle_command 的整条调用链（REMOTES 会遍历
       配对表 + 候选表 + 绑定表），已经吃满 8KB ——
       再压一个 512B 的帧就直接溢出重启过（实测：发 REMOTES 板子重启，
       PC 端表现为「连上以后再也收不到任何数据」）。 */
    static char line[BR_LINE_MAX];

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

/*
 * 堆占用巡检。
 *
 * 板子只有 512KB 内部 RAM，BLE + WiFi + TLS 挤在一起。
 * 关键不是「剩多少」，而是「最大的连续块还有多大」——
 * 碎片化会让连续块远小于剩余总量，TLS 握手要的就是连续块。
 */
static void heap_watch_task(void *arg)
{
    (void)arg;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        /*
         * 只在「内存开始不够」时才出声 —— 正常时刷屏没意义，
         * 出问题时日志里能直接看到连续块还剩多少。
         */
        uint32_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
        if (largest < 16384) {
            /* cdc 任务的栈余量也一起打出来：它以前在 REMOTES 上溢过一次，
               重启前只有 panic 一行，事后完全看不出是哪条任务吃满的。 */
            UBaseType_t cdc_hw = s_cdc_task ? uxTaskGetStackHighWaterMark(s_cdc_task) : 0;
            ESP_LOGW(TAG, "[heap] 内部 RAM %u，最大连续块 %u，PSRAM %u，总堆 %u，cdc栈余 %u",
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                     (unsigned)largest,
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)(cdc_hw * sizeof(StackType_t)));
        }
    }
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
    /* TLS 相关的日志开 DEBUG —— HTTPS 起不来时只有这里能看到真正原因 */
    esp_log_level_set("esp_https_server", ESP_LOG_DEBUG);
    esp_log_level_set("httpd", ESP_LOG_WARN);
    esp_log_level_set("httpd_sess", ESP_LOG_WARN);
    esp_log_level_set("tcp_transport", ESP_LOG_WARN);
    /* 浏览器把 keep-alive 连接关掉时 httpd 会报一次 recv 错 —— 无害，
       但每条都是一个警告，刷起来很吵。 */
    esp_log_level_set("httpd_txrx", ESP_LOG_ERROR);
    esp_log_level_set("esp-tls", ESP_LOG_DEBUG);
    esp_log_level_set("mbedtls", ESP_LOG_WARN);

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
    br_ckeys_init();

    /* USB 复合设备 */
    ESP_ERROR_CHECK(bridge_usb_init());
    bridge_usb_install_log_hook();
    ESP_ERROR_CHECK(br_voice_init());

    /* 协议层 */
    br_proto_init(usb_write);

    /*
     * ★ CDC 命令任务必须在这里建（WiFi 起来之前）★
     *
     * 这条任务要跑 handle_command 的整条调用链：
     *   REMOTES → br_ble_list_remotes()（配对表 + 候选表 + 绑定表）
     * 栈需求实测超过 8KB。以前在 WiFi/HTTPS 之后创建，那时内部 RAM
     * 只剩 ~10KB，只能退到 4KB 栈 → 一发 REMOTES 就 **stack overflow
     * 重启**，PC 端看到的是「连上后立刻静默、USB 设备消失」。
     *
     * 现在提前到 WiFi 之前建（此时内部 RAM 还有 ~140KB），给足 16KB。
     */
    if (xTaskCreate(cdc_task, "cdc", 16384, NULL, 4, &s_cdc_task) != pdPASS) {
        ESP_LOGE(TAG, "cdc task create failed at 16KB, retry 8KB");
        if (xTaskCreate(cdc_task, "cdc", 8192, NULL, 4, &s_cdc_task) != pdPASS) {
            ESP_LOGE(TAG, "cdc task create failed even at 8KB");
        }
    }

    /* 板端日志实时推送给 PC（限流在 br_logbuf 里做） */
    br_logbuf_set_sink(log_live_sink);

    /* LED */
    bridge_led_init();
    xTaskCreate(bridge_led_task, "led", 3072, NULL, 3, NULL);

    /* BLE */
    ESP_LOGI(TAG, "BLE 前：总堆 %u，内部 RAM %u，最大连续块 %u",
             (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    ESP_ERROR_CHECK(br_ble_init());
    ESP_LOGI(TAG, "BLE 后：总堆 %u，内部 RAM %u，最大连续块 %u",
             (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));

    /*
     * 手机端（WiFi + HTTPS）
     *
     * 顺序很讲究：
     *   1) 先起 WiFi（STA）—— 没有凭据时自动降级开热点
     *   2) 再起 HTTPS —— 手机要访问的是这个地址
     *   3) 手机事件翻译层
     *
     * ★ 与 BLE 的关系 ★
     *   板子同时是 BLE central（连 Chromecast 遥控器）与 WiFi STA。
     *   ESP32-S3 单射频，两者共用 —— WiFi 发射会遮蔽 BLE 接收。
     *   这是本设计最大的硬件风险（详见计划书），要靠实机压测验证。
     */
    br_phone_init();

#if BR_LINK_WIFI
    /* ★ 回退开关 ★ 手机走 BLE 以后不再需要 WiFi；
       真要回到「手机连板子 WiFi」的老方案，把 BR_LINK_WIFI 改成 1 重新编译。
       两个链路在下位机里是可以同时跑的，但射频只有一根：
       WiFi + 全速 BLE 扫描会让手机链路每几秒就 supervision timeout 掉一次。 */
    br_wifi_init();
    br_web_start();
#else
    ESP_LOGI(TAG, "BLE 模式：不启动 WiFi / HTTP（手机直连蓝牙）");
#endif

    /*
     * 手机快捷键的出厂默认映射。
     * 用户配过就绝不覆盖（br_mapping_apply_phone_defaults 自己判）。
     */
    br_mapping_apply_phone_defaults();

    /*
     * ★ 堆占用巡检（定位 TLS 握手内存不足用）★
     *   板子只有 512KB 内部 RAM，BLE + WiFi + TLS 一起用非常紧。
     *   握手失败时要从这里看「内部 RAM 还剩多少、最大连续块多大」——
     *   碎片化会让最大的连续块远小于剩余总量。
     */
    xTaskCreate(heap_watch_task, "heap", 3072, NULL, 1, NULL);

    /* UART0 走同一套命令（调试备份通道） */
    br_uart_set_line_handler(handle_command);
    br_uart_init();

    br_proto_send_ok("HELLO", NULL);
    ESP_LOGI(TAG, "ready");
}
