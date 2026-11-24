/*
 * br_phone - 手机事件 → USB HID 翻译层（实现）
 * ---------------------------------------------------------------------------
 * 事件名与参数形态**完全照抄 movo 的前端**：
 *   move       {dx, dy}            相对位移（浮点）
 *   click      {button}            left / right / middle / x1 / x2
 *   scroll     {dx, dy}            滚轮
 *   zoom       {dir, steps}        双指捏合 = Ctrl + 滚轮
 *   drag_start / drag_end          按住左键拖动
 *   mid_down / mid_up              中键按下/抬起
 *   tap_key    {key}               点按一个键（按下即抬起）
 *   key_action {key, action}       down / up
 *   key_combo  {keys:[...]}        组合键
 *   type_text  {text}              文本（板子做不了 → 上报 PC）
 *   sys_action {action, url}       PC 专属动作（上报 PC）
 *   voice_start / voice_end        语音开关
 *   voice_audio                     二进制 PCM
 */
#include "br_phone.h"
#include "br_led.h"
#include "br_ble_core.h"
#include "br_ble_gatt.h"
#include "br_ckeys.h"
#include "br_json_min.h"
#include "br_web.h"
#include "br_mapping.h"
#include "br_proto.h"
#include "br_types.h"
#include "br_usb.h"
#include "br_voice.h"

#include <string.h>
#include <stdlib.h>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <esp_log.h>
#include <esp_timer.h>

static const char *TAG = "br_phone";

/*
 * ★ 修饰键掩码 ★
 *
 * 注意区分两个东西：
 *   - HID **usage** 码：lctrl = 0xE0（查表用）
 *   - USB HID **modifier 位掩码**：lctrl = 0x01（发报告用）
 * bridge_usb_press_keyboard() 的第二个参数是位掩码，不是 usage。
 */
#define BR_MOD_LCTRL 0x01
#define BR_MOD_LSHIFT 0x02
#define BR_MOD_LALT  0x04
#define BR_MOD_LWIN  0x08

/* ------------------------------------------------------------------ 键名表 */

/*
 * 手机端键名 → HID usage。
 *
 * 名字来自 movo 的前端：
 *   - 触控板页快捷键栏（index.html:2087）用 esc / volumedown / volumeup
 *   - 键盘页（keyboard.html）用 a~z / 0~9 / f1~f12 / enter / esc / tab /
 *     space / backspace / delete / up / down / left / right / prtsc
 *     以及 ctrl / shift / alt / win / caps 这些修饰键
 *
 * 消费者键（音量/播放）走 16 位 usage，与键盘 usage 分开存。
 */
typedef struct {
    const char *name;
    uint16_t    usage;      /* 键盘页 usage */
    uint16_t    consumer;   /* 消费者页 usage（0 = 不是消费者键） */
} br_pkey_t;

static const br_pkey_t s_phone_keys[] = {
    /* 修饰键（键盘页 0xE0~0xE7） */
    {"ctrl",     0xE0, 0}, {"shift", 0xE1, 0}, {"alt", 0xE2, 0}, {"win", 0xE3, 0},
    {"ctrl_r",   0xE4, 0}, {"shift_r", 0xE5, 0}, {"alt_r", 0xE6, 0}, {"win_r", 0xE7, 0},
    {"caps",     0x39, 0},

    /* 导航 */
    {"up",       0x52, 0}, {"down", 0x51, 0}, {"left", 0x50, 0}, {"right", 0x4F, 0},
    {"home",     0x4A, 0}, {"end", 0x4D, 0}, {"page_up", 0x4B, 0}, {"page_down", 0x4E, 0},
    {"insert",   0x49, 0}, {"delete", 0x4C, 0},

    /* 编辑 */
    {"enter",    0x28, 0}, {"esc", 0x29, 0}, {"tab", 0x2B, 0},
    {"backspace", 0x2A, 0}, {"space", 0x2C, 0},
    {"prtsc",    0x46, 0}, {"printscreen", 0x46, 0},
    {"pause",    0x45, 0}, {"scroll_lock", 0x47, 0},
    {"num_lock", 0x53, 0},

    /* 符号 */
    {"minus",    0x2D, 0}, {"equal", 0x2E, 0},
    {"bracket_l", 0x2F, 0}, {"bracket_r", 0x30, 0},
    {"backslash", 0x31, 0}, {"semicolon", 0x33, 0},
    {"quote",    0x34, 0}, {"backtick", 0x35, 0},
    {"comma",    0x36, 0}, {"dot", 0x37, 0}, {"period", 0x37, 0},
    {"slash",    0x38, 0},

    /* 消费者键（触控板页快捷键栏） */
    {"volumeup", 0, 0xE9}, {"volumedown", 0, 0xEA}, {"mute", 0, 0xE2},
    {"play_pause", 0, 0xCD}, {"next", 0, 0xB5}, {"prev", 0, 0xB6},
    {"media_volume_up", 0, 0xE9}, {"media_volume_down", 0, 0xEA},
    {"media_volume_mute", 0, 0xE2},
    {"media_play_pause", 0, 0xCD}, {"media_next", 0, 0xB5},
    {"media_previous", 0, 0xB6},
};

/* 字母与数字：ASCII → HID usage 直接算，不进表 */
static bool ascii_to_usage(char c, uint8_t *usage)
{
    if (c >= 'a' && c <= 'z') {
        *usage = (uint8_t)(0x04 + (c - 'a'));
        return true;
    }
    if (c >= '1' && c <= '9') {
        *usage = (uint8_t)(0x1E + (c - '1'));
        return true;
    }
    if (c == '0') {
        *usage = 0x27;
        return true;
    }
    return false;
}

/**
 * 查一个键名 → usage。
 *
 * @param usage     键盘页 usage（消费者键时为 0）
 * @param consumer  消费者页 usage（普通键时为 0）
 */
static bool lookup_key(const char *name, uint8_t *usage, uint16_t *consumer)
{
    size_t i;

    if (!name || !*name) {
        return false;
    }
    *usage = 0;
    *consumer = 0;

    if (strlen(name) == 1) {
        if (ascii_to_usage(name[0], usage)) {
            return true;
        }
        /* 单字符也可能是符号 */
        switch (name[0]) {
        case '-': *usage = 0x2D; return true;
        case '=': *usage = 0x2E; return true;
        case '[': *usage = 0x2F; return true;
        case ']': *usage = 0x30; return true;
        case '\\': *usage = 0x31; return true;
        case ';': *usage = 0x33; return true;
        case '\'': *usage = 0x34; return true;
        case '`': *usage = 0x35; return true;
        case ',': *usage = 0x36; return true;
        case '.': *usage = 0x37; return true;
        case '/': *usage = 0x38; return true;
        case ' ': *usage = 0x2C; return true;
        default: break;
        }
        return false;
    }

    for (i = 0; i < sizeof(s_phone_keys) / sizeof(s_phone_keys[0]); i++) {
        if (strcmp(s_phone_keys[i].name, name) == 0) {
            *usage = (uint8_t)s_phone_keys[i].usage;
            *consumer = s_phone_keys[i].consumer;
            return true;
        }
    }
    /* f1~f12 */
    if (name[0] == 'f' && name[1] && name[2] == '\0' &&
        name[1] >= '1' && name[1] <= '9') {
        *usage = (uint8_t)(0x3A + (name[1] - '1'));
        return true;
    }
    if (strcmp(name, "f10") == 0) { *usage = 0x44; return true; }
    if (strcmp(name, "f11") == 0) { *usage = 0x45; return true; }
    if (strcmp(name, "f12") == 0) { *usage = 0x46; return true; }
    return false;
}

/* ------------------------------------------------------------------ 指针 */

/** 鼠标按键名 → 掩码 */
static uint8_t mouse_button_mask(const char *button)
{
    if (!button) {
        return 0;
    }
    if (strcmp(button, "left") == 0)   return BR_MOUSE_LEFT;
    if (strcmp(button, "right") == 0)  return BR_MOUSE_RIGHT;
    if (strcmp(button, "middle") == 0) return BR_MOUSE_MIDDLE;
    return 0;
}

/**
 * 双指捏合缩放 = 按住 Ctrl 再滚轮。
 *
 * ★ 顺序严格 ctrl down → wheel → ctrl up ★
 *   多数应用是靠 GetKeyState(VK_CONTROL) 判断这次滚轮该不该缩放，
 *   先滚后按完全没反应（照抄 movo_mouse_service.py:204-238 的注释）。
 */
static void do_zoom(int steps)
{
    int i;

    if (steps < 1) {
        steps = 1;
    }
    if (steps > 6) {
        steps = 6;   /* 一次最多 6 档，防止误触手抖放大过头 */
    }

    bridge_usb_press_keyboard(BR_MOD_LCTRL, NULL, 0);
    /* 抬起 modifier 与滚轮之间留一点余量，让应用先看到 Ctrl 按下 */
    vTaskDelay(pdMS_TO_TICKS(15));

    for (i = 0; i < steps; i++) {
        bridge_usb_mouse_scroll(-1);
        /* 两档之间留一点间隔，太密会被应用合并成一次，缩放就"吃档"了 */
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    vTaskDelay(pdMS_TO_TICKS(12));
    bridge_usb_release_keyboard(BR_MOD_LCTRL, NULL, 0);
}

/* ------------------------------------------------------------------ 键盘 */

/**
 * 按下/抬起一个键。
 *
 * 消费者键（音量之类）走消费者报告，普通键走键盘报告。
 */
static void phone_key(const char *name, bool down)
{
    uint8_t usage = 0;
    uint16_t consumer = 0;

    if (!lookup_key(name, &usage, &consumer)) {
        ESP_LOGW(TAG, "未知键名: %s", name ? name : "(null)");
        return;
    }
    if (consumer) {
        if (down) {
            bridge_usb_press_consumer(consumer);
        } else {
            bridge_usb_release_consumer(consumer);
        }
        return;
    }
    if (usage == 0) {
        return;
    }
    if (down) {
        bridge_usb_press_keyboard(0, &usage, 1);
    } else {
        bridge_usb_release_keyboard(0, &usage, 1);
    }
}

/** 组合键：全部按下 → 全部抬起 */
static void phone_key_combo(const char *keys_json)
{
    char raw[256];
    char names[8][32];
    uint8_t usages[8];
    int count, i;
    int n = 0;

    if (!br_json_get_raw(keys_json, "keys", raw, sizeof(raw))) {
        return;
    }
    count = br_json_str_array(raw, names, 8);
    if (count <= 0) {
        return;
    }
    for (i = 0; i < count && n < 6; i++) {
        uint8_t u = 0;
        uint16_t c = 0;
        if (!lookup_key(names[i], &u, &c) || u == 0 || c != 0) {
            /* 组合键里暂不支持消费者键 —— 跳过它，不影响其余键 */
            continue;
        }
        usages[n++] = u;
    }
    if (n == 0) {
        return;
    }
    /* 修饰键（0xE0~0xE7）要走 modifier 位，不能当普通键码 */
    {
        uint8_t mods = 0;
        uint8_t plain[6];
        int np = 0;
        for (i = 0; i < n; i++) {
            if (usages[i] >= 0xE0 && usages[i] <= 0xE7) {
                mods |= (uint8_t)(1 << (usages[i] - 0xE0));
            } else if (np < 6) {
                plain[np++] = usages[i];
            }
        }
        bridge_usb_press_keyboard(mods, plain, (uint8_t)np);
        vTaskDelay(pdMS_TO_TICKS(40));
        bridge_usb_release_keyboard(mods, plain, (uint8_t)np);
    }
}

/* ------------------------------------------------------------------ 上报 */

/** 把文本上报给 PC 客户端（板子注入不了 Unicode 文本） */
static void report_text(const char *args)
{
    char text[512];
    br_kv_t kv;

    if (!br_json_get_str(args, "text", text, sizeof(text))) {
        return;
    }
    /*
     * ★ 文本一律回 PC 注入（v1.2.13 恢复）★
     *   板子用 HID 直接敲字母会被电脑的中文输入法当成拼音
     *   （实测 hidOK123 → 记得OK23），行为不可预测；
     *   用户已确认：中文本来就必须开 PC 端软件，那就统一走注入，
     *   英文/中文都由客户端用剪贴板落地，绕开输入法。
     */
    br_kv_init(&kv);
    br_kv_add(&kv, "text", text);
    br_proto_send_ev("TEXT", &kv);
    ESP_LOGI(TAG, "上报文本 %u 字节", (unsigned)strlen(text));
}

/** 把 PC 专属动作上报给 PC 客户端 */
static void report_sys_action(const char *args)
{
    char action[32];
    char url[256];
    br_kv_t kv;

    if (!br_json_get_str(args, "action", action, sizeof(action))) {
        return;
    }
    br_kv_init(&kv);
    br_kv_add(&kv, "action", action);
    if (br_json_get_str(args, "url", url, sizeof(url))) {
        br_kv_add(&kv, "url", url);
    }
    br_proto_send_ev("SYSACT", &kv);
    ESP_LOGI(TAG, "上报系统动作 %s", action);
}

/* ------------------------------------------------------------------ 快捷键 */

/*
 * 触控板页快捷键栏的键名 → 语义 usage。
 *
 * ★ 这一组走映射表 ★
 *   语义上等同遥控器上的实体键，用户可以在 PC 端给它们配任意动作。
 *   查不到映射时回落到内置默认行为（见 phone_key）——
 *   这样「没配过也能用」，配了就以配置为准。
 */
static bool quickbar_usage(const char *name, uint16_t *usage)
{
    if (!name) {
        return false;
    }
    if (strcmp(name, "esc") == 0)              { *usage = BR_PHU_ESC; return true; }
    if (strcmp(name, "volumeup") == 0)         { *usage = BR_PHU_VOLUME_UP; return true; }
    if (strcmp(name, "volumedown") == 0)       { *usage = BR_PHU_VOLUME_DOWN; return true; }
    if (strcmp(name, "mute") == 0)             { *usage = BR_PHU_MUTE; return true; }
    if (strcmp(name, "play_pause") == 0)       { *usage = BR_PHU_PLAY_PAUSE; return true; }
    if (strcmp(name, "shortcut") == 0)         { *usage = BR_PHU_SHORTCUT; return true; }
    /* 自定义键：ck<id>，1..12 → 0x101..0x10C */
    if (name[0] == 'c' && name[1] == 'k') {
        int id = 0;
        const char *p = name + 2;
        while (*p >= '0' && *p <= '9') {
            id = id * 10 + (*p - '0');
            if (id > 999) return false;
            p++;
        }
        if (*p == 0 && id >= 1 && id <= (int)BR_PHU_CUSTOM_MAX) {
            *usage = (uint16_t)(BR_PHU_CUSTOM_BASE + id);
            return true;
        }
    }
    return false;
}

/** 快捷键的出厂默认行为（映射表里没有这一条时用） */
static void quickbar_default(uint16_t usage, bool down)
{
    switch (usage) {
    case BR_PHU_ESC:
        phone_key("esc", down);
        break;
    case BR_PHU_VOLUME_UP:
        phone_key("volumeup", down);
        break;
    case BR_PHU_VOLUME_DOWN:
        phone_key("volumedown", down);
        break;
    case BR_PHU_MUTE:
        phone_key("mute", down);
        break;
    case BR_PHU_PLAY_PAUSE:
        phone_key("play_pause", down);
        break;
    case BR_PHU_SHORTCUT:
        /* 自定义快捷键：默认不发键；由用户映射（组合键板子执行 / 打开软件 PC 执行） */
        break;
    default:
        break;
    }
}

/**
 * 手机快捷键也发 EV KEY 给 PC。
 *
 * 「打开软件」这类动作板子做不了，只能 PC 执行；PC 端需要按下/抬起
 * 两帧才能判单击/双击/长按。映射到组合键的键 PC 端会跳过（不是 PC 专属动作），
 * 所以这里无条件上报不会误触发。
 */
static void report_phone_key(uint16_t usage, bool down)
{
    static const uint8_t phone_addr[6] = BR_PHONE_ADDR;
    char addr_s[18];
    br_kv_t kv;
    br_kv_init(&kv);
    br_addr_to_str(phone_addr, addr_s, sizeof(addr_s));
    br_kv_add(&kv, "addr", addr_s);
    br_kv_add_int(&kv, "src", BR_SRC_PHONE);
    br_kv_add_int(&kv, "usage", usage);
    br_kv_add(&kv, "state", down ? "down" : "up");
    br_proto_send_ev("KEY", &kv);
}

/**
 * 走映射表的按键。
 *
 * @return true = 已经交给映射表处理
 */
static bool phone_mapped_key(const char *name, bool down)
{
    uint16_t usage = 0;
    uint32_t key_id;
    static const uint8_t phone_addr[6] = BR_PHONE_ADDR;

    if (!quickbar_usage(name, &usage)) {
        return false;
    }
    report_phone_key(usage, down);
    key_id = BR_KEY_MAKE(BR_SRC_PHONE, usage);

    /*
     * 映射表里没有这一条 → 用出厂默认。
     * br_mapping_has_key 是只读查询，不会误触发 MAPMISS 上报。
     */
    if (!br_mapping_has_key(phone_addr, key_id)) {
        quickbar_default(usage, down);
        return true;
    }
    br_mapping_key(phone_addr, key_id, down);
    return true;
}

/* ------------------------------------------------------------------ 入口 */

/*
 * ★ 手机最近一次说话（发事件）的时间 ★
 *   手机的 WebView 开着就会每 2~3 秒发一次心跳，
 *   所以「多久没动静」就能当成「手机还连着没有」。
 */
static volatile int64_t s_last_activity_ms;

bool br_phone_recently_active(void)
{
    if (s_last_activity_ms == 0) {
        return false;
    }
    return (esp_timer_get_time() - s_last_activity_ms) < BR_PHONE_ONLINE_MS;
}

void br_phone_init(void)
{
    ESP_LOGI(TAG, "手机事件翻译层就绪");
}

void br_phone_push_pcm(const uint8_t *pcm, size_t len)
{
    br_voice_push_phone_pcm(pcm, len);
}

void br_phone_push_adpcm(const uint8_t *data, size_t len, bool reset)
{
    br_voice_push_phone_adpcm(data, len, reset);
}

void br_phone_voice_start(void)
{
    br_voice_phone_start();
}

void br_phone_voice_end(void)
{
    br_voice_phone_end();
}

void br_phone_event(const char *name, const char *args)
{
    if (!name) {
        return;
    }
    /*
     * 手机有**输入**才闪灯 —— 用户据此确认「板子收到了」。
     *
     * ★ 必须过滤心跳 ★
     *   页面 link.js 每 3 秒发一次 hb 探活；把它也当输入的话，
     *   灯就会「隔几秒闪一下」，白灯又亮又刺眼（用户实测反馈）。
     */
    static const char *k_input_ev[] = {
        "move", "click", "scroll", "drag_start", "drag_end",
        "mid_down", "mid_up", "tap_key", "key", "press", "release",
        "combo", "type_text",
    };
    for (size_t i = 0; i < sizeof(k_input_ev) / sizeof(k_input_ev[0]); i++) {
        if (strcmp(name, k_input_ev[i]) == 0) {
            bridge_led_phone_activity();
            break;
        }
    }
    /*
     * ★ 手机在线状态靠这个时间戳 ★
     *   手机每 2~3 秒发一次心跳，任何事件也算数。
     *   PC 端要显示「手机现在连着没有」—— 不能像以前那样永远写死
     *   conn=1（手机明明没开，界面上却写着「已连接」）。
     */
    s_last_activity_ms = esp_timer_get_time();

    /* ---- 指针类 ---- */
    if (strcmp(name, "move") == 0) {
        double dx = 0, dy = 0;
        if (args) {
            br_json_get_double(args, "dx", &dx);
            br_json_get_double(args, "dy", &dy);
        }
        /*
         * ★ 小数别扔 ★
         *   触控板给的是整像素，陀螺仪给的是 0.4 像素这种小数
         *   （50Hz 一次，每次 0.4 像素 = 20 像素/秒，稳稳地走）。
         *   以前每条都四舍五入成整数 → 每条都是 0 → 慢速移动完全看不见。
         *   这里把余数留到下一条，累计够了再发。
         */
        static double rem_x, rem_y;
        rem_x += dx;
        rem_y += dy;
        int32_t ix = (int32_t)rem_x;
        int32_t iy = (int32_t)rem_y;
        rem_x -= ix;
        rem_y -= iy;
        bridge_usb_mouse_move(ix, iy);
        return;
    }

    if (strcmp(name, "click") == 0) {
        char button[16];
        uint8_t mask;
        if (!args || !br_json_get_str(args, "button", button, sizeof(button))) {
            return;
        }
        mask = mouse_button_mask(button);
        if (!mask) {
            return;
        }
        /* 点按 = 按下即抬起 */
        bridge_usb_press_mouse_button(mask);
        vTaskDelay(pdMS_TO_TICKS(15));
        bridge_usb_release_mouse_button(mask);
        return;
    }

    if (strcmp(name, "scroll") == 0) {
        double dx = 0, dy = 0;
        if (args) {
            br_json_get_double(args, "dx", &dx);
            br_json_get_double(args, "dy", &dy);
        }
        if (dy != 0) {
            bridge_usb_mouse_scroll((int32_t)(dy >= 0 ? dy + 0.5 : dy - 0.5));
        }
        if (dx != 0) {
            /* 水平滚动：Windows 侧滚轮没有横向轴，用 Shift+滚轮近似 */
            bridge_usb_press_keyboard(BR_MOD_LSHIFT, NULL, 0);
            vTaskDelay(pdMS_TO_TICKS(15));
            bridge_usb_mouse_scroll((int32_t)(dx >= 0 ? dx + 0.5 : dx - 0.5));
            vTaskDelay(pdMS_TO_TICKS(12));
            bridge_usb_release_keyboard(BR_MOD_LSHIFT, NULL, 0);
        }
        return;
    }

    if (strcmp(name, "zoom") == 0) {
        long steps = 1;
        if (args) {
            br_json_get_int(args, "steps", &steps);
        }
        do_zoom((int)steps);
        return;
    }

    if (strcmp(name, "drag_start") == 0) {
        bridge_usb_press_mouse_button(BR_MOUSE_LEFT);
        return;
    }
    if (strcmp(name, "drag_end") == 0) {
        bridge_usb_release_mouse_button(BR_MOUSE_LEFT);
        return;
    }
    if (strcmp(name, "mid_down") == 0) {
        bridge_usb_press_mouse_button(BR_MOUSE_MIDDLE);
        return;
    }
    if (strcmp(name, "mid_up") == 0) {
        bridge_usb_release_mouse_button(BR_MOUSE_MIDDLE);
        return;
    }

    /* ---- 键盘类 ---- */
    if (strcmp(name, "tap_key") == 0) {
        char key[32];
        if (!args || !br_json_get_str(args, "key", key, sizeof(key))) {
            return;
        }
        /* 快捷键栏的键走映射表，其余（键盘页的字面输入）直接发 HID */
        if (phone_mapped_key(key, true)) {
            vTaskDelay(pdMS_TO_TICKS(15));
            phone_mapped_key(key, false);
            return;
        }
        phone_key(key, true);
        vTaskDelay(pdMS_TO_TICKS(15));
        phone_key(key, false);
        return;
    }

    if (strcmp(name, "key_action") == 0) {
        char key[32];
        char action[8];
        if (!args || !br_json_get_str(args, "key", key, sizeof(key))) {
            return;
        }
        if (!br_json_get_str(args, "action", action, sizeof(action))) {
            return;
        }
        {
            bool down = strcmp(action, "up") != 0;
            if (phone_mapped_key(key, down)) {
                return;
            }
            phone_key(key, down);
        }
        return;
    }

    if (strcmp(name, "key_combo") == 0) {
        phone_key_combo(args);
        return;
    }

    /* ---- PC 专属类 ---- */
    if (strcmp(name, "phone_info") == 0) {
        /* App 自己上报的机型名（Build.MODEL）—— 比读 GATT 0x2A00 可靠 */
        char nm[48] = "";
        if (br_json_get_str(args, "name", nm, sizeof(nm))) {
            br_ble_gatt_set_phone_name(nm);
        }
        return;
    }
    if (strcmp(name, "type_text") == 0) {
        report_text(args);
        return;
    }
    if (strcmp(name, "sys_action") == 0) {
        report_sys_action(args);
        return;
    }

    /* ---- 语音类 ---- */
    if (strcmp(name, "voice_status") == 0) {
        /*
         * ★ 就绪查询：必须先回 ready:true，手机才肯发 voice_start ★
         *   页面（voice.js）按住麦克风时先查 ready，没就绪就只弹
         *   「识别引擎还在加载，稍后再试」并再问一次 voice_status ——
         *   它**不会**主动发 voice_start。以前板子只在收到 voice_start
         *   之后才回 voice_state，于是「等就绪 → 才发 start → 才回就绪」
         *   死锁，语音永远按不下去（用户实测 + COM6 零语音日志）。
         *   识别在 PC（Windows 输入法），板子只把手机 PCM 转成 UAC
         *   麦克风送过去 —— 通道一通就算就绪，这里直接答 ready:true。
         */
        br_web_send_event("voice_state", "{\"ready\":true}");
        return;
    }
    if (strcmp(name, "ckget") == 0) {
        /* 手机页晚于订阅加载时主动拉一次清单 */
        br_ckeys_report();
        return;
    }
    if (strcmp(name, "voice_start") == 0) {
        br_phone_voice_start();
        /*
         * ★ 告诉手机「可以开始收音了」★
         *   页面等的就是这条：没有它，手机上一律显示
         *   「识别引擎还在加载，稍后再试」，用户根本按不下去。
         *   我们的识别引擎在 PC 上（Windows 输入法），板子只是把
         *   手机采到的 PCM 转成 UAC 麦克风送过去 —— 通道一通就算就绪。
         */
        br_web_send_event("voice_state", "{\"ready\":true}");
        return;
    }
    if (strcmp(name, "voice_end") == 0) {
        br_phone_voice_end();
        return;
    }

    if (strcmp(name, "gyro_probe") == 0) {
        /* 页面探针：报告传感器的实际情况（有没有数据、页面是不是安全上下文）。
           这是判断「陀螺仪为什么没反应」最直接的证据。 */
        ESP_LOGI(TAG, "陀螺仪探针 %s", args ? args : "{}");
        return;
    }

    if (strcmp(name, "__diag") == 0) {
        /* 页面诊断（voice.js 的 [mic] … / [gyro] …）：落到 COM6，
           不用连手机 logcat 就能看出语音/陀螺仪卡在哪一层。 */
        ESP_LOGI(TAG, "页面诊断 %s", args ? args : "");
        return;
    }

    ESP_LOGD(TAG, "未处理的事件: %s", name);
}
