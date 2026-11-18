/*
 * br_proto - SP-v2 行协议（固件侧实现）
 */
#include "br_proto.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static br_proto_write_fn s_write;

/* ------------------------------------------------------------------ 转义 */

static const char *k_hex = "0123456789ABCDEF";

/** 只认大写十六进制，与编码端保持一致 */
static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void append_escaped(char *dst, size_t cap, size_t *len, const char *src)
{
    for (const unsigned char *p = (const unsigned char *)src; *p; p++) {
        char enc[4];
        size_t n = 0;
        switch (*p) {
        case ' ':  enc[0] = '%'; enc[1] = '2'; enc[2] = '0'; n = 3; break;
        case '%':  enc[0] = '%'; enc[1] = '2'; enc[2] = '5'; n = 3; break;
        case '=':  enc[0] = '%'; enc[1] = '3'; enc[2] = 'D'; n = 3; break;
        case '\n': enc[0] = '%'; enc[1] = '0'; enc[2] = 'A'; n = 3; break;
        case '\r': enc[0] = '%'; enc[1] = '0'; enc[2] = 'D'; n = 3; break;
        default:
            if (*p < 0x20) {
                enc[0] = '%'; enc[1] = '0'; enc[2] = '2'; enc[3] = k_hex[*p >> 4];
                n = 4;
            } else {
                enc[0] = (char)*p; n = 1;
            }
            break;
        }
        for (size_t i = 0; i < n; i++) {
            if (*len + 1 >= cap) return;
            dst[(*len)++] = enc[i];
        }
    }
}

void br_proto_unescape(char *dst, size_t cap, const char *src)
{
    size_t o = 0;
    for (const char *p = src; *p && o + 1 < cap; p++) {
        if (*p == '%' && p[1] && p[2]) {
            int hi = hexval(p[1]);
            int lo = hexval(p[2]);
            if (hi >= 0 && lo >= 0) {
                dst[o++] = (char)((hi << 4) | lo);
                p += 2;
                continue;
            }
        }
        dst[o++] = *p;
    }
    dst[o] = '\0';
}

/* --------------------------------------------------------------- KV 构建 */

void br_kv_init(br_kv_t *kv)
{
    kv->len = 0;
    kv->overflow = false;
    kv->buf[0] = '\0';
}

static void kv_push(br_kv_t *kv, const char *raw)
{
    size_t n = strlen(raw);
    if (kv->len + n + 2 >= sizeof(kv->buf)) {
        kv->overflow = true;
        return;
    }
    if (kv->len > 0) {
        kv->buf[kv->len++] = ' ';
    }
    memcpy(kv->buf + kv->len, raw, n);
    kv->len += n;
    kv->buf[kv->len] = '\0';
}

void br_kv_add(br_kv_t *kv, const char *key, const char *value)
{
    if (!kv || !key || !value) return;

    size_t klen = strlen(key);
    if (kv->len + klen + 2 >= sizeof(kv->buf)) {
        kv->overflow = true;
        return;
    }
    if (kv->len > 0) {
        kv->buf[kv->len++] = ' ';
    }
    memcpy(kv->buf + kv->len, key, klen);
    kv->len += klen;
    kv->buf[kv->len++] = '=';

    size_t vstart = kv->len;
    append_escaped(kv->buf, sizeof(kv->buf), &kv->len, value);
    kv->buf[kv->len] = '\0';
    (void)vstart;
}

void br_kv_add_int(br_kv_t *kv, const char *key, long value)
{
    char tmp[24];
    snprintf(tmp, sizeof(tmp), "%ld", value);
    br_kv_add(kv, key, tmp);
}

void br_kv_add_hex(br_kv_t *kv, const char *key, const uint8_t *data, size_t len)
{
    size_t klen = strlen(key);
    if (kv->len + klen + 2 + len * 2 + 1 >= sizeof(kv->buf)) {
        kv->overflow = true;
        return;
    }
    if (kv->len > 0) {
        kv->buf[kv->len++] = ' ';
    }
    memcpy(kv->buf + kv->len, key, klen);
    kv->len += klen;
    kv->buf[kv->len++] = '=';
    for (size_t i = 0; i < len; i++) {
        kv->buf[kv->len++] = k_hex[data[i] >> 4];
        kv->buf[kv->len++] = k_hex[data[i] & 0x0F];
    }
    kv->buf[kv->len] = '\0';
}

void br_kv_add_flag(br_kv_t *kv, const char *key)
{
    kv_push(kv, key);
}

/* ------------------------------------------------------------------ 发送 */

void br_proto_init(br_proto_write_fn write_fn)
{
    s_write = write_fn;
}

/*
 * 发一行。
 *
 * ★ 分三段写，不拼大缓冲 ★
 *   协议是流式行文本，CDC 侧不关心一次 write 的边界。
 *   拼整行需要在栈上开 2KB 缓冲 —— 实测直接把 cdc 任务打爆栈。
 */
static void emit(const char *token, const char *rest, const br_kv_t *kv)
{
    char head[96];
    int n = snprintf(head, sizeof(head), "%s%s%s", token,
                     rest ? " " : "", rest ? rest : "");
    if (n < 0) {
        return;
    }
    if ((size_t)n >= sizeof(head)) {
        n = (int)sizeof(head) - 1;
    }
    if (s_write) {
        s_write(head, (size_t)n);
    }
    if (kv && kv->len > 0 && kv->buf[0]) {
        if (s_write) {
            s_write(" ", 1);
            s_write(kv->buf, kv->len);
        }
    }
    if (s_write) {
        s_write("\n", 1);
    }
}

void br_proto_send_ok(const char *verb, const br_kv_t *kv)
{
    emit("OK", verb, kv);
}

void br_proto_send_err(const char *verb, const char *reason, const br_kv_t *kv)
{
    char rest[128];
    snprintf(rest, sizeof(rest), "%s %s", verb ? verb : "?", reason ? reason : "unknown");
    emit("ER", rest, kv);
}

void br_proto_send_ev(const char *event, const br_kv_t *kv)
{
    char rest[128];
    snprintf(rest, sizeof(rest), "%s", event ? event : "?");
    emit("EV", rest, kv);
}

void br_proto_send_dt(const char *kind, const br_kv_t *kv)
{
    char rest[128];
    snprintf(rest, sizeof(rest), "%s", kind ? kind : "?");
    emit("DT", rest, kv);
}

void br_proto_send_lg(const char *level, const char *msg)
{
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add(&kv, "level", level ? level : "info");
    br_kv_add(&kv, "msg", msg ? msg : "");
    emit("LG", NULL, &kv);
}

void br_proto_send_raw(const char *text)
{
    char head[128];
    int n = snprintf(head, sizeof(head), "# %s", text ? text : "");
    if (n < 0) {
        return;
    }
    if ((size_t)n >= sizeof(head)) {
        n = (int)sizeof(head) - 1;
    }
    if (s_write) {
        s_write(head, (size_t)n);
        s_write("\n", 1);
    }
}

/* ------------------------------------------------------------------ 解析 */

int br_proto_parse(char *line, br_parsed_t *out)
{
    out->argc = 0;
    if (!line) return 0;

    /* 去掉行尾空白 */
    size_t n = strlen(line);
    while (n > 0 && (line[n - 1] == '\r' || line[n - 1] == '\n' ||
                     line[n - 1] == ' ' || line[n - 1] == '\t')) {
        line[--n] = '\0';
    }
    if (n == 0) return 0;

    /* 复制到 storage，改动不影响调用方的原串 */
    if (n >= sizeof(out->storage)) {
        n = sizeof(out->storage) - 1;
    }
    memcpy(out->storage, line, n);
    out->storage[n] = '\0';

    char *p = out->storage;
    while (*p && out->argc < BR_MAX_ARGS) {
        while (*p == ' ' || *p == '\t') p++;
        if (!*p) break;
        out->argv[out->argc++] = p;
        while (*p && *p != ' ' && *p != '\t') p++;
        if (*p) *p++ = '\0';
    }
    return out->argc;
}

bool br_proto_arg_int(const br_parsed_t *p, int i, long *out)
{
    if (!p || i < 0 || i >= p->argc || !p->argv[i]) return false;
    char *end = NULL;
    long v = strtol(p->argv[i], &end, 0);
    if (end == p->argv[i]) return false;
    if (out) *out = v;
    return true;
}
