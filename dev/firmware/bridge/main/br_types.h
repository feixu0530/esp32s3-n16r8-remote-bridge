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
#define BR_OUT_MOUSE       3u

/* ------------------------------------------------------------------ 手机端 */
/*
 * 「手机」作为一个虚拟遥控器的契约。
 *
 * 手机没有 BLE 地址，给它一个固定伪地址 ——
 * NVS 里按这个地址存映射表（br_mapping.c 的 "m%02X..." 键），
 * 和真实遥控器天然分开，不会互相覆盖。
 */
#define BR_PHONE_ADDR {0x50, 0x50, 0x50, 0x50, 0x50, 0x50}

/*
 * 手机事件的 source（占用 key_id 的高 4 bit）。
 * 与 1键盘/2消费者/3数据/4厂商/5ATVV 并列，
 * ⇒ (6<<12)|x 与 (2<<12)|x 永不相等，映射表物理隔离。
 */
#define BR_SRC_PHONE        6u

/*
 * 手机语义键 → HID usage。
 *
 * 名字与 movo 前端保持一致（esc / volume_up / enter / backspace…），
 * PC 端 core/remote_layout.py 里有一份同样的表（两边必须一致）。
 * 键盘页发的键名来自 movo 的键盘布局，快捷键栏发的是
 * esc / volumedown / volumeup。
 */
#define BR_PKU_A        0x04
#define BR_PKU_Z        0x1D
#define BR_PKU_1        0x1E
#define BR_PKU_0        0x27
#define BR_PKU_ENTER    0x28
#define BR_PKU_ESC      0x29
#define BR_PKU_BACKSPACE 0x2A
#define BR_PKU_TAB      0x2B
#define BR_PKU_SPACE    0x2C
#define BR_PKU_MINUS    0x2D
#define BR_PKU_EQUAL    0x2E
#define BR_PKU_F1       0x3A
#define BR_PKU_F12      0x45
#define BR_PKU_PRINTSCREEN 0x46
#define BR_PKU_DELETE   0x4C
#define BR_PKU_RIGHT    0x4F
#define BR_PKU_LEFT     0x50
#define BR_PKU_DOWN     0x51
#define BR_PKU_UP       0x52

/* 消费者页（16 位 usage） */
#define BR_PKU_VOLUME_UP   0xE9
#define BR_PKU_VOLUME_DOWN 0xEA
#define BR_PKU_MUTE        0xE2
#define BR_PKU_PLAY_PAUSE  0xCD
#define BR_PKU_NEXT        0xB5
#define BR_PKU_PREV        0xB6

/*
 * 手机「快捷键栏」的语义键 → 虚拟 usage。
 *
 * ★ 这一组才是「像遥控器一样能配映射」的键 ★
 *   触控板页的快捷键栏（movo index.html 的 quickbar）发的是
 *   esc / volumeup / volumedown —— 语义上等同遥控器上的实体键，
 *   所以走映射表（key_id = (BR_SRC_PHONE<<12)|usage），
 *   用户可以在 PC 端给它们配任意动作。
 *
 *   键盘页敲的字（a~z、F1、回车…）是**字面输入**，
 *   直接发 HID 键盘报告，不进映射表 —— 否则「按 a 打出 a」这件事
 *   也要用户先配一遍映射，没道理。
 */
#define BR_PHU_ESC         0x01
#define BR_PHU_VOLUME_UP   0x02
#define BR_PHU_VOLUME_DOWN 0x03
#define BR_PHU_MUTE        0x04
#define BR_PHU_PLAY_PAUSE  0x05
#define BR_PHU_SHORTCUT   0x06  /* 手机底部的自定义快捷键（动作在 PC 端配） */

/*
 * 手机「自定义快捷键页」的键：usage = BR_PHU_CUSTOM_BASE + id（id 1..12）。
 * 占用 0x101..0x10C，与上面 5 个固定键不冲突；key_id 仍按 12 位 usage 打包。
 */
#define BR_PHU_CUSTOM_BASE 0x100u
#define BR_PHU_CUSTOM_MAX  64u

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
