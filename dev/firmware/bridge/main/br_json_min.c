/*
 * br_json_min - 极简 JSON 取值（实现）
 * ---------------------------------------------------------------------------
 * 只做「找键 → 取标量」这一件事，不做通用 JSON 解析。
 * socket.io 的载荷形态固定（扁平对象 + 标量 + 字符串数组），
 * 够用且省 flash。
 */
#include "br_json_min.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

/*
 * 在 obj 里定位 "key" 对应的值。
 *
 * 逐个扫过所有带引号的片段：命中「键名」时看它后面是不是 ':'，
 * 是则值就在其后；不是就跳过（这就把字符串**值**里恰好含键名的情况排掉了）。
 */
static const char *json_find_value(const char *obj, const char *key)
{
    size_t klen;
    const char *p;

    if (!obj || !key || !*key) {
        return NULL;
    }
    klen = strlen(key);

    p = obj;
    while ((p = strchr(p, '"')) != NULL) {
        const char *ks = p + 1;
        const char *ke = strchr(ks, '"');
        if (!ke) {
            return NULL;
        }
        if ((size_t)(ke - ks) == klen && strncmp(ks, key, klen) == 0) {
            const char *q = ke + 1;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') {
                q++;
            }
            if (*q == ':') {
                q++;
                while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') {
                    q++;
                }
                return q;
            }
        }
        p = ke + 1;
    }
    return NULL;
}

/** 跳过一个字符串字面量（从起始的 '"' 开始），返回结尾之后的指针。 */
static const char *json_skip_string(const char *p)
{
    if (*p != '"') {
        return p;
    }
    p++;
    while (*p) {
        if (*p == '\\' && p[1]) {
            p += 2;
            continue;
        }
        if (*p == '"') {
            return p + 1;
        }
        p++;
    }
    return p;
}

/** 跳过一个值（字符串 / 数字 / 布尔 / null / 数组 / 对象）。 */
static const char *json_skip_value(const char *p)
{
    if (*p == '"') {
        return json_skip_string(p);
    }
    if (*p == '[' || *p == '{') {
        char open = *p;
        char close = (open == '[') ? ']' : '}';
        int depth = 0;
        while (*p) {
            if (*p == '"') {
                p = json_skip_string(p);
                continue;
            }
            if (*p == open) {
                depth++;
            } else if (*p == close) {
                depth--;
                if (depth == 0) {
                    return p + 1;
                }
            }
            p++;
        }
        return p;
    }
    while (*p && *p != ',' && *p != '}' && *p != ']') {
        p++;
    }
    return p;
}

/** 把 JSON 字符串内容拷进 out（解转义）。返回拷贝后的长度。 */
static size_t json_unescape(const char *src, char *out, size_t cap)
{
    size_t n = 0;
    while (*src && *src != '"') {
        char c = *src;
        if (c == '\\' && src[1]) {
            src++;
            switch (*src) {
            case 'n': c = '\n'; break;
            case 't': c = '\t'; break;
            case 'r': c = '\r'; break;
            case 'b': c = '\b'; break;
            case 'f': c = '\f'; break;
            case 'u':
                /* Unicode 转义：按原样保留（UTF-8 已经在源里了） */
                c = *src;
                break;
            default: c = *src; break;
            }
        }
        if (n + 1 < cap) {
            out[n++] = c;
        }
        src++;
    }
    return n;
}

bool br_json_get_int(const char *obj, const char *key, long *out)
{
    const char *v = json_find_value(obj, key);
    char *end = NULL;
    long val;

    if (!v || *v == '"') {
        return false;
    }
    val = strtol(v, &end, 10);
    if (end == v) {
        return false;
    }
    *out = val;
    return true;
}

bool br_json_get_double(const char *obj, const char *key, double *out)
{
    const char *v = json_find_value(obj, key);
    char *end = NULL;
    double val;

    if (!v || *v == '"') {
        return false;
    }
    val = strtod(v, &end);
    if (end == v) {
        return false;
    }
    *out = val;
    return true;
}

bool br_json_get_bool(const char *obj, const char *key)
{
    const char *v = json_find_value(obj, key);

    if (!v) {
        return false;
    }
    if (strncmp(v, "true", 4) == 0) {
        return true;
    }
    if (strncmp(v, "false", 5) == 0) {
        return false;
    }
    return false;
}

bool br_json_get_str(const char *obj, const char *key, char *out, size_t cap)
{
    const char *v = json_find_value(obj, key);
    size_t n;

    if (!v || *v != '"' || cap == 0) {
        return false;
    }
    v++;
    n = json_unescape(v, out, cap);
    out[n] = '\0';
    return true;
}

bool br_json_get_raw(const char *obj, const char *key, char *out, size_t cap)
{
    const char *v = json_find_value(obj, key);
    const char *end;
    size_t n;

    if (!v || cap == 0) {
        return false;
    }
    end = json_skip_value(v);
    n = (size_t)(end - v);
    if (n >= cap) {
        n = cap - 1;
    }
    memcpy(out, v, n);
    out[n] = '\0';
    return true;
}

int br_json_str_array(const char *arr, char out[][32], int max)
{
    const char *p;
    int count = 0;

    if (!arr) {
        return 0;
    }
    p = strchr(arr, '[');
    if (!p) {
        return 0;
    }
    p++;
    for (;;) {
        while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
            p++;
        }
        if (*p == ']') {
            break;
        }
        if (*p == '"') {
            char tmp[32];
            size_t n;
            p++;
            n = json_unescape(p, tmp, sizeof(tmp));
            tmp[n] = '\0';
            if (count < max) {
                memcpy(out[count], tmp, sizeof(tmp));
                out[count][sizeof(out[count]) - 1] = '\0';
            }
            count++;
            while (*p && *p != '"') {
                if (*p == '\\' && p[1]) {
                    p++;
                }
                p++;
            }
            if (*p == '"') {
                p++;
            }
        } else {
            p = json_skip_value(p);
        }
        while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
            p++;
        }
        if (*p == ',') {
            p++;
            continue;
        }
        break;
    }
    return count;
}
