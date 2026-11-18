/*
 * br_hidmap - 从 HID Report Map 描述符解析每个 report 的角色
 * ---------------------------------------------------------------------------
 * 为什么要解析而不是写死 report id：
 *   这只遥控器的 9 个 report 特征里，report id 并不是固定的语义
 *   （不同固件版本可能变），而且有一堆是数据通道（0xFC 之类），
 *   误当成按键订阅会直接断链。
 *
 *   CMCC 的做法（已实测）：读特征里的 Report Reference 描述符拿到
 *   Report Map，按 HID 规范解析 Short Items，得出每个 report id 的：
 *     - Usage Page（全局项 tag 0x00）
 *     - Usage（集合的第一个 Usage，局部项 tag 0x00）
 *     - 位数（Report Size 0x07 × Report Count 0x09）
 *   再据此判定角色：
 *     - Usage Page 0x01(Generic Desktop) + Usage 0x06(Keyboard) -> 键盘
 *     - Usage Page 0x0C(Consumer)                                -> 消费者控制
 *       （位数为 2 字节时是 16 位 usage 的消费者键）
 *     - 其余                                                        -> 数据通道，不订阅
 *
 * 许可说明：本文件是本项目自己实现的解析器（HID 规范是公开标准），
 * 思路参考了 CMCC 的实现但代码独立编写。
 */
#include "br_hidmap.h"

#include <string.h>

/* HID 报告项前缀（bTag<<4 | bType<<2 | bSize） */
#define ITEM(tag, type, size) ((uint8_t)(((tag) << 4) | ((type) << 2) | (size)))
/*
 * ★★★ Item 类型（bType，prefix 的 bits 2-3）★★★（2026-11-04 修正）
 *
 * HID 规范（HID 1.11, Appendix E — Short Item Format）：
 *     bit 0-1  = bSize
 *     bit 2-3  = bType   0=Main  1=Global  2=Local
 *     bit 4-7  = bTag
 *
 * ★ 之前这里写成 GLOBAL=0 / LOCAL=1 / MAIN=2 —— **三个全错位了**。
 *   后果：所有 Item 都被判成错误的类型，
 *   Report Map 一个 report 都解析不出来：
 *       report map parsed: 214 bytes, 0 reports
 *       report 0x00 -> role=0 bytes=0     ← ×7
 *
 *   正确值（用真实 Report Map 验证）：
 *       05 01  → bType=1 Global, bTag=0  = Usage Page(Generic Desktop)
 *       09 06  → bType=2 Local,  bTag=0  = Usage(Keyboard)
 *       A1 01  → bType=0 Main,   bTag=0xA= Collection(Application)
 *       85 01  → bType=1 Global, bTag=8  = Report ID(1)
 */
#define ITEM_TYPE_MAIN   0
#define ITEM_TYPE_GLOBAL 1
#define ITEM_TYPE_LOCAL  2

#define TAG_USAGE_PAGE   0x00
#define TAG_REPORT_SIZE  0x07
#define TAG_REPORT_ID    0x08
#define TAG_REPORT_COUNT 0x09
#define TAG_USAGE        0x00
#define TAG_INPUT        0x08
#define TAG_COLLECTION   0x0A
#define TAG_END_COLL     0x0C

#define PAGE_GENERIC_DESKTOP 0x01
#define PAGE_CONSUMER        0x0C
#define USAGE_KEYBOARD       0x06
#define USAGE_GAMEPAD        0x05
#define USAGE_MULTI_AXIS     0x08
#define USAGE_BUTTON         0x09

void br_hidmap_reset(br_hidmap_t *map)
{
    if (!map) {
        return;
    }
    memset(map, 0, sizeof(*map));
}

static void map_entry(br_hidmap_t *map, uint8_t report_id)
{
    if (report_id < BR_HIDMAP_MAX_REPORTS) {
        map->entries[report_id].seen = true;
    }
}

/**
 * Collection 作用域栈。
 *
 * ★ HID 规范：Usage Page 是**作用域状态** ——
 *   在 Collection 内部出现的 Usage Page 只作用于该作用域，
 *   出了作用域要恢复外层的值。
 *
 *   实测（Google Chromecast Remote）：
 *     顶层 Collection  page=0x0001 (Keyboard)
 *     内层 Collection  page=0x0007 / 0x0009 / 0xFF01 ...
 *     每个 report 属于**它所在的那一层**。
 *
 *   不区分作用域就会把厂商页（0xFF01，语音通道）算成键盘页。
 */
#define HIDMAP_MAX_DEPTH 8

typedef struct {
    uint16_t page;
    uint8_t  usage;
} hidmap_scope_t;

/**
 * 解析 Report Map。
 *
 * report_map: 描述符字节
 * len:        长度
 * map:        输出
 *
 * 返回识别出的有效 report 数量。
 */
int br_hidmap_parse(br_hidmap_t *map, const uint8_t *report_map, uint16_t len)
{
    if (!map || !report_map || len == 0) {
        return 0;
    }
    br_hidmap_reset(map);

    uint16_t i = 0;
    uint16_t page = 0;
    uint8_t report_size = 0;
    uint8_t report_id = 0;
    uint8_t report_count = 0;
    uint8_t last_usage = 0;
    uint8_t depth = 0;

    /* Collection 作用域栈 */
    hidmap_scope_t s_coll_stack[HIDMAP_MAX_DEPTH];
    memset(s_coll_stack, 0, sizeof(s_coll_stack));

    while (i < len) {
        uint8_t prefix = report_map[i];
        uint8_t b_size = prefix & 0x03;
        uint8_t b_type = (prefix >> 2) & 0x03;
        uint8_t b_tag = (prefix >> 4) & 0x0F;

        uint32_t data = 0;
        uint16_t nb = 0;

        /*
         * ★★★ Main 类型的 Item 永远是「零数据字节」★★★（2026-11-04 修正）
         *
         * HID 规范（HID 1.11, 6.2.2 —— Main Items）：
         *     Input / Output / Feature / Collection / End Collection
         *     这些 Main Item **没有数据字段**，bSize 对它们无效。
         *
         * ★ 之前这里对所有 Item 都按 bSize 读数据 ——
         *   于是 `81 02`（Input）被当成「Input + 1 字节数据 0x02」，
         *   **整个字节流错位**！
         *
         * 后果（实测 Google Chromecast Remote）：
         *     Report Size / Report Count 从错误位置读出来
         *     -> bits 算成 64（实际应该是 24）
         *     -> Usage Page / Usage 也读错位置
         *     -> page=0x00 usage=0x00
         *     -> 角色判定全失败
         *
         * 只有 Global 和 Local 类型的 Item 才带数据。
         */
        if (b_type == ITEM_TYPE_MAIN) {
            nb = 0;   /* Main Item 无数据 */
        } else if (b_size == 3) {
            /* 4 字节项：前缀后跟 4 字节 */
            nb = 4;
            if (i + 1 + nb > len) {
                break;
            }
            data = (uint32_t)report_map[i + 1] |
                   ((uint32_t)report_map[i + 2] << 8) |
                   ((uint32_t)report_map[i + 3] << 16) |
                   ((uint32_t)report_map[i + 4] << 24);
        } else if (b_size == 2) {
            nb = 2;
            if (i + 1 + nb > len) {
                break;
            }
            data = (uint32_t)report_map[i + 1] | ((uint32_t)report_map[i + 2] << 8);
        } else if (b_size == 1) {
            nb = 1;
            if (i + 1 + nb > len) {
                break;
            }
            data = report_map[i + 1];
        } else {
            nb = 0;
        }

        if (b_type == ITEM_TYPE_GLOBAL) {
            switch (b_tag) {
            case TAG_USAGE_PAGE:
                /*
                 * ★ 保留 16 位 ★
                 * 厂商自定义页是 0xFF01 / 0xFF80 —— 超过 uint8_t 范围，
                 * 截断成 8 位就认不出厂商通道了（语音数据就在那）。
                 */
                page = (uint16_t)(data & 0xFFFF);
                break;
            case TAG_REPORT_SIZE:
                report_size = (uint8_t)data;
                break;
            case TAG_REPORT_ID:
                report_id = (uint8_t)data;
                break;
            case TAG_REPORT_COUNT:
                report_count = (uint8_t)data;
                break;
            default:
                break;
            }
        } else if (b_type == ITEM_TYPE_LOCAL) {
            if (b_tag == TAG_USAGE) {
                last_usage = (uint8_t)data;
                /*
                 * ★ Usage 也会更新当前作用域的 Collection ★
                 * 实测：鼠标 Collection 的 Usage=0x02 出现在它的
                 * Input 之后 —— 不更新就认不出鼠标 report。
                 */
                if (depth > 0 && depth <= HIDMAP_MAX_DEPTH) {
                    s_coll_stack[depth - 1].usage = (uint8_t)data;
                }
            }
        } else {  /* MAIN */
            if (b_tag == TAG_INPUT) {
                map_entry(map, report_id);
                if (report_id < BR_HIDMAP_MAX_REPORTS) {
                    br_hidmap_entry_t *e = &map->entries[report_id];
                    if (!e->seen) {
                        e->seen = true;
                        /*
                         * ★★★ Usage Page 的正确语义（2026-11-04 修正）★★★
                         *
                         * HID 规范里，Usage Page 是**作用域状态**：
                         *   - 它可以出现在 Collection **内部**
                         *   - 只影响它**所在作用域**内的 Input 项
                         *   - 出了那个作用域要恢复外层的值
                         *
                         * 实测（Google Chromecast Remote 的 Report Map）：
                         *     字节 4:   A1 Collection (Keyboard, page=0x0001)
                         *     字节 22:  81 Input  -> Keyboard ✓
                         *     字节 12:  05 07    -> Usage Page = 0x0007（仅作用于当前作用域）
                         *     字节 166: 06 01    -> Usage Page = 0xFF01
                         *     字节 185: 81 Input  -> 厂商页 ✓
                         *
                         * ★ 之前的实现只认「顶层 Collection 的 page」，
                         *   所以 report 30（0xFF01 厂商页）的 page 被算成 0x0001，
                         *   语音通道就认不出来了。
                         *
                         * ★ 也不能简单地「用当前 page」——
                         *   因为 page 是全局变量，进入子作用域后又被内层的
                         *   Usage Page 改掉了（0x0007 / 0x0009 那些）。
                         *
                         * 修法：用**栈**保存每层 Collection 自己的 page/usage。
                         */
                        if (depth > 0 && s_coll_stack[depth - 1].page != 0) {
                            e->page = s_coll_stack[depth - 1].page;
                            /*
                             * ★ Collection 的 Usage 可能出现在它的
                             *   **第一个 Input 之后**（实测：鼠标 Collection
                             *   的 Usage=0x02 在字节 90，而它的 Input 在 108）。
                             *   所以栈里的 usage 要**持续更新**，
                             *   不能只在进 Collection 时拍一次快照。
                             */
                            e->usage = s_coll_stack[depth - 1].usage;
                        } else {
                            e->page = page;
                            e->usage = last_usage;
                        }
                    }
                    /*
                     * ★ Report Size 是**位数**，不是字节数 ★
                     *
                     * HID 规范：Report Size 描述每个 report item 占多少 **bit**。
                     * 所以 report 的字节数 = ceil(bits / 8)。
                     *
                     * 实测（Chromecast Remote）：
                     *   report 2（消费者）：ReportSize=16 bit -> 2 字节 ✓
                     *   report 1（键盘）  ：ReportSize=8 bit  -> 1 字节/项 × 8 项
                     *
                     * ★ 关键：同一个 report 里可能有**多个 Input 项**，
                     *   每个的 Report Size 不同（消费者报告有「按下位」和
                     *   「usage 值」两种布局）。
                     *   报告长度取决于**最大的那个**，不是累加 ——
                     *   累加会算出 24 bit = 3 字节，而实际是 2 字节。
                     */
                    uint16_t item_bits = (uint16_t)report_size * report_count;
                    if (item_bits > e->bits) {
                        e->bits = item_bits;
                    }
                }
                last_usage = 0;
            } else if (b_tag == TAG_COLLECTION) {
                /*
                 * ★ 入栈：记下这一层 Collection 自己的 page/usage ★
                 *
                 * Usage Page 可能是**内层**才出现的
                 * （实测：厂商页 0xFF01 就出现在 Collection 内部），
                 * 所以每层都要单独记。
                 */
                if (depth < HIDMAP_MAX_DEPTH) {
                    s_coll_stack[depth].page = page;
                    s_coll_stack[depth].usage = last_usage;
                }
                depth++;
                last_usage = 0;
            } else if (b_tag == TAG_END_COLL) {
                if (depth > 0) {
                    depth--;
                }
                last_usage = 0;
            } else {
                /* Output / Feature 等：清掉 pending usage */
                last_usage = 0;
            }
        }

        i += (uint16_t)(1 + nb);
    }

    int found = 0;
    for (int r = 0; r < BR_HIDMAP_MAX_REPORTS; r++) {
        if (map->entries[r].seen && map->entries[r].bits > 0) {
            found++;
        }
    }
    return found;
}

/**
 * 判定某个 report id 的角色。
 *
 * 返回 BR_ROLE_* 之一，并把该 report 的字节数写进 *bytes。
 */
uint8_t br_hidmap_role(const br_hidmap_t *map, uint8_t report_id,
                       uint16_t *bytes)
{
    if (bytes) {
        *bytes = 0;
    }
    if (!map || report_id >= BR_HIDMAP_MAX_REPORTS) {
        return BR_ROLE_NONE;
    }
    const br_hidmap_entry_t *e = &map->entries[report_id];
    if (!e->seen || e->bits == 0) {
        return BR_ROLE_NONE;
    }

    uint16_t b = (uint16_t)((e->bits + 7) / 8);
    if (bytes) {
        *bytes = b;
    }

    if (e->page == PAGE_GENERIC_DESKTOP && e->usage == USAGE_KEYBOARD) {
        return BR_ROLE_KEYBOARD;
    }
    if (e->page == PAGE_CONSUMER) {
        /* 消费者控制：2 字节 = 16 位 usage；更多字节是数据通道 */
        return (b == 2) ? BR_ROLE_CONSUMER : BR_ROLE_DATA;
    }

    /*
     * ★ 游戏手柄 / 多轴控制器 ★
     *
     * Google Chromecast Remote 的 Appearance 是 0x03C3 = **HID Gamepad**，
     * 它的按键报告走 Generic Desktop 页的 Gamepad 集合 ——
     * 之前这里只认 Keyboard 和 Consumer，Gamepad 一律返回 BR_ROLE_NONE，
     * 结果**所有按键被静默丢弃**：
     *   - RGB 灯不闪（用户看不到任何反馈）
     *   - 学习模式永远学不到键
     *
     * 判据：Generic Desktop 页里 usage 是 Gamepad(0x05) / MultiAxis(0x08)
     * 或 Button(0x09) 的，都按「按键位图」处理。
     */
    if (e->page == PAGE_GENERIC_DESKTOP &&
        (e->usage == USAGE_GAMEPAD || e->usage == USAGE_MULTI_AXIS ||
         e->usage == USAGE_BUTTON)) {
        return BR_ROLE_GAMEPAD;
    }

    /*
     * ★ 厂商自定义页（0xFF01 / 0xFF80 等）★
     *
     * 实测（Google Chromecast Remote 的 Report Map 解码）：
     *     INPUT id=30 page=0xFF01 usage=0x1 rsize=8  rcount=20
     *     INPUT id=5  page=0xFF80 usage=0x0 rsize=8  rcount=20
     *
     * 这两个是 20 字节的厂商自定义通道 —— **语音数据就在这里**。
     *
     * ★ 之前的实现把语音通道硬编码成 `report_id == 0xFC`，
     *   而这只遥控器的语音在 report 30 / 5（厂商页）——
     *   所以「按语音键不闪灯、ASR 也拿不到音频」。
     *
     * 正确做法：**按 Usage Page 判断**，不写死 report id。
     * 这样任何用厂商自定义页传数据的遥控器都能识别。
     */
    if (e->page >= 0xFF00) {
        return BR_ROLE_VENDOR;
    }

    return BR_ROLE_NONE;
}
