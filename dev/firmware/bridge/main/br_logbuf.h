/*
 * br_logbuf - 板端滚动日志缓冲
 * ---------------------------------------------------------------------------
 * 为什么要它：
 *   ESP_LOG 的输出走 UART0（CH343），而**协议应答只走 USB CDC**。
 *   PC 用 CDC 连上开发板时，板端日志一行都看不到 —— 现场出问题完全瞎。
 *   所以板子上必须自己留一份滚动日志，PC 随时能拉走。
 *
 * 设计取舍：
 *   1. **纯 RAM 环形缓冲**，完全不写 flash。
 *      → 零 flash 占用；断电重插自然从空开始重新记录，正好符合
 *        「断电重插后重新记录」的需求。
 *   2. 容量固定 256 行 / 约 30 KB。满了覆盖最旧的。
 *   3. 实时推送**不在** vprintf 钩子里做 —— 钩子可能跑在任何任务的栈上，
 *      在那里拼协议行会踩栈。钩子只负责入环，实时推送由独立任务做。
 *   4. 实时推送限流 20 行/秒，防止日志量把 USB CDC 冲爆。
 *
 * 对外协议（见 app_main.c）：
 *   LOGREAD [n]   回传最近 n 行（DT LOG ...），上限 256
 *   LOGLIVE 0|1   开关实时推送（新行以 LG 行下发）
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

/* 每行的字段上限（含结尾 0） */
#define BR_LOGBUF_LEVEL_MAX 6
#define BR_LOGBUF_TAG_MAX   16
#define BR_LOGBUF_MSG_MAX   96

/* 环形缓冲行数 */
#define BR_LOGBUF_LINES     256

/**
 * 实时推送回调。level 是 "info"/"warn"/"error"/"debug" 之类的小写词。
 */
typedef void (*br_logbuf_sink_fn)(const char *level, const char *tag,
                                  const char *msg);

/** 初始化（内部会起实时推送任务） */
void br_logbuf_init(void);

/** 追加一行日志（内部会截断到字段上限） */
void br_logbuf_append(const char *level, const char *tag, const char *msg);

/** 当前保留的行数 */
int br_logbuf_count(void);

/**
 * 取第 index 行（0 = 最旧的一行）。
 * 成功返回写入长度，失败返回 -1。
 */
int br_logbuf_get(int index, char *level, size_t level_cap,
                  char *tag, size_t tag_cap,
                  char *msg, size_t msg_cap);

/** 开关实时推送 */
void br_logbuf_set_live(bool on);
bool br_logbuf_live(void);

/** 注册实时推送回调 */
void br_logbuf_set_sink(br_logbuf_sink_fn fn);
