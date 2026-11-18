/*
 * br_types - 固件与 PC 客户端之间的数据契约
 * ---------------------------------------------------------------------------
 * 这里定义的东西是**跨语言契约**：固件用 C 写，客户端用 Python 写，
 * 两边都按这里的编码规则解释同一批字节。任何改动必须两边同步。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------- 容量上限 */
#define BR_MAX_PEERS        8      /* 同时可连接的遥控器数 */
#define BR_MAX_CANDIDATES   16     /* 一轮扫描保留的候选数 */
#define BR_MAX_NAME_LEN     40     /* 遥控器名字最大长度（含结尾 0） */
#define BR_MAX_BINDS        64     /* 单台遥控器最多 64 条映射 */
#define BR_MAX_KEYS         8      /* 一条映射最多 8 个键（6 键 + 修饰） */
#define BR_MAX_IMG_PATH     128    /* 平面图路径（仅 PC 侧用，固件不存） */

/* ------------------------------------------------------------ 按键 id 编码 */
/*
 * key_id = (source << 12) | usage
 *   source: 1=键盘报告 2=消费者报告 3=数据/厂商报告 4=ATVV 控制
 *   usage: HID usage，低 12 位
 * 这样同一个 usage 出现在不同报告类型下不会互相覆盖。
 */
#define BR_KEY_MAKE(src, usage) \
    ((uint32_t)(((uint32_t)(src) << 12) | ((uint32_t)(usage) & 0x0FFFu)))

#define BR_KEY_SRC(id)     ((uint8_t)(((id) >> 12) & 0x0Fu))
#define BR_KEY_USAGE(id)   ((uint16_t)((id) & 0x0FFFu))

#define BR_SRC_KEYBOARD    1u
#define BR_SRC_CONSUMER    2u
#define BR_SRC_DATA        3u
#define BR_SRC_VENDOR      4u
#define BR_SRC_ATVV        5u

/* ------------------------------------------------------------ 触发类型 */
/*
 * 与 PC 侧 key_mapping 的 trigger 编号保持一致。
 */
#define BR_TRIG_SINGLE     0u
#define BR_TRIG_DOUBLE     1u
#define BR_TRIG_LONG       2u
#define BR_TRIG_COUNT      3u

/* ------------------------------------------------------------ 输出类型 */
#define BR_OUT_NONE        0u
#define BR_OUT_KEYBOARD    1u
#define BR_OUT_CONSUMER    2u

/* ------------------------------------------------------------------ 映射 */
typedef struct {
    uint32_t key_id;       /* BR_KEY_MAKE(src, usage) */
    uint8_t  trig;         /* BR_TRIG_* */
    uint8_t  out;          /* BR_OUT_* */
    uint8_t  mods;         /* USB HID 修饰键位掩码 */
    uint8_t  nkeys;        /* 实际键数 */
    uint8_t  keys[BR_MAX_KEYS];
} br_bind_t;

/* ---------------------------------------------------------------- 遥控器 */
typedef struct {
    bool     in_use;
    uint8_t  idx;
    uint8_t  addr_type;    /* 0=PUBLIC 1=RANDOM */
    uint8_t  addr[6];
    char     name[BR_MAX_NAME_LEN];
    uint16_t bind_count;
    br_bind_t binds[BR_MAX_BINDS];
} br_remote_t;

/* ------------------------------------------------------------ 协议常量 */
#define BR_LINE_MAX         512    /* 收行缓冲 */
#define BR_TX_BUF_MAX       2048   /* 单次 CDC 写入上限 */

/*
 * ★ KV 缓冲必须小 ★
 *   br_kv_t 是**栈上局部变量**，出现在很多函数里。
 *   一旦开太大（比如 2048），一个函数里用两三个就爆栈 ——
 *   实测踩过：cdc 任务 4096 字节栈直接 "stack overflow in task cdc"。
 *   512 足够装下最长的一行（DT AU 带 64 字节 hex = 128 个 hex 字符 + 若干键值）。
 */
#define BR_KV_MAX           512

#ifdef __cplusplus
}
#endif
