/*
 * br_proto - SP-v2 行协议（固件侧）
 * ---------------------------------------------------------------------------
 * 设计目标：消灭老协议的歧义。
 *
 * 老协议的问题：
 *   - "A " 既是广播 dump 又是语音音频帧
 *   - "R " 既是 REMOTES 列表行又是连接结果
 *   - 名字里有空格就解析错位
 *
 * SP-v2 用 6 个保留 token 把所有上行行分成互斥的类别：
 *
 *   OK <VERB> [k=v ...]      命令成功
 *   ER <VERB> <reason> [k=v] 命令失败
 *   EV <EVENT> [k=v ...]     异步事件
 *   DT <KIND> [k=v ...]      数据行（列表项）
 *   LG level=... msg=...     结构化日志
 *   # <free text>            调试文本
 *
 * 值转义：空格 %20、% %25、= %3D、换行 %0A、回车 %0D，其余 UTF-8 原样。
 * 转义后值里不含空格与 =，解析时按空格切 token、按第一个 = 切键值。
 *
 * 顺序保证：任何列表型命令的所有 DT 行必须先于该命令的 OK 行到达。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include "br_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ KV 构建 */
/*
 * 行 = "TOKEN arg1 arg2 ..."，每个 arg 形如 key=value。
 * 用 br_kv_t 逐个拼，避免 printf 格式化与转义互相打架。
 */
typedef struct {
    char  buf[BR_KV_MAX];
    size_t len;
    bool  overflow;
} br_kv_t;

void br_kv_init(br_kv_t *kv);
/** 加一个字符串值（自动转义） */
void br_kv_add(br_kv_t *kv, const char *key, const char *value);
/** 加一个整数值 */
void br_kv_add_int(br_kv_t *kv, const char *key, long value);
/** 加一个十六进制字节串（值形如 hex=0A0B0C） */
void br_kv_add_hex(br_kv_t *kv, const char *key, const uint8_t *data, size_t len);
/** 加一个原始标记（形如 flag，无值） */
void br_kv_add_flag(br_kv_t *kv, const char *key);

/* ------------------------------------------------------------------ 发送 */
typedef void (*br_proto_write_fn)(const char *data, size_t len);

void br_proto_init(br_proto_write_fn write_fn);

void br_proto_send_ok(const char *verb, const br_kv_t *kv);
void br_proto_send_err(const char *verb, const char *reason, const br_kv_t *kv);
void br_proto_send_ev(const char *event, const br_kv_t *kv);
void br_proto_send_dt(const char *kind, const br_kv_t *kv);
void br_proto_send_lg(const char *level, const char *msg);
/** 调试文本行（以 # 开头） */
void br_proto_send_raw(const char *text);

/* ------------------------------------------------------------------ 解析 */
#define BR_MAX_ARGS 16

typedef struct {
    char *argv[BR_MAX_ARGS];
    int   argc;
    char  storage[BR_LINE_MAX];
} br_parsed_t;

/**
 * 解析一行命令。
 * 原地修改 line（token 之间用 '\0' 隔开），token 指针指向 line 内部。
 * 返回 token 数；0 表示空行。
 */
int br_proto_parse(char *line, br_parsed_t *out);

/**
 * 取第 i 个 token 的整数值。失败返回 false。
 */
bool br_proto_arg_int(const br_parsed_t *p, int i, long *out);

/** 工具：把值原地反转义（用于需要还原字符串的场景） */
void br_proto_unescape(char *dst, size_t cap, const char *src);

#ifdef __cplusplus
}
#endif
