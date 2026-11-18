/*
 * br_logbuf - 板端滚动日志缓冲（实现）
 */
#include "br_logbuf.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <esp_log.h>
#include <esp_log_write.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#define LOG_BUF_TAG "br_logbuf"

/* 实时推送：每 100 ms 最多推 2 行 → 20 行/秒 */
#define LIVE_TICK_MS   100
#define LIVE_PER_TICK  2

typedef struct {
    char level[BR_LOGBUF_LEVEL_MAX];
    char tag[BR_LOGBUF_TAG_MAX];
    char msg[BR_LOGBUF_MSG_MAX];
} br_log_line_t;

static br_log_line_t s_lines[BR_LOGBUF_LINES];
static int s_count;          /* 当前有效行数（<= BR_LOGBUF_LINES） */
static int s_head;           /* 下一个写入位置 */
static int s_pushed;         /* 已经推给 PC 的行数（累计计数，用于取增量） */
static int s_total;          /* 累计写入总行数 */
static bool s_live;
static br_logbuf_sink_fn s_sink;
static SemaphoreHandle_t s_lock;

/* ------------------------------------------------------------------ 工具 */

static void copy_field(char *dst, size_t cap, const char *src)
{
    if (cap == 0) {
        return;
    }
    if (!src) {
        dst[0] = '\0';
        return;
    }
    size_t n = strlen(src);
    if (n >= cap) {
        n = cap - 1;
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

/* ------------------------------------------------------------------ 环形缓冲 */

void br_logbuf_append(const char *level, const char *tag, const char *msg)
{
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }

    br_log_line_t *slot = &s_lines[s_head];
    copy_field(slot->level, sizeof(slot->level), level);
    copy_field(slot->tag, sizeof(slot->tag), tag);
    copy_field(slot->msg, sizeof(slot->msg), msg);

    s_head = (s_head + 1) % BR_LOGBUF_LINES;
    if (s_count < BR_LOGBUF_LINES) {
        s_count++;
    }
    s_total++;

    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
}

int br_logbuf_count(void)
{
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
    int n = s_count;
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
    return n;
}

int br_logbuf_get(int index, char *level, size_t level_cap,
                  char *tag, size_t tag_cap,
                  char *msg, size_t msg_cap)
{
    if (index < 0 || index >= br_logbuf_count()) {
        return -1;
    }
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
    /* 最旧的一行在 (head - count) 处（环形） */
    int oldest = (s_head - s_count + BR_LOGBUF_LINES * 2) % BR_LOGBUF_LINES;
    const br_log_line_t *slot = &s_lines[(oldest + index) % BR_LOGBUF_LINES];
    copy_field(level, level_cap, slot->level);
    copy_field(tag, tag_cap, slot->tag);
    copy_field(msg, msg_cap, slot->msg);
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
    return (int)strlen(msg);
}

/* ------------------------------------------------------------------ 实时推送 */

void br_logbuf_set_live(bool on)
{
    s_live = on;
    if (on) {
        /* 从当前末尾开始推，不要把历史重推一遍 */
        s_pushed = s_total;
    }
}

bool br_logbuf_live(void)
{
    return s_live;
}

void br_logbuf_set_sink(br_logbuf_sink_fn fn)
{
    s_sink = fn;
}

/**
 * 把新增的行推给 PC。
 *
 * 一次最多推 LIVE_PER_TICK 行，超出的留到下一轮 ——
 * 日志爆发时宁可慢一点，也不能把 USB CDC 冲爆。
 */
static void live_pump(void)
{
    if (!s_live || s_sink == NULL) {
        return;
    }
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
    int pushed = s_pushed;
    int total = s_total;
    int count = s_count;
    int oldest = (s_head - count + BR_LOGBUF_LINES * 2) % BR_LOGBUF_LINES;
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }

    int pending = total - pushed;
    if (pending <= 0) {
        return;
    }
    /* 行被覆盖掉的部分已经推不出去了，只能跳过 */
    int oldest_seq = total - count;
    if (pushed < oldest_seq) {
        pushed = oldest_seq;
    }

    int n = 0;
    while (pushed < total && n < LIVE_PER_TICK) {
        int index = (oldest + (pushed - oldest_seq)) % BR_LOGBUF_LINES;
        char level[BR_LOGBUF_LEVEL_MAX];
        char tag[BR_LOGBUF_TAG_MAX];
        char msg[BR_LOGBUF_MSG_MAX];
        if (br_logbuf_get(index, level, sizeof(level),
                          tag, sizeof(tag), msg, sizeof(msg)) >= 0) {
            s_sink(level, tag, msg);
        }
        pushed++;
        n++;
    }

    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
    s_pushed = pushed;
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
}

static void live_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(LIVE_TICK_MS));
        live_pump();
    }
}

/* ------------------------------------------------------------------ ESP_LOG 钩子 */

/**
 * ESP-IDF 的日志行格式：
 *     "I (12345) tag: message"
 * 解析出 level / tag / message 三段，省掉时间戳占地方。
 */
static void parse_esp_log(const char *line, char *level, size_t level_cap,
                          char *tag, size_t tag_cap,
                          char *msg, size_t msg_cap)
{
    level[0] = '\0';
    tag[0] = '\0';
    msg[0] = '\0';

    const char *p = line;
    if (*p >= 'A' && *p <= 'Z') {
        level[0] = *p;
        level[1] = '\0';
        p++;
    }
    /* 跳过 " (12345) " */
    while (*p == ' ') {
        p++;
    }
    if (*p == '(') {
        while (*p && *p != ')') {
            p++;
        }
        if (*p == ')') {
            p++;
        }
    }
    while (*p == ' ') {
        p++;
    }
    /* tag 到 ": " 为止 */
    const char *colon = strstr(p, ": ");
    if (colon == NULL) {
        copy_field(msg, msg_cap, p);
        return;
    }
    size_t tag_len = (size_t)(colon - p);
    if (tag_len >= tag_cap) {
        tag_len = tag_cap - 1;
    }
    memcpy(tag, p, tag_len);
    tag[tag_len] = '\0';
    copy_field(msg, msg_cap, colon + 2);
}

/**
 * ESP_LOG 输出钩子。
 *
 * ★ 必须**继续输出到 UART0** ★
 *   我们替换了 esp_log_set_vprintf()，如果不自己往 stdout 写，
 *   UART0 上的日志就全没了 —— 那正是当前唯一可靠的调试通道。
 */
static int bridge_log_vprintf(const char *fmt, va_list args)
{
    char line[224];
    int n = vsnprintf(line, sizeof(line), fmt, args);
    if (n < 0) {
        return 0;
    }
    /* 去掉行尾换行 */
    size_t len = strlen(line);
    while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) {
        line[--len] = '\0';
    }

    /* 1) 进板端滚动缓冲 */
    char level[BR_LOGBUF_LEVEL_MAX];
    char tag[BR_LOGBUF_TAG_MAX];
    char msg[BR_LOGBUF_MSG_MAX];
    parse_esp_log(line, level, sizeof(level), tag, sizeof(tag),
                  msg, sizeof(msg));
    br_logbuf_append(level, tag, msg);

    /* 2) 保持 UART0 的原有输出（stdout） */
    printf("%s\n", line);

    return n;
}

void br_logbuf_init(void)
{
    if (!s_lock) {
        s_lock = xSemaphoreCreateMutex();
    }
    memset(s_lines, 0, sizeof(s_lines));
    s_count = 0;
    s_head = 0;
    s_pushed = 0;
    s_total = 0;
    s_live = false;

    esp_log_set_vprintf(bridge_log_vprintf);

    xTaskCreate(live_task, "log_live", 3072, NULL, 2, NULL);
    ESP_LOGI(LOG_BUF_TAG, "log buffer ready: %d lines", BR_LOGBUF_LINES);
}
