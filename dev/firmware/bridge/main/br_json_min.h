/*
 * br_json_min - 极简 JSON 取值
 * ---------------------------------------------------------------------------
 * 为什么不用 cJSON：
 *   ESP-IDF v6.1 的 components 里**没有 json 组件**（已全目录确认），
 *   而 socket.io 的载荷全是「扁平对象 + 标量」这一种形态
 *   （{"dx":12,"dy":-3} / {"key":"up"} / {"keys":["ctrl","c"]}），
 *   为此拉一个通用 JSON 库不划算 —— 自己写一个够用的更省 flash 也更好控。
 *
 * 支持：
 *   - 扁平对象的 int / double / bool / string 取值
 *   - 字符串数组取值（key_combo 用）
 * 不支持（遇到就当没这个键）：
 *   - 嵌套对象、数组里的对象、Unicode \uXXXX 转义（按原样保留）
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 取整数。obj 是对象文本（可带可不带花括号）。
 * 找不到该键或值不是数字时返回 false。
 */
bool br_json_get_int(const char *obj, const char *key, long *out);

/**
 * 取浮点数。找不到或不是数字时返回 false。
 */
bool br_json_get_double(const char *obj, const char *key, double *out);

/**
 * 取字符串（已解转义，\n \t \" \\ 会被还原）。
 * cap 不含结尾 0。找不到该键或值不是字符串时返回 false。
 */
bool br_json_get_str(const char *obj, const char *key, char *out, size_t cap);

/**
 * 取字符串数组。arr 是数组文本（形如 ["ctrl","c"]）。
 * 返回实际元素个数（可能大于 max —— 超出的部分被丢弃）。
 */
int br_json_str_array(const char *arr, char out[][32], int max);

/**
 * 取布尔值。找到且为 true 时返回 true。
 */
bool br_json_get_bool(const char *obj, const char *key);

/**
 * 取原始值文本（不做反转义）。
 *
 * 用来取数组：拿到 "keys":["ctrl","c"] 里数组的原文，
 * 再交给 br_json_str_array 解析。
 */
bool br_json_get_raw(const char *obj, const char *key, char *out, size_t cap);

#ifdef __cplusplus
}
#endif
