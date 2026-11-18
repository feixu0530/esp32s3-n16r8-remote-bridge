/*
 * br_ble_core - BLE 主机端（Central + Observer）
 *
 * ★ 适配 IDF v6.1 内置的 NimBLE 2.x API ★
 *   - UUID 用 union 型 ble_uuid_any_t（.u16 / .u32 / .u128）
 *   - ble_gattc_disc_all_svcs() 不再接受句柄范围
 *   - ble_gap_event.connect 里没有 peer_addr（我们自己记）
 *   - ble_gap_connect() 第一个参数是 **本机** 地址类型
 *
 * ★ 来自 CMCC 实测的硬约束（改这个文件前先读）★
 *
 *   1. 扫描回调里绝对不能连接、不能停扫描。
 *      在 GAP 上下文里停扫描会把协议栈状态机卡住，
 *      之后 esp_ble_gattc_open 要等将近半分钟才发得出去（实测 31 秒）。
 *      → 本实现：回调只记录候选，连接动作一律回到主循环任务里做。
 *
 *   2. 扫描与连接必须严格串行。
 *      并行会互相抢控制器：扫描启动 EBUSY(15)、连接 EBUSY(2)/EEXIST(21)。
 *
 *   3. 用扫描时拿到的对象去连接，地址类型必然真实。
 *      配置里存的 addr_type 不可信（遥控器可能用随机地址）。
 */
#include "br_ble_core.h"
#include "br_proto.h"
#include "br_hid.h"
#include "br_mapping.h"
#include "br_usb.h"
#include "br_voice.h"
#include "br_led.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#include <esp_log.h>
#include <esp_mac.h>
#include <esp_random.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <nvs.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>

#include <host/ble_hs.h>
#include <host/ble_gap.h>
#include <host/ble_gatt.h>
#include <host/ble_store.h>
#include <host/ble_uuid.h>
#include <nimble/ble.h>
#include <os/os_mbuf.h>
#include <nimble/nimble_port.h>
#include <nimble/nimble_port_freertos.h>

static const char *TAG = "br_ble";

/* ------------------------------------------------------------------ UUID */
#define UUID_CCCC       0x2902   /* Client Characteristic Configuration */
#define UUID_REPORT_REF 0x2908   /* HID Report Reference（report id + 类型） */

/*
 * ATVV（Android TV Voice）—— Chromecast / 小米语音遥控器共用。
 *
 * 存的是**规范顺序**（跟打印出来的一致，大端），
 * 与 NimBLE 内部的 u128.value（线序，小端）互为逆序。
 */
static const uint8_t k_atvv_service_canon[16] = {
    0xAB, 0x5E, 0x00, 0x01, 0x5A, 0x21, 0x4F, 0x05,
    0xBC, 0x7D, 0xAF, 0x01, 0xF6, 0x17, 0xB6, 0x64
};
static const uint8_t k_atvv_tx_canon[16] = {
    0xAB, 0x5E, 0x00, 0x02, 0x5A, 0x21, 0x4F, 0x05,
    0xBC, 0x7D, 0xAF, 0x01, 0xF6, 0x17, 0xB6, 0x64
};
static const uint8_t k_atvv_audio_canon[16] = {
    0xAB, 0x5E, 0x00, 0x03, 0x5A, 0x21, 0x4F, 0x05,
    0xBC, 0x7D, 0xAF, 0x01, 0xF6, 0x17, 0xB6, 0x64
};
static const uint8_t k_atvv_ctl_canon[16] = {
    0xAB, 0x5E, 0x00, 0x04, 0x5A, 0x21, 0x4F, 0x05,
    0xBC, 0x7D, 0xAF, 0x01, 0xF6, 0x17, 0xB6, 0x64
};

/* ------------------------------------------------------------- 扫描参数 */
#define SCAN_ROUND_MS      1000    /* 一轮扫描时长 */
#define SCAN_COOLDOWN_MS   300     /* 扫空后的冷却 —— 遥控器广播窗口很短，必须几乎连续扫 */
#define CONNECT_SETTLE_MS   120    /* 停扫描到发起连接的间隔 */
/*
 * ★ 连接超时：1500ms ★
 *
 * 之前是 5000ms。问题在于**低功耗遥控器**：
 * 它只在被按键唤醒的那一两秒里广播，地址还是会轮换的 RPA。
 * 我们连不上旧地址 → 撞满 5 秒超时 → 这 5 秒里我们**根本没在扫描**
 * → 遥控器那几秒的广播窗口就这么被错过了
 * （实测：10 分钟 115 次全超时，一次没抓住）。
 *
 * 1.5 秒足够「对端醒着」时连上；
 * 连不上就早点把射频还给扫描，扫描命中率才是关键。
 */
#define CONNECT_TIMEOUT_MS 1500

/*
 * ★ 发起连接时本机用哪个地址类型 ★
 *
 * 单独提成变量（而不是写死 BLE_OWN_ADDR_PUBLIC），有两个原因：
 *   1) 要能把它**当数字打进日志**。日志里写死 "own=PUBLIC" 是自述，
 *      跟 NimBLE 实际用的地址对不上时无从发现（这次就在日志里绕了一圈）。
 *   2) 这块板子的公版地址不可用（见 setup_own_identity_addr），
 *      主机同步后我们会装一个固定的 random static 身份，
 *      届时必须改成 BLE_OWN_ADDR_RANDOM 才会真的用上它。
 */
static uint8_t s_own_addr_type = BLE_OWN_ADDR_PUBLIC;
#define OWN_ADDR_TYPE_FOR_CONNECT  s_own_addr_type

#define MAX_CHRS_PER_PEER  40
#define MAX_SVCS_PER_PEER  12

/* ------------------------------------------------------------------ 类型 */

/** 规范化后的 UUID（规范顺序 = 打印顺序） */
typedef struct {
    uint8_t type;      /* 16 / 32 / 128 */
    uint8_t bytes[16];
} br_uuid_t;

typedef struct {
    bool      used;
    uint8_t   addr_type;
    uint8_t   addr[6];
    char      name[BR_MAX_NAME_LEN];
    /*
     * ★ 名字是否真的拿到了 ★
     *   不能用 name[0] 判断 —— 早期版本给无名的设备填了 "HID_Device"
     *   占位名，于是「名字为空」这个条件永远不成立，
     *   SCAN_RSP 里带来的真名再也填不进去（UI 上全是 HID_Device）。
     */
    bool      name_known;
    uint8_t   adv_type;       /* 广播类型：0=可连接 1=定向 3=不可连接 4=SCAN_RSP */
    int8_t    rssi;
    int       score;
    char      why[32];
} br_candidate_t;

typedef struct {
    uint16_t def_handle;
    uint16_t val_handle;
    uint8_t  props;
    uint8_t  uuid_type;
    uint8_t  uuid[16];
    uint16_t cccd_handle;   /* 0 = 没有 CCCD */
    uint16_t ref_handle;    /* Report Reference 描述符，0 = 没有 */
} br_chr_t;

typedef struct {
    uint16_t start_handle;
    uint16_t end_handle;
    uint8_t  uuid_type;
    uint8_t  uuid[16];
} br_svc_t;

typedef enum {
    BR_DISC_NONE = 0,
    BR_DISC_NEXT_SVC,   /* ★ 待发起：全量服务发现 */
    BR_DISC_SVC,        /* 正在发现服务 */
    BR_DISC_NEXT_CHR,   /* ★ 待发起：下一个服务的特征发现 */
    BR_DISC_CHR,        /* 正在发现特征 */
    BR_DISC_NEXT_DSC,   /* ★ 待发起：当前服务的描述符发现 */
    BR_DISC_DSC,        /* 正在发现描述符 */
    BR_DISC_DONE,
} br_disc_state_t;

typedef struct {
    bool     used;
    uint8_t  idx;
    bool     connected;
    uint16_t conn_handle;
    uint8_t  addr_type;
    uint8_t  addr[6];
    /*
     * ★ identity 地址（固定的那串 MAC）★
     *   广播地址是 BLE 私有地址，**会轮换**；identity 不会。
     *   NimBLE 的 bond 表里存的也是 identity ——
     *   认人、删 bond 都得用它，拿广播地址去比对永远对不上。
     */
    uint8_t  id_addr[6];
    uint8_t  id_type;
    bool     has_id;
    /*
     * ★ 规范映射地址 ★
     *   PC 端写映射用的是配对表里那个地址（DT RM 的 addr=），
     *   按键查表用的是本次连接的广播地址 —— BLE 私有地址会轮换，
     *   两者一错开就出现「板子上显示有 N 条，按下去没反应」。
     *   这里统一成配对表里的地址（查不到就退回本次连接的地址）。
     */
    uint8_t  map_addr[6];
    bool     map_addr_valid;
    int64_t  last_seen_us;    /* LRU：槽位不够时先淘汰最久没动静的那个 */
    char     name[BR_MAX_NAME_LEN];

    br_disc_state_t disc_state;
    int      svc_count;
    br_svc_t svcs[MAX_SVCS_PER_PEER];
    int      chr_count;
    br_chr_t chrs[MAX_CHRS_PER_PEER];
    int      cur_svc;
    bool     atvv_seen;

    uint16_t atvv_audio_handle;
    uint16_t atvv_ctl_handle;
    uint16_t atvv_tx_handle;
    uint16_t atvv_audio_cccd;   /* ATVV 音频特征的 CCCD */
    uint16_t atvv_ctl_cccd;      /* ATVV 控制特征的 CCCD */
    uint8_t  atvv_sub_step;     /* 0=没写 1=写音频 2=写控制 3=完成 */
    bool     atvv_subscribed;

    /*
     * 加密协商状态
     *   HID over GATT 的 Report 特征默认要求加密链路，
     *   不加密的话 CCCD 写得进去、通知却一个都不会来 —— 见 ensure_encrypted()。
     */
    bool     sec_attempted;
    bool     encrypted;
    bool     channels_open;
    bool     channels_enc;     /* 订阅时链路是不是加密的，用来判断要不要重订阅 */
    bool     params_pending;   /* 连上后要请求一次连接参数（超时要拉长） */
} br_peer_t;

/* ------------------------------------------------------------------ 全局 */
static br_candidate_t s_candidates[BR_MAX_CANDIDATES];
static int            s_candidate_count;
static br_peer_t      s_peers[BR_MAX_PEERS];
static int            s_peer_count;

static bool s_scanning;
static bool s_scan_continuous;
static bool s_scan_session_active = false;
static bool s_scan_round_done;
static bool s_connect_in_progress;
static int64_t s_next_pick = 0;   /* 下次挑目标的时刻 */
static int64_t s_next_blind = 0;  /* 下次盲连兜底的时刻 */
static int     s_bond_fail_streak = 0;  /* bond 连接连续失败次数 */
static int     s_type_flip = 0;         /* 地址类型逐次交替 */
static int64_t s_round_done_at;

static bool s_auto;
static bool s_auto_dump;
static char s_target_name[BR_MAX_NAME_LEN];

/* 扫描候选的协议上报进度（扫描回调只记录，主循环负责发协议） */
static int s_emit_upto;
static int s_emit_done;

/* ---------------------------------------------------------------- 待铺默认 */

/* NVS 配对表（定义在文件后面的「已配对遥控器表」一节）——
   下面铺默认时要用到它们，先前置声明。 */
static int  pl_find(const uint8_t *addr);
static int  pl_find_by_id(const uint8_t *id);
static bool pl_get_profile(int i, char *out, size_t cap);
static bool pl_set_profile(int i, const char *id);

/* 地址格式化（定义在下面，这里先声明） */
static void addr_to_str(const uint8_t *addr, char *out, size_t cap);

/*
 * ★ 铺出厂默认的排队区 ★
 *
 *   全新刷写的板子 NVS 是空的 —— 插上板子、遥控器配上对，
 *   如果一条映射都没有，按什么键都不动。
 *   所以「连上了 + 这个型号有默认档案 + 这台一条映射都没有」
 *   → 铺一套出厂默认，插上就能用，**不需要 PC 端在场**。
 *
 * ★ 为什么不在 GAP 回调里直接写 ★
 *   铺默认要写 NVS（flash 写，可能几十毫秒）。
 *   在 NimBLE 的连接回调里写，会把 host 任务卡住，
 *   遥控器判定拥塞就断链 —— 实测过，别在回调里做。
 *   所以这里只排队，由主循环（br_ble_task，每拍 30ms）去执行。
 */
#define DEF_QUEUE_MAX 4

typedef struct {
    int  peer_idx;
    char name[BR_MAX_NAME_LEN];
} def_pending_t;

static def_pending_t s_def_pending[DEF_QUEUE_MAX];
static int s_def_pending_count;

/** 排队等「铺出厂默认」。同一台重复排队只更新名字。 */
static void queue_defaults(int peer_idx, const char *name)
{
    if (peer_idx < 0 || peer_idx >= BR_MAX_PEERS) {
        return;
    }
    for (int i = 0; i < s_def_pending_count; i++) {
        if (s_def_pending[i].peer_idx == peer_idx) {
            snprintf(s_def_pending[i].name, sizeof(s_def_pending[i].name),
                     "%s", name ? name : "");
            return;
        }
    }
    if (s_def_pending_count >= DEF_QUEUE_MAX) {
        return;
    }
    s_def_pending[s_def_pending_count].peer_idx = peer_idx;
    snprintf(s_def_pending[s_def_pending_count].name,
             sizeof(s_def_pending[s_def_pending_count].name),
             "%s", name ? name : "");
    s_def_pending_count++;
    ESP_LOGI(TAG, "排队铺默认：peer=%d name='%s'", peer_idx, name ? name : "");
}

/**
 * 主循环里执行排队。
 * PC 正在写映射（暂存区忙）时**下一拍再试**，不放弃 ——
 * 否则「PC 正在给 A 写映射时 B 连上来」→ B 永远拿不到默认。
 */
static void run_pending_defaults(void)
{
    for (int i = 0; i < s_def_pending_count;) {
        def_pending_t *q = &s_def_pending[i];
        br_peer_t *p = &s_peers[q->peer_idx];
        if (!p->used || !p->connected) {
            /* 这台已经断了 —— 删掉；下次连上会重新排队 */
            s_def_pending[i] = s_def_pending[--s_def_pending_count];
            continue;
        }
        char addr_s[24];
        addr_to_str(p->map_addr_valid ? p->map_addr : p->addr,
                    addr_s, sizeof(addr_s));
        /*
         * ★ 档案按「型号 + MAC」定，并记在板子上 ★
         *   同一台重配 → 配对表里存着上次用的 id → 用回同一套；
         *   新的台 → 按名字匹配，匹配到的 id 写回配对表（下次重配就用它）。
         */
        char prof[16] = "";
        int slot = -1;
        if (p->has_id) {
            slot = pl_find_by_id(p->id_addr);
        }
        if (slot < 0) {
            slot = pl_find(p->map_addr_valid ? p->map_addr : p->addr);
        }
        if (slot >= 0) {
            pl_get_profile(slot, prof, sizeof(prof));
        }
        char matched[16] = "";
        int rc = br_mapping_apply_defaults(addr_s, q->name, prof, matched);
        if (rc == -1) {
            i++;                 /* PC 正在写 —— 下一拍再试 */
            continue;
        }
        if (matched[0] && slot >= 0 && prof[0] == '\0') {
            pl_set_profile(slot, matched);
            ESP_LOGI(TAG, "配对表[%d] 记下默认档案 %s", slot, matched);
        }
        s_def_pending[i] = s_def_pending[--s_def_pending_count];
    }
}

/*
 * ★ 通知数据队列 ★
 *
 * ATT 通知回调里**只做一次 memcpy**，协议组装全部交给主循环。
 * 理由（CMCC 实测）：回调里做任何阻塞动作都会拖慢 ATT 确认，
 * 遥控器会判定拥塞主动断链。
 */
#define AU_ITEM_MAX 160
#define AUQ_LEN     256

typedef struct {
    uint16_t attr;
    uint16_t len;
    uint8_t  data[AU_ITEM_MAX];
} au_item_t;

static QueueHandle_t s_auq = NULL;

/* 按键事件队列（ATT 回调只入队，协议组装在主循环里做） */
#define KEYQ_LEN 64
static QueueHandle_t s_keyq = NULL;

/* 学习模式：开启后按下的第一个键单独上报为 LEARN 事件 */
static bool s_learn_mode = false;
static int  s_learn_idx = -1;

/** HID 层的按键回调 */
static void on_hid_key(const br_hid_key_t *key, void *ctx)
{
    (void)ctx;
    if (!s_keyq) {
        return;
    }

    /*
     * ★ LED 反馈 ★
     *   按下 -> 亮蓝，松开 -> 熄灭。
     *   用户靠这个判断「开发板到底有没有收到按键」。
     */
    if (key->down) {
        bridge_led_key_down();
    } else {
        bridge_led_key_up();
    }

    br_hid_key_t k = *key;
    if (xQueueSend(s_keyq, &k, 0) != pdTRUE) {
        br_hid_key_t drop;
        xQueueReceive(s_keyq, &drop, 0);
        xQueueSend(s_keyq, &k, 0);
    }
}

/*
 * ★ 已知遥控器名单 ★
 *
 * 自动模式一直开着扫描，如果不加限制，板子会去连扫描到的**任意**设备
 * （你的手机、电视盒、耳机……）。所以只连名单里的。
 */
#define KNOWN_MAX 8
static char s_known_names[KNOWN_MAX][BR_MAX_NAME_LEN];
static int  s_known_count;

static bool name_is_known(const char *name)
{
    if (!name || !name[0]) {
        return false;
    }
    for (int i = 0; i < s_known_count; i++) {
        if (strcmp(s_known_names[i], name) == 0) {
            return true;
        }
    }
    return false;
}

static void remember_name(const char *name)
{
    if (!name || !name[0] || name_is_known(name)) {
        return;
    }
    if (s_known_count >= KNOWN_MAX) {
        return;
    }
    snprintf(s_known_names[s_known_count], BR_MAX_NAME_LEN, "%s", name);
    s_known_count++;
    ESP_LOGI(TAG, "记住遥控器：%s", name);
}

/*
 * ★ 配对目标 ★
 * PC 端发 PAIR 时记下「要配对谁 + 叫什么」，
 * 连接成功（此时 NimBLE 已自动完成配对并保存 bond）后回报结果。
 */
static bool  s_pair_pending = false;
static bool  s_scan_continuous_before_pair = false;
static char  s_pair_name[BR_MAX_NAME_LEN];
static uint8_t  s_pair_addr[6];
static uint8_t  s_pair_type;
/* 配对窗口截止时刻（esp_timer 微秒）；0 = 没开窗 */
static int64_t  s_auto_pair_until = 0;
static int64_t  s_pair_retry_until;   /* 配对重试截止时刻 */
static int64_t  s_next_pair_retry;    /* 下次重试时刻 */
/*
 * ★ 新鲜广播盯人（fresh-adv watch）★
 *
 * 为什么必须用「刚刚收到的这一条广播」去连：
 *   候选表里存下的地址可能是几十秒前那一轮的。遥控器只在醒着的时候广播，
 *   拿一个过期地址连，控制器发出 CONNECT_IND 也不会有人应答，
 *   表现就是恒定 status=13（BLE_HS_ETIMEOUT）—— 看起来像"谁的错都没有"。
 *   实测：用当轮新鲜地址连，1.8~4 秒就成功。
 *
 * 为什么**只认地址**、不认名字（2026-11-06 用户纠正）：
 *   同型号遥控器名字一模一样，多台在场时按名字根本分不清连的是哪台。
 *   遥控器的 MAC 是固定的（identity address），按地址可靠。
 */
static volatile bool s_pair_fresh_ready = false;
static uint8_t  s_pair_fresh_addr[6];
static uint8_t  s_pair_fresh_type;
static int64_t  s_pair_fresh_us;      /* 抓到这条广播的时刻，用来算「连的时候广播多新」 */

/* 上一次连接失败的真实原因（0 = 还没失败过）*/
static int s_last_conn_status;

/*
 * ★ 连接失败原因要「双语」翻译 ★
 *
 * IDF v6.1 的 NimBLE 没有导出 ble_hs_strerror()，而且 event->connect.status
 * 有两种来源，数值还会撞车：
 *   - 主机自己的错误码 BLE_HS_*（13 = ETIMEOUT：发了 CONNECT_IND 没人应答）
 *   - 控制器透上来的 HCI 错误码（0x08=Connection Timeout、0x3E=失败于建立、
 *     0x13=对端主动断开 —— 注意这个也是 13！）
 * 之前日志里只写一个裸数字，我把「13」当成 BLE_HS_ETIMEOUT 解读，
 * 于是整条排查方向都被带偏了。这里必须两种解释一起打。
 */
static const char *hs_err_name(int e)
{
    switch (e) {
    case BLE_HS_EAGAIN:       return "EAGAIN";
    case BLE_HS_EALREADY:     return "EALREADY";
    case BLE_HS_EINVAL:       return "EINVAL";
    case BLE_HS_ENOENT:       return "ENOENT";
    case BLE_HS_ENOMEM:       return "ENOMEM";
    case BLE_HS_ENOTCONN:     return "ENOTCONN";
    case BLE_HS_ENOTSUP:      return "ENOTSUP";
    case BLE_HS_EBADDATA:     return "EBADDATA";
    case BLE_HS_ECONTROLLER:  return "ECONTROLLER";
    case BLE_HS_ETIMEOUT:     return "ETIMEOUT";
    case BLE_HS_EDONE:        return "EDONE";
    case BLE_HS_EBUSY:        return "EBUSY";
    case BLE_HS_EREJECT:      return "EREJECT";
    case BLE_HS_EUNKNOWN:     return "EUNKNOWN";
    case BLE_HS_ETIMEOUT_HCI: return "ETIMEOUT_HCI";
    case BLE_HS_ENOADDR:      return "ENOADDR";
    default:                  return "?";
    }
}

static const char *hci_err_name(int e)
{
    switch (e) {
    case 0x04: return "UNKNOWN_CONN_ID";
    case 0x08: return "L2CAP_CONN_TIMEOUT";
    case 0x13: return "REMOTE_USER_TERMINATED";
    case 0x14: return "REMOTE_DEV_TERMINATED_RES_LIMIT";
    case 0x1a: return "REMOTE_DEV_TERMINATED_LOW_RES";
    case 0x28: return "INSTANT_RETURNED";
    case 0x29: return "INSUFFICIENT_ENCRYPTION_KEY";
    case 0x3b: return "UNACCEPTABLE_CONN_PARAMS";
    case 0x3e: return "CONN_FAILED_TO_BE_ESTABLISHED";
    default:   return "?";
    }
}

/**
 * 把连接失败状态码翻译成「两种解释都列出来」的一句话。
 */
static void conn_status_desc(int s, char *out, size_t cap)
{
    snprintf(out, cap, "BLE_HS=%d(%s)|HCI=0x%02X(%s)",
             s, hs_err_name(s), s & 0xFF, hci_err_name(s));
}

static bool adv_is_connectable(uint8_t event_type)
{
    /* 0=ADV_IND 1=ADV_DIRECT_IND 4=ADV_DIRECT_IND_HIGH 可连；2/3 不可连 */
    return event_type <= 1 || event_type == 4;
}

/**
 * 这条广播是不是本次配对的目标？—— 只比地址。
 */
static bool is_pair_target_addr(const uint8_t *addr)
{
    return memcmp(addr, s_pair_addr, 6) == 0;
}

/* 配对表在文件后面，这里先声明 */
static int  pl_count(void);
static bool pl_get(int i, uint8_t addr[6], uint8_t *type,
                   char *name, size_t name_cap);

/**
 * 这个广播名像不像一只遥控器？
 *
 * ★ 配对窗口只认它 ★
 *   配对窗口原来「来者不拒」，扫到任何没登记过的 BLE 设备就认作目标。
 *   周围十几个候选（音箱、手机、耳机），抓到哪个算哪个，
 *   于是板子一直对着一个根本不会应答的地址 ETIMEOUT，
 *   用户真正在配的那台遥控器永远轮不上。
 */
static bool name_contains_ci(const char *hay, const char *needle)
{
    if (!hay || !needle || !*hay || !*needle) {
        return false;
    }
    size_t nlen = strlen(needle);
    for (const char *p = hay; *p; p++) {
        size_t i = 0;
        while (i < nlen && p[i] &&
               ((p[i] | 0x20) == (needle[i] | 0x20))) {
            i++;
        }
        if (i == nlen) {
            return true;
        }
    }
    return false;
}

static bool looks_like_remote(const char *name)
{
    if (!name || !name[0]) {
        return false;
    }
    /* 谷歌遥控器的广播名 */
    if (name_contains_ci(name, "chromecast") ||
        name_contains_ci(name, "hid_device") ||
        name_contains_ci(name, "google tv")) {
        return true;
    }
    /* 配对表里出现过的名字 —— 那是用户以前配过的那台 */
    int n = pl_count();
    for (int i = 0; i < n; i++) {
        uint8_t a[6];
        uint8_t t;
        char nm[BR_MAX_NAME_LEN];
        if (!pl_get(i, a, &t, nm, sizeof(nm))) {
            continue;
        }
        if (nm[0] && strcmp(nm, name) == 0) {
            return true;
        }
    }
    return false;
}

/**
 * 这条广播是我们已经登记过的遥控器吗？
 *
 * ★ 只查内存里的 peer 表，不查 bond 表 ★
 *   ble_store_util_bonded_peers() 会读 NVS（flash），
 *   在扫描回调里做等于在 GAP 上下文里阻塞 —— CMCC 文档明确禁止。
 */
/* NVS 配对表（定义在文件后面的「已配对遥控器表」一节） */
static bool pl_get(int i, uint8_t addr[6], uint8_t *type,
                   char *name, size_t name_cap);
static int  pl_count(void);

/**
 * ★ 这个地址是不是「我们登记过的遥控器」★
 *
 * 两个来源都要看：
 *   - 在线槽位（s_peers）：连过一次的
 *   - NVS 配对表（pl）：登记过的
 *
 * ★ 为什么必须查 NVS 表 ★
 *   刚开机时一个 peer 槽都还没有（要连上才有），
 *   只看 s_peers 的话「遥控器醒了 → 扫到 → 用新地址连回去」这条路
 *   在开机后第一次是死的 —— 只能拿旧地址硬撞，每次 5 秒超时。
 *   实测：遥控器 10 分钟里被连了 115 次全部超时，就是这么来的。
 */
static bool is_tracked_addr(const uint8_t *addr)
{
    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (s_peers[i].used && memcmp(s_peers[i].addr, addr, 6) == 0) {
            return true;
        }
    }
    int n = pl_count();
    for (int i = 0; i < n; i++) {
        uint8_t paddr[6];
        uint8_t ptype;
        char pname[BR_MAX_NAME_LEN];
        if (!pl_get(i, paddr, &ptype, pname, sizeof(pname))) {
            continue;
        }
        if (memcmp(paddr, addr, 6) == 0) {
            return true;
        }
    }
    return false;
}

#define PAIR_RETRY_WINDOW_US  (120 * 1000000LL)
#define PAIR_RETRY_GAP_US     (1500 * 1000LL)

/* 连接进行中时记住的目标（connect 事件里拿不到 peer_addr） */
static uint8_t s_connecting_addr[6];
static uint8_t s_connecting_type;
static char    s_connecting_name[BR_MAX_NAME_LEN];

/* --------------------------------------------------------------- 工具函数 */

static void addr_to_str(const uint8_t *addr, char *out, size_t cap)
{
    snprintf(out, cap, "%02X:%02X:%02X:%02X:%02X:%02X",
             addr[5], addr[4], addr[3], addr[2], addr[1], addr[0]);
}

/* 对外的版本：别的模块（br_mapping.c）也要用同一套字节序 */
void br_addr_to_str(const uint8_t *addr, char *out, size_t cap)
{
    addr_to_str(addr, out, cap);
}

/*
 * ★★★ 把 "7E:A2:99:61:45:FE" 解析回 NimBLE 的字节序 ★★★
 *
 * NimBLE 的 ble_addr_t.val 是**小端**：val[5] 才是打印出来的第一字节。
 * 所以这里必须 out[5 - i] = v[i]。
 * 写成正着填（out[i] = v[i]）的后果是致命的，本项目 2026-11-06 实测踩过：
 *   - 要连的目标地址被整体反成 FE:45:61:99:A2:7E → 必然 status=13
 *   - 「这条广播是不是我要配的那台」的 memcmp 永远不相等 → 新鲜广播永不命中
 * 唯一真相放在这里，app_main.c 直接调它，不要再各自写一份。
 */
bool br_ble_addr_parse(const char *s, uint8_t out[6])
{
    unsigned v[6];
    if (sscanf(s, "%02X:%02X:%02X:%02X:%02X:%02X",
               &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        if (v[i] > 0xFF) {
            return false;
        }
        out[5 - i] = (uint8_t)v[i];
    }
    return true;
}

static bool addr_is_all_zero(const uint8_t *a)
{
    for (int i = 0; i < 6; i++) {
        if (a[i] != 0x00) {
            return false;
        }
    }
    return true;
}

static bool addr_is_all_ff(const uint8_t *a)
{
    for (int i = 0; i < 6; i++) {
        if (a[i] != 0xFF) {
            return false;
        }
    }
    return true;
}

/** 从 NimBLE 的 union 型 UUID 转成规范顺序 */
static void uuid_from_any(const ble_uuid_any_t *u, uint8_t *out, uint8_t *out_type)
{
    memset(out, 0, 16);
    *out_type = 0;
    if (!u) {
        return;
    }
    switch (u->u.type) {
    case BLE_UUID_TYPE_16:
        out[0] = (uint8_t)((u->u16.value >> 8) & 0xFF);
        out[1] = (uint8_t)(u->u16.value & 0xFF);
        *out_type = 16;
        break;
    case BLE_UUID_TYPE_32:
        for (int i = 0; i < 4; i++) {
            out[i] = (uint8_t)((u->u32.value >> (24 - 8 * i)) & 0xFF);
        }
        *out_type = 32;
        break;
    case BLE_UUID_TYPE_128:
        /* NimBLE 存的是线序（小端），规范顺序要倒过来 */
        for (int i = 0; i < 16; i++) {
            out[i] = u->u128.value[15 - i];
        }
        *out_type = 128;
        break;
    default:
        break;
    }
}

static void uuid_to_str(const uint8_t *b, uint8_t type, char *out, size_t cap)
{
    if (type == 16) {
        snprintf(out, cap, "%04X",
                 (unsigned)(((unsigned)b[0] << 8) | (unsigned)b[1]));
    } else if (type == 32) {
        snprintf(out, cap, "%08X",
                 (unsigned)(((unsigned)b[0] << 24) | ((unsigned)b[1] << 16) |
                            ((unsigned)b[2] << 8) | (unsigned)b[3]));
    } else if (type == 128) {
        snprintf(out, cap,
                 "%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-"
                 "%02X%02X%02X%02X%02X%02X",
                 (unsigned)b[0], (unsigned)b[1], (unsigned)b[2], (unsigned)b[3],
                 (unsigned)b[4], (unsigned)b[5], (unsigned)b[6], (unsigned)b[7],
                 (unsigned)b[8], (unsigned)b[9], (unsigned)b[10], (unsigned)b[11],
                 (unsigned)b[12], (unsigned)b[13], (unsigned)b[14], (unsigned)b[15]);
    } else {
        snprintf(out, cap, "?");
    }
}

static void sanitize_name(const char *src, char *dst, size_t cap)
{
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p && o + 1 < cap; p++) {
        if (*p < 0x20 || *p == 0x7F) {
            continue;   /* 控制字符丢掉 */
        }
        dst[o++] = (char)*p;
    }
    dst[o] = '\0';
    /*
     * ★ 不填占位名 ★
     *   占位名（比如 "HID_Device"）会骗过「名字为空」的判断，
     *   导致 SCAN_RSP 里带来的真名再也补不进去。
     *   无名就留空，由 UI 诚实显示「未命名设备」。
     */
}

static int find_peer_by_conn(uint16_t conn_handle)
{
    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (s_peers[i].used && s_peers[i].connected &&
            s_peers[i].conn_handle == conn_handle) {
            return i;
        }
    }
    return -1;
}

static int alloc_peer(void)
{
    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (!s_peers[i].used) {
            memset(&s_peers[i], 0, sizeof(s_peers[i]));
            s_peers[i].used = true;
            s_peers[i].idx = (uint8_t)i;
            s_peers[i].disc_state = BR_DISC_NONE;
            if (i + 1 > s_peer_count) {
                s_peer_count = i + 1;
            }
            return i;
        }
    }

    /*
     * ★ 没有空槽了：回收最久没动静的**断链**槽 ★
     *
     * 以前这里是直接 return -1，然后调用方打「no free peer slot」就放弃。
     * 而槽位**从断链到复位一次都没有**（s_peers[i].used 全文件没有第二处置 false），
     * BLE 私有地址每轮广播都换 → 每次重连都新占一个槽 → 8 个槽很快就用光，
     * 板子从此再也连不上任何遥控器，**只有断电清 RAM 才能恢复**
     * （真机症状：删掉遥控器再配对，刷新永远刷不出来，
     *   必须两个 USB 口都拔掉重插才好）。
     *
     * 复用断链槽不影响「已登记设备醒了用当轮地址连回去」——
     * 那条路走的是 s_pair_fresh_addr，不占 peer 槽。
     */
    int victim = -1;
    int64_t oldest = 0;
    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (s_peers[i].connected) {
            continue;
        }
        if (victim < 0 || s_peers[i].last_seen_us < oldest) {
            victim = i;
            oldest = s_peers[i].last_seen_us;
        }
    }
    if (victim < 0) {
        return -1;   /* 全都在连，确实没槽了 */
    }
    ESP_LOGW(TAG, "peer 槽位用完，回收最久没动静的槽 %d", victim);
    memset(&s_peers[victim], 0, sizeof(s_peers[victim]));
    s_peers[victim].used = true;
    s_peers[victim].idx = (uint8_t)victim;
    s_peers[victim].disc_state = BR_DISC_NONE;
    if (victim + 1 > s_peer_count) {
        s_peer_count = victim + 1;
    }
    return victim;
}

/* --------------------------------------------------------------- 扫描回调 */

/**
 * 扫描事件回调。
 *
 * ★ 这里只做记录，**绝不做任何连接/停扫描动作** ★
 */
static int scan_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;

    switch (event->type) {
    case BLE_GAP_EVENT_DISC: {
        struct ble_hs_adv_fields fields;
        memset(&fields, 0, sizeof(fields));
        ble_hs_adv_parse_fields(&fields, event->disc.data,
                                 event->disc.length_data);

        char raw_name[BR_MAX_NAME_LEN] = { 0 };
        if (fields.name && fields.name_len &&
            fields.name_len < sizeof(raw_name)) {
            memcpy(raw_name, fields.name, fields.name_len);
            raw_name[fields.name_len] = '\0';
        }

        char addr[24];
        addr_to_str(event->disc.addr.val, addr, sizeof(addr));

        /*
         * ★ 新鲜广播盯人 ★
         *
         * 必须在**去重提前 return 之前**做：候选表的条目一旦名字补齐，
         * 后续同地址的广播全部走 dedup 分支并直接 return，
         * 老写法（只在名字刚补齐的那一刻标一次）之后就再也标不上，
         * 于是遥控器醒了也抓不到，只能拿过期地址瞎试 —— 恒超时。
         */
        if (adv_is_connectable((uint8_t)event->disc.event_type)) {
            /*
             * ★ 配对目标地址会过期 ★
             *
             *   遥控器用轮换的 BLE 私有地址：同一台机器，每轮广播
             *   地址都不一样。原来只在「地址和目标完全一样」时
             *   才刷新新鲜地址 → 轮换一次之后，
             *   板子就一直对着那个已经失效的地址死磕，
             *   每次都是 ETIMEOUT，配对永远差一步。
             *
             *   改成：**名字一样就是同一台** —— 名字跟着地址换，
             *   目标地址也跟着换。
             */
            bool same_target = is_pair_target_addr(event->disc.addr.val);
            if (!same_target && s_pair_pending &&
                raw_name[0] != '\0' && s_pair_name[0] != '\0' &&
                strcmp(raw_name, s_pair_name) == 0) {
                same_target = true;
                memcpy(s_pair_addr, event->disc.addr.val, 6);
                s_pair_type = event->disc.addr.type;
                ESP_LOGI(TAG, "配对目标换地址了：%s", addr);
            }
            if (s_pair_pending && same_target) {
                memcpy(s_pair_fresh_addr, event->disc.addr.val, 6);
                s_pair_fresh_type = event->disc.addr.type;
                s_pair_fresh_us = esp_timer_get_time();
                s_pair_fresh_ready = true;
            } else if (!s_pair_pending && is_tracked_addr(event->disc.addr.val)
                       && !s_connect_in_progress) {
                /* 已登记遥控器醒了：同样用当前地址连回去 */
                memcpy(s_pair_fresh_addr, event->disc.addr.val, 6);
                s_pair_fresh_type = event->disc.addr.type;
                s_pair_fresh_us = esp_timer_get_time();
                s_pair_fresh_ready = true;
            }
        }

        /*
         * ★ 配对窗口 ★
         *
         * PC 端说「接下来 120 秒你帮我收新遥控器」时，
         * 窗口内扫到**没登记过**的设备就直接走配对流程，
         * 连上就 pl_put() 落库并发 EV PAIR —— 不用 PC 再发 PAIR。
         *
         * 以前必须 PC 发 PAIR 才会落库（pl_put 的唯一入口在
         * s_pair_pending 分支里），所以「两边都进配对模式」时
         * 板子虽然连上了，却没写进配对表 —— PC 端看着像没反应。
         */
        if (s_auto_pair_until && !s_pair_pending && !s_connect_in_progress &&
            !is_tracked_addr(event->disc.addr.val) &&
            looks_like_remote(raw_name)) {
            int64_t now = esp_timer_get_time();
            if (now < s_auto_pair_until) {
                sanitize_name(raw_name, s_pair_name, sizeof(s_pair_name));
                s_pair_pending = true;
                memcpy(s_pair_addr, event->disc.addr.val, 6);
                s_pair_type = event->disc.addr.type;
                s_pair_retry_until = now + PAIR_RETRY_WINDOW_US;
                s_next_pair_retry = now + PAIR_RETRY_GAP_US;
                memcpy(s_pair_fresh_addr, event->disc.addr.val, 6);
                s_pair_fresh_type = event->disc.addr.type;
                s_pair_fresh_ready = true;
                ESP_LOGI(TAG, "pair window: accepting %s", addr);
            }
        }

        /*
         * 板端自主静默扫描只负责捕获已登记设备的新鲜地址。
         * 不把周围其他 BLE 设备塞入 PC 候选队列，避免客户端关闭时
         * USB CDC TX 长期积压；显式配对扫描仍完整收集候选设备。
         */
        if (s_auto && !s_pair_pending && !s_scan_session_active &&
            !is_tracked_addr(event->disc.addr.val)) {
            return 0;
        }

        /* 打分 */
        int score = 0;
        const char *why = "";
        if (s_target_name[0] && raw_name[0] &&
            strstr(raw_name, s_target_name)) {
            score = 100;
            why = "name";
        } else if (s_target_name[0]) {
            score = 1;
            why = "other";
        } else if (fields.num_uuids128) {
            score = 50;
            why = "uuid128";
        } else if (fields.num_uuids16) {
            score = 40;
            why = "uuid16";
        } else if (raw_name[0]) {
            score = 20;
            why = "name-only";
        } else {
            score = 1;
            why = "anon";
        }

        /*
         * 去重 —— 但**要补名字**。
         *
         * ★ 遥控器的名字通常不在 ADV_IND 里，而在 SCAN_RSP 里 ★
         *   同一个地址会广播两次：先 ADV_IND（无名字），再 SCAN_RSP（有名字）。
         *   之前这里直接 return，把带名字的那条丢掉了 ——
         *   结果扫出来的设备全是 "HID_Device"，UI 上根本认不出哪个是遥控器。
         */
        for (int i = 0; i < s_candidate_count; i++) {
            if (memcmp(s_candidates[i].addr, event->disc.addr.val, 6) == 0 &&
                s_candidates[i].addr_type == event->disc.addr.type) {
                s_candidates[i].rssi = event->disc.rssi;
                if (raw_name[0] &&
                    (!s_candidates[i].name_known ||
                     strcmp(s_candidates[i].name, raw_name) != 0)) {
                    sanitize_name(raw_name, s_candidates[i].name,
                                  sizeof(s_candidates[i].name));
                    s_candidates[i].name_known = true;
                    s_candidates[i].adv_type = (uint8_t)event->disc.event_type;
                    ESP_LOGI(TAG, "cand %s name resolved: %s",
                             addr, s_candidates[i].name);
                    /* 重新算分并重发一条 SCAN 事件 */
                    s_candidates[i].score = score;
                    br_kv_t kv;
                    br_kv_init(&kv);
                    br_kv_add_int(&kv, "rssi", s_candidates[i].rssi);
                    br_kv_add(&kv, "addr", addr);
                    br_kv_add_int(&kv, "type", s_candidates[i].addr_type);
                    br_kv_add(&kv, "name", s_candidates[i].name);
                    br_kv_add(&kv, "why", why);
                    br_kv_add_int(&kv, "score", score);
                    br_kv_add_int(&kv, "idx", i);
                    br_kv_add_int(&kv, "adv", (int)event->disc.event_type);
                    br_proto_send_ev("SCAN", &kv);
                }
                return 0;
            }
        }
        if (s_candidate_count >= BR_MAX_CANDIDATES) {
            return 0;
        }

        br_candidate_t *c = &s_candidates[s_candidate_count++];
        memset(c, 0, sizeof(*c));
        c->used = true;
        memcpy(c->addr, event->disc.addr.val, 6);
        c->addr_type = event->disc.addr.type;
        /*
         * ★ 无名就留空，不要填占位名 ★
         *   占位名会骗过「名字为空」的判断，导致真名永远补不进来；
         *   而且 UI 上显示 "HID_Device" 会让人以为是真名。
         */
        if (raw_name[0]) {
            sanitize_name(raw_name, c->name, sizeof(c->name));
            c->name_known = true;
        } else {
            c->name[0] = 0;
        }
        c->rssi = event->disc.rssi;
        c->score = score;
        c->adv_type = (uint8_t)event->disc.event_type;
        snprintf(c->why, sizeof(c->why), "%s", why);

        ESP_LOGI(TAG, "cand %s '%s' rssi=%d score=%d (%s)",
                 addr, c->name_known ? c->name : "(未命名)", c->rssi, c->score,
                 why);

        /*
         * ★ 这里**不发协议行** ★
         *   协议行的组装要 512 字节栈 + snprintf，
         *   在 GAP 回调里做会加重协议栈的负担。
         *   事件交给主循环任务去发。
         */
        s_emit_upto = s_candidate_count;
        return 0;
    }

    case BLE_GAP_EVENT_DISC_COMPLETE:
        s_scanning = false;
        s_scan_round_done = true;
        s_round_done_at = esp_timer_get_time();
        ESP_LOGI(TAG, "scan round complete");
        return 0;

    default:
        return 0;
    }
}

/**
 * 开始一次**扫描会话**（PC 端点「扫描」按钮）。
 *
 * 会话开始 = 清空候选表 + 通知 PC 清空列表；
 * 会话内各轮扫描复用同一张候选表。
 */
bool br_ble_scan_session_start(bool continuous)
{
    s_candidate_count = 0;
    s_emit_done = 0;
    s_emit_upto = 0;
    s_scan_session_active = false;
    return br_ble_scan_start(continuous);
}

static bool scan_start_internal(bool continuous, bool quiet);

bool br_ble_scan_start(bool continuous)
{
    return scan_start_internal(continuous, false);
}

/**
 * ★ 自动模式用的「静默扫描轮」★
 *
 * 目的只有一个：抓住「已登记遥控器刚醒来」的那一下广播，
 * 好用**新鲜地址**连回去（拿旧地址硬撞每次都是 5 秒超时）。
 *
 * 区别：不发 SCANSTART —— 那是 PC 端「扫描」按钮的信号，
 * 自动扫描每几秒发一次的话，客户端的候选列表会被一直冲掉。
 */
bool br_ble_scan_round_quiet(void)
{
    return scan_start_internal(false, true);
}

static bool scan_start_internal(bool continuous, bool quiet)
{
    if (s_scanning) {
        return true;
    }
    /*
     * ★ 这里**不清空**候选列表 ★
     *
     *   候选表按「扫描会话」清空，不是按「轮次」：
     *   设备的 ADV_IND 和 SCAN_RSP（带名字）可能落在不同轮里，
     *   按轮清空的话，SCAN_RSP 带来的真名就再也补不进来了。
     *
     *   会话级的清空见 br_ble_scan_session_start()。
     */
    s_scan_continuous = continuous || quiet;
    s_scan_round_done = false;

    /*
     * 扫描占空比：itvl=160(100ms) / window=60(37.5ms) ≈ 37%。
     * 之前照 CMCC 用 300/30（10%）—— 遥控器醒着的时间窗口就那么几秒，
     * 10% 占空比很容易整段错过，导致「扫不到 → 连不上 → 超时」。
     *
     * 默认用**主动扫描**（passive=0）—— 实测被动扫描收不到定向广播，
     * 而且很多遥控器把名字放在 SCAN_RSP 里。
     */
    struct ble_gap_disc_params params = {
        .itvl = 160,
        .window = 60,
        .filter_policy = 0,
        .passive = 0,
        .filter_duplicates = 0,
    };

    /*
     * ★ 自动模式的静默扫描轮：占空比拉满 ★
     *
     * 低功耗遥控器只在被唤醒的那一两秒广播，地址还会轮换 ——
     * 我们必须**尽量频繁地**看到它，才有机会用新鲜地址连上去。
     * itvl=16/window=16（10ms/10ms）= 100% 占空比，
     * 跟 NimBLE 自己连���时用的扫描参数一样（它也是 16/16）。
     */
    if (quiet) {
        params.itvl = 16;
        params.window = 16;
    }

    int rc = BLE_HS_EBUSY;
    for (int attempt = 0; attempt < 5; attempt++) {
        /*
         * ★ 扫描也用同一个本机地址 ★
         *   主动扫描要发 SCAN_REQ，里面带的就是本机地址；
         *   地址全 0 时对端可以不回 SCAN_RSP ——
         *   这正好对上之前「扫得到设备却拿不到真名字」的现象。
         */
        rc = ble_gap_disc(s_own_addr_type, SCAN_ROUND_MS, &params,
                          scan_event, NULL);
        if (rc != BLE_HS_EBUSY) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(150));
    }

    if (!quiet && (!continuous || !s_scan_session_active)) {
        /*
         * ★ SCANSTART 只在**会话开始**时发一次 ★
         *   每轮都发的话，PC 端会不停清空自己的候选列表。
         *   自动模式的静默扫描轮（quiet）更不能发。
         */
        if (!quiet) {
            s_scan_session_active = true;
            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add_int(&kv, "rc", rc);
            br_kv_add_int(&kv, "duration", SCAN_ROUND_MS);
            br_kv_add_int(&kv, "continuous", continuous ? 1 : 0);
            br_proto_send_ev("SCANSTART", &kv);
        }
    }

    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gap_disc failed: %d", rc);
        return false;
    }
    s_scanning = true;
    ESP_LOGI(TAG, "scanning started (continuous=%d)", continuous ? 1 : 0);
    return true;
}

void br_ble_scan_stop(void)
{
    /*
     * ★ 连着设备的时候绝不能继续扫描 ★
     *   扫描和连接抢同一个控制器，遥控器那边会判定拥塞直接断链
     *   （CMCC 实测：一边扫一边连，链路必断）。
     *
     * ★ 而且必须把 s_scan_continuous 一起清掉 ★
     *   不清的话主循环下一轮就把它重新拉起来 ——
     *   PC 端点「停止扫描」，固件这边一秒后又开上了，
     *   表现就是「停不下来的扫描」，连接一建立就被自己搞断。
     */
    if (!s_pair_pending) {
        s_scan_continuous = false;
    }
    if (!s_scanning) {
        return;
    }
    int rc = ble_gap_disc_cancel();
    s_scanning = false;
    s_scan_session_active = false;
    ESP_LOGI(TAG, "scan stopped (rc=%d)", rc);
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "rc", rc);
    br_proto_send_ev("SCANSTOP", &kv);
}

bool br_ble_scanning(void)
{
    return s_scanning;
}

int br_ble_candidate_count(void)
{
    return s_candidate_count;
}

/* ----------------------------------------------------------- 发现回调 */

static int disc_svc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       const struct ble_gatt_svc *service, void *arg);
static int disc_chr_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       const struct ble_gatt_chr *chr, void *arg);
static int disc_dsc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       uint16_t chr_val_handle, const struct ble_gatt_dsc *dsc,
                       void *arg);
static void subscribe_atvv(int peer_idx);
static void ensure_encrypted(int peer_idx);
static void open_channels(int peer_idx);
/* bond 相关（定义在文件后面，这里先声明） */
static bool peer_has_bond(const uint8_t *addr);
static bool peer_drop_bond(const uint8_t *addr);

/** 发出一条服务的所有特征 */
static void emit_service(int peer_idx, const br_svc_t *svc)
{
    br_peer_t *p = &s_peers[peer_idx];

    char suuid[40];
    uuid_to_str(svc->uuid, svc->uuid_type, suuid, sizeof(suuid));

    /*
     * ★ 只用一个 br_kv_t，反复 br_kv_init 清空 ★
     *   br_kv_t 是 512 字节的栈对象，开三个就是 1.5KB ——
     *   这个函数在 BLE 回调链上跑，栈不能乱花。
     */
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "idx", peer_idx);
    br_kv_add(&kv, "uuid", suuid);
    br_kv_add_int(&kv, "utype", svc->uuid_type);
    br_kv_add_int(&kv, "start", svc->start_handle);
    br_kv_add_int(&kv, "end", svc->end_handle);
    br_proto_send_dt("SVC", &kv);

    /* ATVV 服务单独标出来 —— 这是 go/no-go 的关键 */
    if (svc->uuid_type == 128 &&
        memcmp(svc->uuid, k_atvv_service_canon, 16) == 0) {
        ESP_LOGI(TAG, ">>> ATVV service found: %s (handle %u..%u)",
                 suuid, svc->start_handle, svc->end_handle);
        br_kv_init(&kv);
        br_kv_add_int(&kv, "idx", peer_idx);
        br_kv_add(&kv, "uuid", suuid);
        br_kv_add_int(&kv, "start", svc->start_handle);
        br_kv_add_int(&kv, "end", svc->end_handle);
        br_proto_send_dt("ATVVSVC", &kv);
    }

    for (int i = 0; i < p->chr_count; i++) {
        const br_chr_t *c = &p->chrs[i];
        if (c->val_handle < svc->start_handle ||
            c->val_handle > svc->end_handle) {
            continue;
        }
        char cuuid[40];
        uuid_to_str(c->uuid, c->uuid_type, cuuid, sizeof(cuuid));

        br_kv_init(&kv);
        br_kv_add_int(&kv, "idx", peer_idx);
        br_kv_add(&kv, "uuid", cuuid);
        br_kv_add_int(&kv, "utype", c->uuid_type);
        br_kv_add_int(&kv, "props", c->props);
        br_kv_add_int(&kv, "handle", c->val_handle);
        br_kv_add_int(&kv, "def", c->def_handle);
        br_kv_add_int(&kv, "cccd", c->cccd_handle);
        br_kv_add_int(&kv, "ref", c->ref_handle);
        br_proto_send_dt("CHR", &kv);

        /* 重点标出 ATVV 的三个特征 */
        if (c->uuid_type == 128) {
            const uint8_t *u = c->uuid;
            bool is_audio = memcmp(u, k_atvv_audio_canon, 16) == 0;
            bool is_ctl = memcmp(u, k_atvv_ctl_canon, 16) == 0;
            bool is_tx = memcmp(u, k_atvv_tx_canon, 16) == 0;

            if (is_audio) {
                p->atvv_audio_handle = c->val_handle;
                p->atvv_audio_cccd = c->cccd_handle;
                p->atvv_seen = true;
            } else if (is_ctl) {
                p->atvv_ctl_handle = c->val_handle;
                p->atvv_ctl_cccd = c->cccd_handle;
                p->atvv_seen = true;
            } else if (is_tx) {
                p->atvv_tx_handle = c->val_handle;
                p->atvv_seen = true;
            }
            if (is_audio || is_ctl || is_tx) {
                char tag[40];
                uuid_to_str(u, 128, tag, sizeof(tag));
                ESP_LOGI(TAG, ">>> ATVV char %s handle=%u props=0x%02x cccd=%u",
                         tag, c->val_handle, c->props, c->cccd_handle);
                br_kv_init(&kv);
                br_kv_add_int(&kv, "idx", peer_idx);
                br_kv_add(&kv, "uuid", tag);
                br_kv_add_int(&kv, "handle", c->val_handle);
                br_kv_add_int(&kv, "props", c->props);
                br_kv_add_int(&kv, "cccd", c->cccd_handle);
                br_proto_send_dt("ATVV", &kv);
            }
        }
    }
}

/** 特征发现回调 */
static int disc_chr_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       const struct ble_gatt_chr *chr, void *arg)
{
    (void)conn_handle;
    int peer_idx = (int)(intptr_t)arg;
    if (peer_idx < 0 || peer_idx >= BR_MAX_PEERS) {
        return 0;
    }
    br_peer_t *p = &s_peers[peer_idx];

    uint16_t status = error ? error->status : 0;
    if (status != 0 && status != BLE_HS_EDONE) {
        ESP_LOGE(TAG, "chr discovery failed: %u", status);
        p->disc_state = BR_DISC_NONE;
        return 0;
    }
    if (status == BLE_HS_EDONE) {
        /*
         * 该服务的特征发现完毕。
         *
         * ★ 这里**不能直接发起** disc_all_dscs ★
         *   我们正处在 disc_all_chrs 的回调里，NimBLE 不允许在过程回调中
         *   再发起另一个过程（会返回 EBUSY，描述符一个都发现不到）。
         *   → 只改状态，真正的发起动作交给主循环任务。
         */
        p->disc_state = BR_DISC_NEXT_DSC;
        return 0;
    }
    if (!chr) {
        return 0;
    }

    if (p->chr_count < MAX_CHRS_PER_PEER) {
        br_chr_t *c = &p->chrs[p->chr_count++];
        memset(c, 0, sizeof(*c));
        c->def_handle = chr->def_handle;
        c->val_handle = chr->val_handle;
        c->props = chr->properties;
        uuid_from_any(&chr->uuid, c->uuid, &c->uuid_type);
    }
    return 0;
}

/** 描述符发现回调 */
static int disc_dsc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       uint16_t chr_val_handle, const struct ble_gatt_dsc *dsc,
                       void *arg)
{
    (void)conn_handle;
    (void)chr_val_handle;   /* 见下面注释：这个参数在整服务范围的发现里没有意义 */
    int peer_idx = (int)(intptr_t)arg;
    if (peer_idx < 0 || peer_idx >= BR_MAX_PEERS) {
        return 0;
    }
    br_peer_t *p = &s_peers[peer_idx];

    uint16_t status = error ? error->status : 0;
    if (status == BLE_HS_EDONE) {
        /* 本服务处理完 → 发出并进入下一个服务 */
        emit_service(peer_idx, &p->svcs[p->cur_svc]);
        p->cur_svc++;
        if (p->cur_svc >= p->svc_count) {
            p->disc_state = BR_DISC_DONE;
            ESP_LOGI(TAG, "gatt discovery done (peer %d, %d svcs, %d chrs, atvv=%d)",
                     peer_idx, p->svc_count, p->chr_count,
                     p->atvv_seen ? 1 : 0);
            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add_int(&kv, "idx", peer_idx);
            br_kv_add_int(&kv, "svcs", p->svc_count);
            br_kv_add_int(&kv, "chrs", p->chr_count);
            br_kv_add_int(&kv, "atvv", p->atvv_seen ? 1 : 0);
            br_proto_send_ev("GATTDONE", &kv);
            /*
             * ★ 发现完就先协商加密，再订阅 ★
             *   订阅动作在 ensure_encrypted() 的分支里做
             *   （已加密 / 协商失败 → 直接订阅；协商中 → 等 ENC_CHANGE 再订阅）。
             */
            ensure_encrypted(peer_idx);
        } else {
            /* ★ 交给主循环发起下一个服务的特征发现 ★ */
            p->disc_state = BR_DISC_NEXT_CHR;
        }
        return 0;
    }
    if (status != 0 || !dsc) {
        return 0;
    }

    /* 只关心 CCCD(0x2902) 和 Report Reference(0x2908) */
    if (dsc->uuid.u.type != BLE_UUID_TYPE_16 ||
        (dsc->uuid.u16.value != UUID_CCCC &&
         dsc->uuid.u16.value != UUID_REPORT_REF)) {
        ESP_LOGD(TAG, "dsc handle=%u type=%u (not CCCD/ReportRef)",
                 dsc->handle, dsc->uuid.u.type);
        return 0;
    }
    bool is_cccd = (dsc->uuid.u16.value == UUID_CCCC);
    /*
     * ★ 归属判断：按句柄区间找父特征，别用回调里的 chr_val_handle ★
     *   这里发起的是「整个服务范围」的描述符发现
     *       disc_all_dscs(svc.start_handle, svc.end_handle)
     *   NimBLE 回调第二个参数原样回传的是我们传的 start_handle（= 服务起始句柄），
     *   不是每个描述符真正的父特征 —— 拿它去匹配 val_handle 永远匹配不上，
     *   于是 DT CHR 里的 cccd= 全是 0，看起来像「这台设备没有 CCCD」。
     *   正确的归属：描述符句柄落在 val_handle 之后、下一个声明句柄之前。
     */
    {
        uint16_t svc_end = p->svcs[p->cur_svc].end_handle;
        for (int i = 0; i < p->chr_count; i++) {
            uint16_t lo = p->chrs[i].val_handle;
            uint16_t hi = (i + 1 < p->chr_count)
                              ? (uint16_t)(p->chrs[i + 1].def_handle - 1)
                              : svc_end;
            if (dsc->handle > lo && dsc->handle <= hi) {
                if (is_cccd) {
                    p->chrs[i].cccd_handle = dsc->handle;
                } else {
                    p->chrs[i].ref_handle = dsc->handle;
                }
                ESP_LOGI(TAG, "%s found: handle=%u parent_val=%u",
                         is_cccd ? "CCCD" : "ReportRef", dsc->handle, lo);
                break;
            }
        }
    }
    return 0;
}

/** 服务发现回调 */
static int disc_svc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                       const struct ble_gatt_svc *service, void *arg)
{
    (void)conn_handle;
    int peer_idx = (int)(intptr_t)arg;
    if (peer_idx < 0 || peer_idx >= BR_MAX_PEERS) {
        return 0;
    }
    br_peer_t *p = &s_peers[peer_idx];

    uint16_t status = error ? error->status : 0;
    if (status != 0 && status != BLE_HS_EDONE) {
        ESP_LOGE(TAG, "svc discovery failed: %u", status);
        p->disc_state = BR_DISC_NONE;
        return 0;
    }
    if (status == BLE_HS_EDONE) {
        /* 全部服务发现完毕 → 逐个发现特征（由主循环发起） */
        p->cur_svc = 0;
        p->chr_count = 0;
        if (p->svc_count == 0) {
            p->disc_state = BR_DISC_DONE;
            br_proto_send_ev("GATTDONE", NULL);
            return 0;
        }
        p->disc_state = BR_DISC_NEXT_CHR;
        return 0;
    }
    if (!service) {
        return 0;
    }

    if (p->svc_count < MAX_SVCS_PER_PEER) {
        br_svc_t *s = &p->svcs[p->svc_count++];
        s->start_handle = service->start_handle;
        s->end_handle = service->end_handle;
        uuid_from_any(&service->uuid, s->uuid, &s->uuid_type);
    }
    return 0;
}

/**
 * 订阅 ATVV 的 audio / control 特征。
 *
 * hold-to-talk 模式下**不需要往 TX 写任何东西** ——
 * 遥控器自己开麦、自己发 AUDIO_START/AUDIO_STOP，我们只被动订阅。
 */
static void subscribe_atvv(int peer_idx);
static void atvv_cap_query(int peer_idx);
static int  peer_idx_of(const br_peer_t *p);

static void subscribe_atvv(int peer_idx)
{
    br_peer_t *p = &s_peers[peer_idx];
    if (!p->connected) {
        return;
    }
    /*
     * ★ 这里只做「本地注册」，真正让对端发数据的是**写 CCCD** ★
     *
     *   ble_gattc_register_for_notification() 只是在本机记一笔，
     *   **对端并不知道我们要收** —— 对端要发通知，
     *   必须由我们去写它那个特征下面的 CCCD（0x2902）。
     *   以前一直没写，所以 ATVV 通道从来没真正打开过
     *   （界面上的「ATVV 语音通道就绪」只是我们自己的标志位）。
     *
     *   ★ 写 CCCD 是 GATT 过程，不能在 GAP 回调里发起 ★
     *   所以这里只置标志，实际写入交给主循环（atvv_sub_drive）。
     */
    if (p->atvv_audio_handle) {
        int rc = ble_gattc_register_for_notification(p->conn_handle,
                                                     p->atvv_audio_handle);
        ESP_LOGI(TAG, "register ATVV audio handle=%u rc=%d",
                 p->atvv_audio_handle, rc);
    }
    if (p->atvv_ctl_handle) {
        int rc = ble_gattc_register_for_notification(p->conn_handle,
                                                     p->atvv_ctl_handle);
        ESP_LOGI(TAG, "register ATVV ctl handle=%u rc=%d",
                 p->atvv_ctl_handle, rc);
    }
    p->atvv_subscribed = p->atvv_audio_handle || p->atvv_ctl_handle;
    /* 排队写 CCCD（主循环里做） */
    p->atvv_sub_step = 0;

    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "idx", peer_idx);
    br_kv_add_int(&kv, "audio", p->atvv_audio_handle);
    br_kv_add_int(&kv, "ctl", p->atvv_ctl_handle);
    br_kv_add_int(&kv, "tx", p->atvv_tx_handle);
    br_kv_add_int(&kv, "ok", p->atvv_subscribed ? 1 : 0);
    br_proto_send_ev("ATVVREADY", &kv);
}

/**
 * ★ 写 ATVV 特征的 CCCD（主循环里做）★
 *
 * 没有这一步，对端永远不会在 ATVV 通道上发通知。
 */
static int atvv_cccd_write_cb(uint16_t conn_handle,
                              const struct ble_gatt_error *error,
                              struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    uint16_t status = error ? error->status : 0;
    br_peer_t *p = (br_peer_t *)arg;
    ESP_LOGI(TAG, "ATVV CCCD 写完: status=%u", status);
    if (status == 0) {
        p->atvv_sub_step++;       /* 下一个（音频 -> 控制） */
        if (p->atvv_sub_step == 2) {
            /* 两个 CCCD 都写完了 -> 发能力查询（winmic 就是这么做的） */
            atvv_cap_query(peer_idx_of(p));
        }
    } else {
        p->atvv_sub_step = 4;     /* 失败就不再试了 */
    }
    return 0;
}

static void atvv_sub_drive(int peer_idx)
{
    br_peer_t *p = &s_peers[peer_idx];
    if (!p->connected || p->atvv_sub_step > 1) {
        return;
    }
    uint16_t cccd = (p->atvv_sub_step == 0) ? p->atvv_audio_cccd
                                            : p->atvv_ctl_cccd;
    if (!cccd) {
        p->atvv_sub_step++;
        return;
    }
    static const uint8_t enable_notify[2] = {0x01, 0x00};
    int rc = ble_gattc_write_flat(p->conn_handle, cccd, enable_notify,
                                  sizeof(enable_notify),
                                  atvv_cccd_write_cb, p);
    if (rc != 0) {
        ESP_LOGW(TAG, "写 ATVV CCCD 失败: %d", rc);
        p->atvv_sub_step = 4;
    }
    /* 成功则等回调推进 atvv_sub_step（这里不要再 ++，否则会跳过一个） */
}

/** 由 peer 指针反推下标（回调里要用） */
static int peer_idx_of(const br_peer_t *p)
{
    return (int)(p - s_peers);
}

/**
 * ★ ATVV 能力查询 ★
 *
 * winmic 在订阅完 ATVV 的两个 CCCD 之后，**还会往 tx 通道写一条命令**：
 *   GET_CAPABILITIES_V10 = 0A 01 00 00 03 03
 * 见 reference/winmic-src/apps/windows/rc003/src/ovb_rc003/atvv_protocol.py:25
 *
 * 我们这只遥控器（ATVV 服务 AB5E0001-5A21-4F05-BC7D-AF01F617B664）
 * 很可能也要这条握手才开始往外发数据 —— 我们之前从来没往 tx 写过东西。
 */
static int atvv_tx_write_cb(uint16_t conn_handle,
                            const struct ble_gatt_error *error,
                            struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    uint16_t status = error ? error->status : 0;
    br_peer_t *p = (br_peer_t *)arg;
    ESP_LOGI(TAG, "ATVV 能力查询写完: status=%u", status);
    p->atvv_sub_step = 4;
    return 0;
}

static void atvv_cap_query(int peer_idx)
{
    br_peer_t *p = &s_peers[peer_idx];
    if (!p->connected || !p->atvv_tx_handle) {
        return;
    }
    static const uint8_t get_capabilities[6] = {
        0x0A, 0x01, 0x00, 0x00, 0x03, 0x03
    };
    int rc = ble_gattc_write_flat(p->conn_handle, p->atvv_tx_handle,
                                  get_capabilities, sizeof(get_capabilities),
                                  atvv_tx_write_cb, p);
    if (rc != 0) {
        ESP_LOGW(TAG, "写 ATVV 能力查询失败: %d", rc);
        p->atvv_sub_step = 4;
    }
}

/**
 * 打开这台遥控器的所有通知通道：ATVV + HID 报告。
 *
 * ★ 加密协商成功后必须再走一次 ★
 *   CCCD 在不加密时也可能「写成功」—— 写请求被接受了，
 *   但设备不会开始发通知。所以协商完之后要重新订阅一遍。
 */
static void open_channels(int peer_idx)
{
    br_peer_t *p = &s_peers[peer_idx];
    if (!p->connected) {
        return;
    }
    /*
     * ENC_CHANGE 常常比 GATT 发现先到（带着旧 LTK 重连时，链路一建立就加密了）。
     * 那时所有特征句柄还是 0，写 CCCD 等于写空气。这里直接返回，
     * 等 GATTDONE 走到 ensure_encrypted() 时再订阅 —— 那时加密状态已经是最新的。
     */
    if (p->disc_state != BR_DISC_DONE) {
        return;
    }
    /* 加密状态没变、也已经订阅过：别把在途的 HID 发现重新来一遍 */
    if (p->channels_open && p->channels_enc == p->encrypted) {
        return;
    }
    p->channels_open = true;
    p->channels_enc = p->encrypted;
    if (p->atvv_seen) {
        subscribe_atvv(peer_idx);
    }
    /* GATT 摸清了，接着去找 HID 报告特征并订阅 */
    br_hid_begin(peer_idx, p->conn_handle);
}

/**
 * ★ 连上之后主动发起一次加密协商 ★
 *
 * 为什么必须发（2026-11-06 实测）：
 *   配对成功、服务全摸清（svcs=8 chrs=36 atvv=1）、链路稳定 65 秒+，
 *   但**一个通知都没收到过**（整份日志 EV KEY / DT NT / DT AU 全 0 条）。
 *   表现就是：按遥控器任何键，板子灯不亮，PC 也学不到键。
 *   HID over GATT 的 Report 特征按规范要求加密链路 —— 安卓盒子/Windows
 *   这些原生主机都是「连上→发现→发起配对→订阅」，我们漏了中间那步。
 *
 * ★ 与 CMCC 那条教训的关系 ★
 *   CMCC 写的是「连上就 startSecurity，5.5 秒被遥控器断链」，
 *   那是**连接刚建立、服务还没发现**的时候插一脚。
 *   这里放在 GATTDONE 之后，且每台连接只试一次；
 *   协商失败（回 EV ENC status≠0）就退回不加密再订阅一次，
 *   所以最坏情况等于现在的行为，不会更差。
 *   如果这台 Chromecast 遥控器照样把链路踢掉，日志里会是
 *   EV ENC 紧跟 EV DISC —— 那时候再决定按设备类型区分策略。
 */
static void ensure_encrypted(int peer_idx)
{
    br_peer_t *p = &s_peers[peer_idx];
    if (!p->connected) {
        return;
    }

    struct ble_gap_conn_desc desc;
    if (ble_gap_conn_find(p->conn_handle, &desc) == 0 &&
        desc.sec_state.encrypted) {
        p->encrypted = true;
        ESP_LOGI(TAG, "peer %d 已加密（keysize=%d）", peer_idx,
                 desc.sec_state.key_size);
        open_channels(peer_idx);
        return;
    }

    if (p->sec_attempted) {
        /* 协商过了还没加密 -> 这台不打算加密，裸着订阅（CMCC 那台就是这样） */
        open_channels(peer_idx);
        return;
    }
    p->sec_attempted = true;

    /*
     * ★★ 有 bond 却没加密上 = 密钥跟对端对不上，别去戳它 ★★
     *
     * CMCC 的结论（reference/cmcc-src/firmware/cmcc_s3_bridge_v4voice_20261031_2226/
     * cmcc_s3_bridge_v4voice_20261031_2226.ino:1401）：
     *   「这里曾经主动发起过配对（BLESecurity::startSecurity），
     *     结果链路在 5.5 秒后被遥控器断掉。原生是"连上就能用"，
     *     说明遥控器不希望被强制走安全流程，所以这里不做任何配对动作。」
     *
     * 我们这边是同一个坑的同一个症状（真机日志 16:17）：
     *   GATTDONE -> EV SEC rc=0 -> 2.8~6.4 秒 -> EV DISC reason=520(=0x208)
     * 而且 CMCC 还给出了 0x208 的真正含义（同文件 1863 行）：
     *   「之前几轮实验留下的密钥已经不匹配了，不清掉的话……加密失败
     *     （GAP ENC status=0x208），遥控器直接把链路掐掉。」
     *
     * ★ 关键区别 ★
     *   CMCC 踩坑时是**拿一把废钥匙去协商**。
     *   我们这边 0.9.6/0.9.7 学它「不协商」，结果走进另一个死胡同：
     *     - 链路是通的（电量读每秒都有回应）
     *     - 订阅也写成功了（ok=6）
     *     - 但链路**没加密**，而 HID 输入报告要求加密链路
     *       → 一个通知都收不到 → 灯不亮、键学不到
     *   真机日志原话：
     *     W br_ble: peer 0 有 bond 但链路没加密：判定为陈旧密钥，
     *               本轮不再发起安全协商
     *
     * 所以正确做法是：**先把废钥匙删掉，再发起一次干净的配对**。
     * 遥控器在配对模式下正等着有人来配；
     * 不在配对模式时它会拒绝，那就退回 0x208 自愈
     * （断链处理器清 bond），下一轮再来。
     */
    if (peer_has_bond(p->addr)) {
        ESP_LOGW(TAG,
                 "peer %d 有 bond 但链路没加密：当陈旧密钥清掉，"
                 "本轮发起一次干净的配对", peer_idx);
        peer_drop_bond(p->addr);
    }

    int rc = ble_gap_security_initiate(p->conn_handle);
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "idx", peer_idx);
    br_kv_add_int(&kv, "rc", rc);
    br_kv_add(&kv, "why", hs_err_name(rc));
    br_proto_send_ev("SEC", &kv);
    ESP_LOGI(TAG, "发起加密协商 peer=%d rc=%d(%s)", peer_idx, rc,
             hs_err_name(rc));
    if (rc != 0) {
        /* 没有回调可等，立刻按不加密订阅 */
        open_channels(peer_idx);
    }
}

/**
 * ★ 请求连接参数：把 supervision timeout 拉长 ★
 *
 * NimBLE 默认在 LE_Create_Connection 里带的是
 *   itvl_min=24 itvl_max=40 latency=0 supervision_timeout=256
 * 真机日志（NimBLE 自己打的）：
 *   NimBLE: scan_itvl=16 scan_window=16 itvl_min=24 itvl_max=40
 *           latency=0 supervision_timeout=256 min_ce_len=0 max_ce_len=0
 *
 * supervision_timeout=256 的单位是 10ms → **2.56 秒**。
 * 而我们的链路恰好在 **2.7 秒**后以 0x208 断掉 —— 一模一样。
 *
 * 也就是说：遥控器不主动发通知的那 2.5 秒里，链路就被判超时断掉了。
 * 「订阅还没写完，链路先没了」，后面所有事都无从谈起。
 *
 * CMCC 的做法（reference/cmcc-src/firmware/cmcc_s3_bridge_v4voice_20261031_2226/
 * cmcc_s3_bridge_v4voice_20261031_2226.ino:856）：
 *   requestConnParams(24, 24, 50, 500, 0, 0xFFFF)
 *     连接间隔 30ms、slave latency 50、supervision timeout 5 秒
 * → 能容忍 5 秒 + 50×30ms ≈ 6.5 秒的静默。
 *
 * 这里照抄它那组参数（单位：itvl 是 1.25ms，timeout 是 10ms）。
 */
static void request_conn_params(br_peer_t *p)
{
    struct ble_gap_upd_params params;
    memset(&params, 0, sizeof(params));
    params.itvl_min = 24;              /* 30ms */
    params.itvl_max = 24;              /* 30ms */
    params.latency = 50;               /* 从机可以偷懒 50 个事件 */
    params.supervision_timeout = 500;  /* 5 秒 */
    params.min_ce_len = 0;
    params.max_ce_len = 0;

    int rc = ble_gap_update_params(p->conn_handle, &params);
    ESP_LOGI(TAG, "请求连接参数 itvl=30ms lat=50 to=5s rc=%d", rc);
    if (rc != 0) {
        /* 对端可能拒绝；不重试，靠 HID 的保活读兜底 */
        ESP_LOGW(TAG, "连接参数请求失败 rc=%d", rc);
    }
}

/* ------------------------------------------------------------ 连接与事件 */

#define MAX_BONDED 16

/**
 * ★ 这台设备在本机有没有存过密钥（BLE bond）★
 *
 * 「有 bond」和「链路已加密」是两件事：
 *   - 有 bond 但链路没加密上 → 密钥跟对端对不上（陈旧），
 *     这时候再主动去戳安全协商，遥控器会直接掐链（CMCC 实测）。
 *   - 没有 bond → 只能靠一次干净的配对来建立。
 */
static bool peer_has_bond(const uint8_t *addr)
{
    ble_addr_t bonded[MAX_BONDED];
    int num = 0;
    if (ble_store_util_bonded_peers(bonded, &num, MAX_BONDED) != 0) {
        return false;
    }
    for (int i = 0; i < num; i++) {
        if (memcmp(bonded[i].val, addr, 6) == 0) {
            return true;
        }
    }
    return false;
}

/**
 * ★ 丢掉某台设备的陈旧密钥 ★
 *
 * 只在断链原因是 0x208（加密失败）时调用：
 * 留着它只会让每一轮重连都拿废钥匙去协商，然后又被断一次。
 */
static bool peer_drop_bond(const uint8_t *addr)
{
    ble_addr_t bonded[MAX_BONDED];
    int num = 0;
    if (ble_store_util_bonded_peers(bonded, &num, MAX_BONDED) != 0) {
        return false;
    }
    for (int i = 0; i < num; i++) {
        if (memcmp(bonded[i].val, addr, 6) == 0) {
            int rc = ble_store_util_delete_peer(&bonded[i]);
            char addr_s[24];
            addr_to_str(addr, addr_s, sizeof(addr_s));
            ESP_LOGW(TAG, "清掉陈旧 bond %s rc=%d（下一轮重新配对）",
                     addr_s, rc);
            return rc == 0;
        }
    }
    return false;
}

/**
 * 连接事件回调。
 *
 * ★ ble_gap_connect 的 cb 参数不能传 NULL ★
 *   MTU 协商、通知（NOTIFY_RX）等事件也会送到这个回调。
 *
 * ★ 签名必须是 int (*)(struct ble_gap_event *, void *) ★
 */
static void pair_window_close(void);

/* NVS 配对表（定义在文件后面的「已配对遥控器表」一节） */
static bool pl_get(int i, uint8_t addr[6], uint8_t *type,
                   char *name, size_t name_cap);
static int  pl_count(void);
static bool pl_put(const uint8_t *addr, uint8_t type, const char *name,
                   const uint8_t *id_addr, uint8_t id_type, bool has_id);
static int  pl_find(const uint8_t *addr);
static int  pl_find_by_id(const uint8_t *id);
static bool pl_get_profile(int i, char *out, size_t cap);
static bool pl_set_profile(int i, const char *id);
static void drop_orphan_bonds(void);
static bool pl_set_id(const uint8_t *addr, const uint8_t *id_addr,
                      uint8_t id_type);

static int conn_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;

    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT: {
        s_connect_in_progress = false;

        if (event->connect.status != 0) {
            s_last_conn_status = event->connect.status;
            char why[64];
            conn_status_desc(event->connect.status, why, sizeof(why));
            ESP_LOGE(TAG, "connect failed: %s", why);
            s_bond_fail_streak++;
            /*
             * ★ 配对期间不在这里报失败，也不放弃 ★
             *   遥控器多半在睡觉 —— 一次连不上不代表配不了。
             *   交给主循环的重试逻辑：窗口耗尽才报失败。
             */
            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add_int(&kv, "status", event->connect.status);
            br_kv_add(&kv, "why", why);
            br_proto_send_ev("CONNFAIL", &kv);
            return 0;
        }
        s_last_conn_status = 0;

        uint16_t h = event->connect.conn_handle;

        /* connect 事件里拿不到 peer_addr —— 用我们自己记的 */
        int idx = -1;
        for (int i = 0; i < BR_MAX_PEERS; i++) {
            if (s_peers[i].used && !s_peers[i].connected &&
                memcmp(s_peers[i].addr, s_connecting_addr, 6) == 0) {
                idx = i;
                break;
            }
        }
        if (idx < 0) {
            idx = alloc_peer();
        }
        if (idx < 0) {
            ESP_LOGE(TAG, "no free peer slot");
            return 0;
        }

        br_peer_t *p = &s_peers[idx];
        p->connected = true;
        p->conn_handle = h;
        p->addr_type = s_connecting_type;
        memcpy(p->addr, s_connecting_addr, 6);
        p->last_seen_us = esp_timer_get_time();
        /*
         * ★ 记住 identity 地址（固定 MAC）★
         *   认人（PC 端按 MAC 归档映射）、删 bond 都靠它。
         */
        {
            struct ble_gap_conn_desc desc;
            {
                struct ble_gap_conn_desc d0;
                int rc0 = ble_gap_conn_find(h, &d0);
                char a0[24];
                addr_to_str(d0.peer_id_addr.val, a0, sizeof(a0));
                char o0[24];
                addr_to_str(d0.peer_ota_addr.val, o0, sizeof(o0));
                ESP_LOGI(TAG, "conn %u desc rc=%d id_type=%u id=%s ota_type=%u ota=%s",
                         h, rc0, d0.peer_id_addr.type, a0,
                         d0.peer_ota_addr.type, o0);
            }
            bool zero = true;
            for (int k = 0; k < 6; k++) {
                if (desc.peer_id_addr.val[k] != 0) {
                    zero = false;
                    break;
                }
            }
            if (ble_gap_conn_find(h, &desc) == 0 && !zero &&
                (desc.peer_id_addr.type == 0 ||
                 desc.peer_id_addr.type == 1)) {
                memcpy(p->id_addr, desc.peer_id_addr.val, 6);
                p->id_type = desc.peer_id_addr.type;
                p->has_id = true;
            } else {
                p->has_id = false;
            }
        }
        p->disc_state = BR_DISC_NONE;
        p->svc_count = 0;
        p->chr_count = 0;
        p->atvv_seen = false;
        p->atvv_audio_handle = 0;
        p->atvv_ctl_handle = 0;
        p->atvv_tx_handle = 0;
        p->atvv_audio_cccd = 0;
        p->atvv_ctl_cccd = 0;
        p->atvv_sub_step = 0;
        p->sec_attempted = false;
        p->encrypted = false;
        p->channels_open = false;
        p->channels_enc = false;
        if (s_connecting_name[0]) {
            snprintf(p->name, sizeof(p->name), "%s", s_connecting_name);
        }

        char addr[24];
        addr_to_str(p->addr, addr, sizeof(addr));
        ESP_LOGI(TAG, "connected: %s handle=%u idx=%d", addr, h, idx);
        bridge_led_set_connected(true);
        /*
         * ★ 这台遥控器在板子上一条映射都没有？铺一套出厂默认 ★
         *   全新刷写的板子 NVS 是空的，不铺的话插上什么键都不动。
         *   这里**只排队** —— 真正写 NVS 由主循环做
         *   （在 GAP 回调里写 flash 会把 NimBLE host 任务卡住，遥控器会断链）。
         *   用户配过（count>0）时 br_mapping_apply_defaults 直接返回，绝不会覆盖。
         */
        p->map_addr_valid = false;   /* 规范映射地址由主循环解析 */
        queue_defaults(idx, p->name);
        bridge_led_set_pairing(false);
        remember_name(p->name);
        s_bond_fail_streak = 0;

        if (s_pair_pending) {
            pair_window_close();

            /*
             * ★ 这里才是「配对成功」的落库点 ★
             *
             * 记进 NVS 配对表：断电重启还在，重连和 REMOTES 列表都靠它。
             * bond 表能不能建成不看这里 —— 遥控器不加密也让我们订阅，
             * 而主动发起 SMP 有把链路搞断的先例（CMCC 实测 5.5 秒被断），
             * 所以不在这里赌。
             */
            pl_put(p->addr, p->addr_type, p->name, p->id_addr, p->id_type,
                    p->has_id);

            br_kv_t pkv;
            br_kv_init(&pkv);
            br_kv_add_int(&pkv, "idx", idx);
            br_kv_add(&pkv, "addr", addr);
            br_kv_add_int(&pkv, "type", p->addr_type);
            br_kv_add(&pkv, "name", p->name);
            br_kv_add_int(&pkv, "ok", 1);
            br_proto_send_ev("PAIR", &pkv);
            ESP_LOGI(TAG, "PAIR ok: %s '%s'", addr, p->name);
        }

        /*
         * ★ 连上了就把「新鲜广播待连」标记清掉 ★
         *   它是个闩锁：不清的话 STATE 里永远显示 fresh=1，
         *   排查时会被误导成「还有一次没发出去的连接排队」。
         */
        s_pair_fresh_ready = false;

        br_kv_t kv;
        br_kv_init(&kv);
        br_kv_add_int(&kv, "idx", idx);
        br_kv_add(&kv, "addr", addr);
        br_kv_add_int(&kv, "type", p->addr_type);
        br_kv_add(&kv, "name", p->name);
        br_kv_add_int(&kv, "handle", h);
        br_proto_send_ev("CONN", &kv);

        /* 连接后必须显式发 ATT_MTU_REQ */
        ble_gattc_exchange_mtu(h, NULL, NULL);

        /*
         * ★ 连上了就停掉扫描 ★
         *   扫描和连接同时跑会抢控制器（EBUSY / EEXIST），
         *   遥控器判定拥塞就会断链 —— 实测「连上了但三秒就断」就是这个。
         */
        if (s_scanning) {
            br_ble_scan_stop();
        }

        /* ★ 拉长链路的 supervision timeout ★（详见 request_conn_params） */
        p->params_pending = true;

        /* 开始全量服务发现（由主循环发起，避免在 GAP 回调里嵌套过程） */
        p->disc_state = BR_DISC_NEXT_SVC;
        return 0;
    }

    case BLE_GAP_EVENT_IDENTITY_RESOLVED: {
        /*
         * ★ identity 地址（固定 MAC）到手的时刻 ★
         *   BLE 私有地址要等控制器解析（或者刚配对完）才知道真正的 MAC。
         *   认人、删 bond、按 MAC 归档映射全靠它。
         */
        int idx = find_peer_by_conn(event->identity_resolved.conn_handle);
        if (idx >= 0) {
            br_peer_t *p = &s_peers[idx];
            memcpy(p->id_addr, event->identity_resolved.peer_id_addr.val, 6);
            p->id_type = event->identity_resolved.peer_id_addr.type;
            p->has_id = true;
            char id_s[24];
            addr_to_str(p->id_addr, id_s, sizeof(id_s));
            ESP_LOGI(TAG, "peer %d identity=%s", idx, id_s);
            pl_set_id(p->addr, p->id_addr, p->id_type);
            /*
             * identity 到手了 → 规范映射地址要重算
             *   （之前查不到配对表条目，只能用本次连接的广播地址）。
             *   顺带再排一次默认映射：这时才知道它是不是有档案的型号。
             */
            p->map_addr_valid = false;
            queue_defaults(idx, p->name);
        }
        return 0;
    }

    case BLE_GAP_EVENT_DISCONNECT: {
        s_connect_in_progress = false;
        int idx = find_peer_by_conn(event->disconnect.conn.conn_handle);
        if (idx >= 0) {
            s_peers[idx].connected = false;
            s_peers[idx].disc_state = BR_DISC_NONE;
            /*
             * ★ HID 上下文必须跟着断链一起清 ★
             *   不清的话，上一轮停在 STEP_TABLE 的上下文会继续驱动这一轮，
             *   拿上一轮的句柄去读表 → 读到空表 → 直接放弃订阅。
             */
            br_hid_peer_reset(idx);
            char addr[24];
            addr_to_str(s_peers[idx].addr, addr, sizeof(addr));
            ESP_LOGI(TAG, "disconnected: %s reason=%d", addr,
                     event->disconnect.reason);
            bridge_led_set_connected(false);
            char why[64];
            conn_status_desc(event->disconnect.reason, why, sizeof(why));
            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add_int(&kv, "idx", idx);
            br_kv_add(&kv, "addr", addr);
            br_kv_add_int(&kv, "reason", event->disconnect.reason);
            br_kv_add(&kv, "why", why);
            br_proto_send_ev("DISC", &kv);

            /*
             * ★ reason 0x208 = BLE_HS_HCI_ERR(0x08) ★
             *
             * CMCC 的记录（reference/cmcc-src/firmware/.../cmcc_s3_bridge_v4voice...ino:1863）：
             *   「之前几轮实验留下的密钥已经不匹配了，不清掉的话订阅 0x03 时
             *     加密失败（GAP ENC status=0x208），遥控器直接把链路掐掉。」
             * 我们这边一模一样：GATTDONE → 发起协商 → 2.8~6.4 秒后 0x208 断链。
             *
             * 所以 0x208 一律按「密钥对不上」处理：清掉本机存的这把密钥，
             * 下一轮连接就能走一次干净的配对，而不是拿一把废钥匙反复撞。
             */
            if (event->disconnect.reason == BLE_HS_HCI_ERR(0x08)) {
                if (peer_drop_bond(s_peers[idx].addr)) {
                    br_kv_t bkv;
                    br_kv_init(&bkv);
                    br_kv_add_int(&bkv, "idx", idx);
                    br_kv_add_int(&bkv, "reason", event->disconnect.reason);
                    br_kv_add(&bkv, "why", "stale-bond-dropped");
                    br_proto_send_ev("BNDSTALE", &bkv);
                }
            }
        }
        return 0;
    }

    /*
     * 加密协商结果。
     *
     * ★ 不管成功失败都要再订阅一次 ★
     *   成功：之前那些不加密状态下写的 CCCD 多半被设备忽略了，重新写才算数；
     *   失败：说明这台压根不打算加密（CMCC 那台就是这样），裸着订阅照样能收。
     */
    case BLE_GAP_EVENT_ENC_CHANGE: {
        int idx = find_peer_by_conn(event->enc_change.conn_handle);
        uint16_t st = event->enc_change.status;
        char desc[64];
        conn_status_desc(st, desc, sizeof(desc));
        ESP_LOGI(TAG, "ENC_CHANGE handle=%u status=%u(%s)",
                 event->enc_change.conn_handle, st, desc);
        if (idx >= 0) {
            s_peers[idx].encrypted = (st == 0);
            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add_int(&kv, "idx", idx);
            br_kv_add_int(&kv, "status", st);
            br_kv_add(&kv, "why", desc);
            br_kv_add_int(&kv, "enc", st == 0 ? 1 : 0);
            br_proto_send_ev("ENC", &kv);
            open_channels(idx);
        }
        return 0;
    }

    case BLE_GAP_EVENT_MTU: {
        int idx = find_peer_by_conn(event->mtu.conn_handle);
        if (idx >= 0) {
            ESP_LOGI(TAG, "MTU negotiated: %u", event->mtu.value);
            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add_int(&kv, "idx", idx);
            br_kv_add_int(&kv, "mtu", event->mtu.value);
            br_proto_send_ev("MTU", &kv);
        }
        return 0;
    }

    case BLE_GAP_EVENT_NOTIFY_RX: {
        /*
         * 收到远端通知。
         * ★ 这里只做一次 memcpy + 入队，**绝不做别的** ★
         *   在 ATT 回调里阻塞会拖慢 ATT 确认，遥控器会判定拥塞主动断链
         *   （CMCC 实测：回调里 Serial.print，8 秒只收到 1.4 秒音频）。
         */
        uint16_t h = event->notify_rx.conn_handle;
        uint16_t attr = event->notify_rx.attr_handle;
        struct os_mbuf *om = event->notify_rx.om;

        int idx = find_peer_by_conn(h);
        if (idx < 0 || om == NULL || !s_auq) {
            return 0;
        }

        uint16_t len = OS_MBUF_PKTLEN(om);
        if (len == 0 || len > AU_ITEM_MAX) {
            return 0;
        }
        /* Audio packets use the queued data path; avoid per-packet log traffic. */

        /* HID reports are short; pull up only this packet's real length.
         * Requesting AU_ITEM_MAX here rejects every short notification. */
        struct os_mbuf *dup = os_mbuf_pullup(om, len);
        if (!dup || OS_MBUF_PKTLEN(dup) == 0) {
            return 0;
        }
        uint16_t n = OS_MBUF_PKTLEN(dup);
        if (n > AU_ITEM_MAX) {
            n = AU_ITEM_MAX;
        }

        /*
         * ★ HID 输入报告先交给 HID 层解析（不阻塞、只做位比较）★
         */
        if (s_peers[idx].atvv_ctl_handle == attr) {
            ESP_LOG_BUFFER_HEX_LEVEL(TAG, OS_MBUF_DATA(dup, uint8_t *), n, ESP_LOG_INFO);
        }        br_hid_on_notify(idx, attr, OS_MBUF_DATA(dup, uint8_t *), n);

        au_item_t item;
        memset(&item, 0, sizeof(item));
        item.attr = attr;
        item.len = n;
        memcpy(item.data, OS_MBUF_DATA(dup, uint8_t *), n);

        /* 队列满：丢最旧的一条，优先保新数据（音频流后面才是当前语音） */
        if (xQueueSend(s_auq, &item, 0) != pdTRUE) {
            au_item_t drop;
            xQueueReceive(s_auq, &drop, 0);
            xQueueSend(s_auq, &item, 0);
        }
        return 0;
    }

    default:
        return 0;
    }
}

/* ------------------------------------------------------------ 连接发起 */

bool br_ble_connect_addr(const uint8_t *addr, uint8_t addr_type,
                          const char *name)
{
    if (s_connect_in_progress) {
        return false;
    }

    /*
     * ★ 先占住连接槽，再停扫描 ★
     *
     * 顺序反了会出事：
     *   停扫描 -> 等 120ms 让控制器收尾 -> 这期间主循环任务跑到调度器，
     *   看到「没在扫描」又把扫描拉起来 -> 然后 ble_gap_connect 撞上 EBUSY。
     *
     * 所以先把 s_connect_in_progress 置上，调度器就不会再动扫描。
     */
    s_connect_in_progress = true;

    /* 扫描与连接严格串行：连接前先确保扫描停下来 */
    if (s_scanning) {
        int drc = ble_gap_disc_cancel();
        s_scanning = false;
        ESP_LOGI(TAG, "连接前停扫描: disc_cancel rc=%d, 等 %dms 让控制器收尾",
                 drc, CONNECT_SETTLE_MS);
        vTaskDelay(pdMS_TO_TICKS(CONNECT_SETTLE_MS));
    }

    ble_addr_t a;
    char addr_str[24];
    memset(&a, 0, sizeof(a));
    a.type = addr_type;
    memcpy(a.val, addr, 6);
    addr_to_str(addr, addr_str, sizeof(addr_str));

    memcpy(s_connecting_addr, addr, 6);
    s_connecting_type = addr_type;
    sanitize_name(name ? name : "", s_connecting_name,
                  sizeof(s_connecting_name));

    bridge_led_set_connected(false);

    /*
     * ★ 第一个参数是**本机**地址类型；远端地址类型走 peer_addr.type。
     *   用扫描到的对象连接，地址类型必然真实 —— 配置里存的 addr_type 不可信。
     *
     * ★ own 类型要打成数字，不能写成"PUBLIC"这种自述 ★
     *   NimBLE 的日志里出现过 own_addr_type=1，而这里写死 own=PUBLIC，
     *   两边对不上就说明"我以为我传了 0"这件事本身不可信。
     */
    int rc = ble_gap_connect(OWN_ADDR_TYPE_FOR_CONNECT, &a,
                             CONNECT_TIMEOUT_MS, NULL, conn_event, NULL);
    if (rc != 0) {
        s_connect_in_progress = false;
        ESP_LOGE(TAG, "ble_gap_connect rc=%d(%s) own=%d peer=%s type=%d",
                 rc, hs_err_name(rc), OWN_ADDR_TYPE_FOR_CONNECT,
                 addr_str, addr_type);
        br_kv_t kv;
        br_kv_init(&kv);
        br_kv_add_int(&kv, "rc", rc);
        br_kv_add(&kv, "why", hs_err_name(rc));
        br_proto_send_ev("CONNFAIL", &kv);
        return false;
    }

    ESP_LOGI(TAG, "connect requested: peer=%s type=%d own=%d t/o=%dms",
             addr_str, addr_type, OWN_ADDR_TYPE_FOR_CONNECT,
             CONNECT_TIMEOUT_MS);
    return true;
}

bool br_ble_connect_candidate(int cand_idx)
{
    if (cand_idx < 0 || cand_idx >= s_candidate_count) {
        return false;
    }
    br_candidate_t *c = &s_candidates[cand_idx];
    return br_ble_connect_addr(c->addr, c->addr_type, c->name);
}

void br_ble_disconnect_peer(int idx)
{
    if (idx < 0 || idx >= BR_MAX_PEERS || !s_peers[idx].connected) {
        return;
    }
    ble_gap_terminate(s_peers[idx].conn_handle, BLE_ERR_REM_USER_CONN_TERM);
}

/* -------------------------------------------------------------- 自动模式 */

/*
 * ★ bond 表枚举 ★
 *
 * 遥控器空闲时**根本不广播**（实测：扫 40 秒 160 个设备里一个都没有），
 * 所以「配对过一次」的设备只能靠 bond 表里的 identity address 主动连。
 * 这也是自主运行（P7）的基础。
 */
#define MAX_BONDED 16

/*
 * ================================================================
 * ★ 已配对遥控器表（NVS）★
 * ================================================================
 *
 * 为什么「已配对」不能只看 bond 表（2026-11-06 实测）：
 *   PAIR 成功后链路一直很稳 —— DT PEER conn=1 disc=7 svcs=8 atvv=1 连续 65 秒+，
 *   ATVV 语音通道就绪，但 REMOTES 是空的。原因是全流程里没有任何 SMP 交互，
 *   bond 表 count=0；而遥控器**不加密也允许我们订阅它的特征**。
 *   CMCC 那条教训还在：连上就主动 startSecurity，结果 5.5 秒后被遥控器断链。
 *   所以不能为了让列表有东西就去赌链路。
 *
 * 这张表自己记「地址 + 地址类型 + 名字」：
 *   - 地址类型来自**扫描到的广播**，是权威值，重连时不用再猜
 *   - 断电重启还在，P7 自主运行靠它找目标
 *   - UNPAIR 删这张表，顺带清 bond（如果真有）
 *
 * 存储格式：namespace "brpair"，键 e0..e7，值 "AA:BB:CC:DD:EE:FF,<type>,<name>"。
 * 直接用串口读 NVS 不方便，所以另有一条 LG 输出（见 REMOTES 命令）。
 */
#define PL_NAMESPACE  "brpair"
#define PL_MAX        BR_MAX_PEERS

/*
 * ★ 表住在 RAM，NVS 只负责持久化 ★
 *
 * pl_count()/pl_get() 会被主循环每 5 秒（EV STATE）和每次重连尝试调用，
 * 而 nvs_open/nvs_get_str 是带锁的 flash 读 —— 一趟几毫秒、十几趟就是几十毫秒，
 * 恰好卡在扫描与连接的调度路径上。这个项目已经被「热路径被拖慢」坑过一次
 * （配对窗口里扫描整整饿死两分钟），所以热路径一次 flash 都不碰。
 * 只有改表（配对成功 / 解绑，都是人操作的低频路径）才落 NVS。
 */
typedef struct {
    bool     used;
    uint8_t  addr[6];
    uint8_t  type;
    /*
     * ★ identity 地址（固定 MAC）★
     *   单独存在 NVS 的 i<idx> 键里 —— 老的 e<idx> 值格式不动，
     *   这样升级后老记录照样读得出来（缺 identity 就拿广播地址顶上）。
     */
    uint8_t  id_addr[6];
    uint8_t  id_type;
    bool     has_id;
    char     name[BR_MAX_NAME_LEN];
    /*
     * ★ 命中的默认档案 id（如 "chromecast"）★
     *   单独存在 NVS 的 p<idx> 键里 —— e<idx> / i<idx> 的格式都不动，
     *   老记录照样读得出来（没有这个键就按名字现匹配）。
     *   作用：同一台遥控器重新配对时用回原来那套默认，
     *   不因为地址轮换了就换一套键位。
     */
    char     profile[16];
} pl_entry_t;

static pl_entry_t s_pl[PL_MAX];
static bool       s_pl_loaded;

static void pl_key(int i, char *out, size_t cap)
{
    snprintf(out, cap, "e%d", i);
}

/**
 * 解析 "AA:BB:CC:DD:EE:FF,<type>,<name>"。
 * 名字里可能有空格，所以只切前两刀，剩下的整段都是名字。
 */
static bool pl_parse(char *s, uint8_t addr[6], uint8_t *type,
                    const char **name_out)
{
    char *comma = strchr(s, ',');
    if (comma == NULL) {
        return false;
    }
    *comma = '\0';
    char *type_s = comma + 1;
    char *name_s = strchr(type_s, ',');
    if (name_s == NULL) {
        return false;
    }
    *name_s = '\0';
    name_s++;

    if (!br_ble_addr_parse(s, addr)) {
        return false;
    }
    int t = atoi(type_s);
    if (t < 0 || t > 1) {
        return false;
    }
    *type = (uint8_t)t;
    *name_out = name_s;
    return true;
}

static void pl_ensure_loaded(void)
{
    if (s_pl_loaded) {
        return;
    }
    /* 先置位：读失败也只撞一次 flash，不让每次调用都重试 */
    s_pl_loaded = true;
    memset(s_pl, 0, sizeof(s_pl));

    nvs_handle_t h;
    if (nvs_open(PL_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "配对表还没有内容（NVS namespace 未建）");
        return;
    }
    int n = 0;
    for (int i = 0; i < PL_MAX; i++) {
        char key[16];
        pl_key(i, key, sizeof(key));
        char buf[128];
        size_t sz = sizeof(buf);
        if (nvs_get_str(h, key, buf, &sz) != ESP_OK) {
            continue;
        }
        const char *name = "";
        uint8_t type = 0;
        if (!pl_parse(buf, s_pl[i].addr, &type, &name)) {
            ESP_LOGW(TAG, "配对表[%d] 内容坏了：%s", i, buf);
            continue;
        }
        s_pl[i].used = true;
        s_pl[i].type = type;
        snprintf(s_pl[i].name, sizeof(s_pl[i].name), "%s", name);
        /* identity 地址在单独的键 i<idx> 里；老记录没这个键也照样能用 */
        char ikey[16];
        snprintf(ikey, sizeof(ikey), "i%d", i);
        char ibuf[32];
        size_t isz = sizeof(ibuf);
        if (nvs_get_str(h, ikey, ibuf, &isz) == ESP_OK) {
            /* 存的是 "<地址>,<类型>" */
            char *comma = strrchr(ibuf, ',');
            if (comma) {
                *comma = '\0';
                int it = atoi(comma + 1);
                if (it >= 0 && it <= 1 && br_ble_addr_parse(ibuf, s_pl[i].id_addr)) {
                    s_pl[i].id_type = (uint8_t)it;
                    s_pl[i].has_id = true;
                }
            }
        }
        /* 默认档案 id（p<idx>）：老记录没这个键也不影响 */
        char pkey[16];
        snprintf(pkey, sizeof(pkey), "p%d", i);
        char pbuf[16];
        size_t psz = sizeof(pbuf);
        if (nvs_get_str(h, pkey, pbuf, &psz) == ESP_OK) {
            snprintf(s_pl[i].profile, sizeof(s_pl[i].profile), "%s", pbuf);
        }
        n++;
    }
    nvs_close(h);
    ESP_LOGI(TAG, "配对表已载入：%d 条", n);
}

/** 整表落盘（只在改表后调用） */
static bool pl_flush(void)
{
    nvs_handle_t h;
    esp_err_t rc = nvs_open(PL_NAMESPACE, NVS_READWRITE, &h);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open rc=%d", rc);
        return false;
    }
    for (int i = 0; i < PL_MAX; i++) {
        char key[16];
        pl_key(i, key, sizeof(key));
        char ikey[16];
        snprintf(ikey, sizeof(ikey), "i%d", i);
        if (!s_pl[i].used) {
            nvs_erase_key(h, key);      /* 不存在时返回错误，忽略即可 */
            nvs_erase_key(h, ikey);
            continue;
        }
        char addr_s[24];
        char val[128];
        addr_to_str(s_pl[i].addr, addr_s, sizeof(addr_s));
        snprintf(val, sizeof(val), "%s,%d,%s", addr_s, s_pl[i].type,
                 s_pl[i].name);
        rc = nvs_set_str(h, key, val);
        if (rc != ESP_OK) {
            ESP_LOGE(TAG, "nvs_set_str[%d] rc=%d", i, rc);
            nvs_close(h);
            return false;
        }
        /* identity（固定 MAC）单独存一份 */
        if (s_pl[i].has_id) {
            char id_s[32];
            char id_a[24];
            addr_to_str(s_pl[i].id_addr, id_a, sizeof(id_a));
            snprintf(id_s, sizeof(id_s), "%s,%d", id_a, s_pl[i].id_type);
            nvs_set_str(h, ikey, id_s);
        } else {
            nvs_erase_key(h, ikey);
        }
        /* 默认档案 id 也单独存一份（p<idx>） */
        char pkey[16];
        snprintf(pkey, sizeof(pkey), "p%d", i);
        if (s_pl[i].profile[0]) {
            nvs_set_str(h, pkey, s_pl[i].profile);
        } else {
            nvs_erase_key(h, pkey);
        }
    }
    rc = nvs_commit(h);
    nvs_close(h);
    return rc == ESP_OK;
}

/** 读第 i 条（纯 RAM，热路径可放心调用） */
static bool pl_get(int i, uint8_t addr[6], uint8_t *type,
                   char *name, size_t name_cap)
{
    pl_ensure_loaded();
    if (i < 0 || i >= PL_MAX || !s_pl[i].used) {
        return false;
    }
    memcpy(addr, s_pl[i].addr, 6);
    *type = s_pl[i].type;
    snprintf(name, name_cap, "%s", s_pl[i].name);
    return true;
}

/**
 * 读第 i 条的 identity 地址（固定 MAC）。
 * 没有 identity（老记录）时 has_id=false —— 调用方拿广播地址顶上。
 */
static bool pl_get_id(int i, uint8_t out[6], uint8_t *out_type,
                      bool *has_id)
{
    pl_ensure_loaded();
    if (i < 0 || i >= PL_MAX || !s_pl[i].used) {
        return false;
    }
    if (s_pl[i].has_id && out) {
        memcpy(out, s_pl[i].id_addr, 6);
    }
    if (out_type) {
        *out_type = s_pl[i].id_type;
    }
    if (has_id) {
        *has_id = s_pl[i].has_id;
    }
    return true;
}

/**
 * 读这台遥控器上次用的默认档案 id（没有就返回空串）。
 */
static bool pl_get_profile(int i, char *out, size_t cap)
{
    pl_ensure_loaded();
    if (i < 0 || i >= PL_MAX || !s_pl[i].used || !out || cap == 0) {
        return false;
    }
    snprintf(out, cap, "%s", s_pl[i].profile);
    return true;
}

/**
 * 记下这台遥控器用的默认档案 id（落盘）。
 * 同一台重配时用回它 —— 「型号 + MAC」分档就落在这里。
 */
static bool pl_set_profile(int i, const char *id)
{
    pl_ensure_loaded();
    if (i < 0 || i >= PL_MAX || !s_pl[i].used) {
        return false;
    }
    snprintf(s_pl[i].profile, sizeof(s_pl[i].profile), "%s", id ? id : "");
    return pl_flush();
}

static int pl_count(void)
{
    pl_ensure_loaded();
    int n = 0;
    for (int i = 0; i < PL_MAX; i++) {
        if (s_pl[i].used) {
            n++;
        }
    }
    return n;
}

/**
 * 清理孤 bond。
 *
 * ★ 这就是「删掉配对后再配对一直连不上」的根因 ★
 *
 *   配对表（pl）是「已配对遥控器」的权威名单，
 *   bond 表只是 NimBLE 存着的密钥。
 *   名单空了、bond 还留着 →
 *     · 板子一直去连那几台（连上也用不上，因为不在名单里）
 *     · 遥控器那边也还留着旧 bond → 重新配对时双方都有旧密钥，
 *       配对流程走不下去，表现为「板子闪蓝灯、遥控器进配对模式，
 *       两者一直建不起连接」，只能两个口都拔掉重插。
 *
 *   所以：名单一空，就把没有对应名单的 bond 全部清掉。
 */
/*
 * ★ 这段（清孤 bond）撤掉了 ★
 *
 *   原来的想法：配对表空了就把没对应的 bond 清掉。
 *   实测两处不对：
 *     · 配对表空是**暂时的**（用户刚删掉、正在重新配对），
 *       这时把 bond 清了，板子就再也找不到那台遥控器了；
 *     · 刚配对成功的那一瞬，配对表还没写进去，
 *       bond 先被清掉 → 配了个寂寞。
 *   bond 表正是「板子怎么找到遥控器」的凭据，**不能随便清**。
 */
static void drop_orphan_bonds(void)
{
    /* 保留：不做任何清理。 */
}

/**
 * identity 地址后到时补进配对表。
 * BLE 私有地址要等控制器解析完才知道 identity，
 * 所以这条路径比「连接成功时顺手记一下」更常见。
 */
static bool pl_set_id(const uint8_t *addr, const uint8_t *id_addr,
                      uint8_t id_type)
{
    pl_ensure_loaded();
    int slot = pl_find(addr);
    if (slot < 0 || !id_addr) {
        return false;
    }
    if (s_pl[slot].has_id &&
        memcmp(s_pl[slot].id_addr, id_addr, 6) == 0) {
        return true;
    }
    memcpy(s_pl[slot].id_addr, id_addr, 6);
    s_pl[slot].id_type = id_type;
    s_pl[slot].has_id = true;
    char id_s[24];
    addr_to_str(id_addr, id_s, sizeof(id_s));
    ESP_LOGI(TAG, "配对表[%d] 记下 identity %s", slot, id_s);
    return pl_flush();
}

static int pl_find(const uint8_t *addr)
{
    pl_ensure_loaded();
    for (int i = 0; i < PL_MAX; i++) {
        if (s_pl[i].used && memcmp(s_pl[i].addr, addr, 6) == 0) {
            return i;
        }
    }
    return -1;
}

/** 按 identity 地址（固定 MAC）找配对表条目。 */
static int pl_find_by_id(const uint8_t *id)
{
    pl_ensure_loaded();
    if (!id) {
        return -1;
    }
    for (int i = 0; i < PL_MAX; i++) {
        if (s_pl[i].used && s_pl[i].has_id &&
            memcmp(s_pl[i].id_addr, id, 6) == 0) {
            return i;
        }
    }
    return -1;
}

/**
 * ★ 解析这台遥控器的「规范映射地址」★
 *
 *   PC 端写映射用的是 DT RM 里报的 addr=（配对表里配对当时记下的地址），
 *   按键查表却用本次连接的广播地址 —— BLE 私有地址会轮换，
 *   两者一错开就出现「板子上显示有 N 条，按下去没反应」。
 *
 *   统一成：identity 反查配对表 → 用表里那个地址；
 *   查不到就用本次连接的地址。
 *   兜底：表里那个地址一条都没有、而本次连接的地址有 ——
 *        说明映射其实存在后者，用后者（别打断已经能用的状态）。
 *
 *   ★ 只在主循环里调 ★（会读 NVS，不能在 GAP 回调里做）
 */
static void peer_resolve_map_addr(br_peer_t *p)
{
    uint8_t canon[6];
    bool have = false;

    if (p->has_id) {
        int slot = pl_find_by_id(p->id_addr);
        if (slot >= 0) {
            memcpy(canon, s_pl[slot].addr, 6);
            have = true;
        }
    }
    if (!have) {
        memcpy(canon, p->addr, 6);
    }
    if (have && memcmp(canon, p->addr, 6) != 0) {
        if (br_mapping_count(canon) == 0 && br_mapping_count(p->addr) > 0) {
            memcpy(canon, p->addr, 6);
        }
    }
    memcpy(p->map_addr, canon, 6);
    p->map_addr_valid = true;
}

/** 写入/更新一条（按地址认人）。名字为空时保留原来的。 */
static bool pl_put(const uint8_t *addr, uint8_t type, const char *name,
                   const uint8_t *id_addr, uint8_t id_type, bool has_id)
{
    pl_ensure_loaded();

    int slot = pl_find(addr);
    if (slot < 0 && has_id && id_addr) {
        /*
         * ★ 先按 identity 认人 ★
         *   遥控器的广播地址会轮换，重新配对时地址可能变了，
         *   但 identity（固定 MAC）还是同一台。
         *   以前只按地址找 → 找不到就新建一个槽位 →
         *   同一台遥控器在板子上就有两份映射（两份 blob）：
         *   按键查表可能落到旧的那份，而 REMOTES 报的是新槽位的条数
         *   → 「显示有 N 条，按下去没反应」。
         */
        slot = pl_find_by_id(id_addr);
    }
    if (slot < 0) {
        for (int i = 0; i < PL_MAX; i++) {
            if (!s_pl[i].used) {
                slot = i;
                break;
            }
        }
    }
    if (slot < 0) {
        ESP_LOGW(TAG, "配对表已满（%d），这台记不下", PL_MAX);
        return false;
    }

    /*
     * 地址变了 → 旧地址上的映射作废。
     * 「重配 = 一台新遥控器」：不继承旧映射，重新铺出厂默认。
     */
    if (s_pl[slot].used && memcmp(s_pl[slot].addr, addr, 6) != 0) {
        br_mapping_clear(s_pl[slot].addr);
    }

    /* 没给新名字就延用表里已有的（可能是上次扫描缓存的） */
    if (name != NULL && name[0] != '\0') {
        snprintf(s_pl[slot].name, sizeof(s_pl[slot].name), "%s", name);
    }
    memcpy(s_pl[slot].addr, addr, 6);
    s_pl[slot].type = type;
    s_pl[slot].used = true;
    /* identity（固定 MAC）：有就存，没有就沿用表里原来的 */
    if (has_id && id_addr) {
        memcpy(s_pl[slot].id_addr, id_addr, 6);
        s_pl[slot].id_type = id_type;
        s_pl[slot].has_id = true;
    }

    char addr_s[24];
    addr_to_str(addr, addr_s, sizeof(addr_s));
    bool ok = pl_flush();
    ESP_LOGI(TAG, "配对表[%d] = %s type=%d '%s' 落盘=%d",
             slot, addr_s, type, s_pl[slot].name, ok ? 1 : 0);
    return ok;
}

/** 删第 idx 条，后面的往前挪，保持 0..n-1 连续 */
static bool pl_drop(int idx)
{
    pl_ensure_loaded();
    if (idx < 0 || idx >= PL_MAX || !s_pl[idx].used) {
        return false;
    }
    s_pl[idx].used = false;
    for (int i = idx + 1; i < PL_MAX; i++) {
        if (!s_pl[i].used) {
            break;
        }
        s_pl[i - 1] = s_pl[i];
        s_pl[i].used = false;
    }
    return pl_flush();
}

/*
 * ================================================================
 * ★ 配对相关 ★
 * ================================================================
 */

/*
 * 配对窗口结束（成功、放弃都走这里）。
 *
 * ★ 要把「是否持续扫描」还给 PC ★
 *   PC 自己发的 SCAN 2 是用户想继续找设备，
 *   不能被配对流程顺手关掉。
 */
static void pair_window_close(void)
{
    s_pair_pending = false;
    s_pair_fresh_ready = false;
    /* 配对窗口还开着的话，扫描要继续（还能再收别的遥控器） */
    if (!s_auto_pair_until) {
        s_scan_continuous = s_scan_continuous_before_pair;
    }
}

/**
 * PC 端发起配对：连上目标设备，NimBLE 会自动完成配对并保存 bond。
 *
 * 命令格式：PAIR <addr> <type> <name>
 */
/*
 * ★ 配对窗口 ★
 *
 * 开窗期间，板子会把**没登记过**的设备自动收进配对表
 * （落库 + 发 EV PAIR），PC 端不用再逐个发 PAIR。
 */
bool br_ble_pair_window_start(int seconds)
{
    if (seconds <= 0) {
        s_auto_pair_until = 0;
        bridge_led_set_pairing(false);
        ESP_LOGI(TAG, "pair window closed");
        return true;
    }
    int64_t now = esp_timer_get_time();
    s_auto_pair_until = now + (int64_t)seconds * 1000000LL;
    /* 窗口期间必须一直扫，不然看不到遥控器什么时候醒 */
    s_scan_continuous_before_pair = s_scan_continuous;
    s_scan_continuous = true;
    br_ble_scan_start(true);
    bridge_led_set_pairing(true);
    ESP_LOGI(TAG, "pair window open: %d s", seconds);
    return true;
}

bool br_ble_pair_window_auto(void)
{
    /*
     * 配对表是空的 → 板子正在自动等人来配对。
     * PC 端用它把界面文案说清楚（不是用户点的窗口）。
     */
    return pl_count() == 0;
}

int br_ble_pair_window_remaining(void)
{
    if (!s_auto_pair_until) {
        return 0;
    }
    int64_t now = esp_timer_get_time();
    if (now >= s_auto_pair_until) {
        s_auto_pair_until = 0;
        return 0;
    }
    return (int)((s_auto_pair_until - now) / 1000000LL);
}

bool br_ble_pair(const uint8_t *addr, uint8_t addr_type, const char *name)
{
    sanitize_name(name ? name : "", s_pair_name, sizeof(s_pair_name));
    s_pair_pending = true;
    memcpy(s_pair_addr, addr, 6);
    s_pair_type = addr_type;

    /*
     * ★ 配对重试窗口 ★
     *
     * 遥控器大部分时间在睡觉 —— 用户点「配对」的时候它往往还没醒。
     * 所以这里**不是试一次就放弃**，而是持续尝试 120 秒：
     * 遥控器什么时候醒，就什么时候连上。
     */
    int64_t now = esp_timer_get_time();
    s_pair_retry_until = now + PAIR_RETRY_WINDOW_US;
    s_next_pair_retry = now + PAIR_RETRY_GAP_US;
    s_last_conn_status = 0;

    memcpy(s_pair_fresh_addr, addr, 6);
    s_pair_fresh_type = addr_type;
    s_pair_fresh_ready = false;

    /*
     * ★ 配对期间必须持续扫描 ★
     *
     * 不扫就看不见遥控器什么时候醒；看不见就只能拿旧地址瞎连，
     * 结果永远是 status=13。这里直接把「扫描会话」拉起来，
     * 主循环的 fresh-adv 分支负责在扫到目标的那一轮立刻发起连接。
     */
    s_scan_continuous_before_pair = s_scan_continuous;
    s_scan_continuous = true;
    s_scan_session_active = false;
    br_ble_scan_start(true);

    ESP_LOGI(TAG, "PAIR 开始：目标=%s type=%d '%s'（持续扫描 %lld 秒）",
             s_pair_name, addr_type,
             s_pair_name, (long long)(PAIR_RETRY_WINDOW_US / 1000000LL));

    /* 先拿 PC 给的最新地址试一次；失败就交给 fresh-adv + 重试循环 */
    br_ble_connect_addr(addr, addr_type, s_pair_name);
    return true;
}

/**
 * 彻底解绑：断开 + 从 NVS 配对表删除 + 顺带清 bond（如果真有）。
 *
 * ★ idx 是 REMOTES 里 DT RM 的 idx（配对表位置），不是 bond 表下标 ★
 *   PC 端「删除」默认只做软删除（客户端本地隐藏，配对表不动），
 *   这个硬删要用户二次确认才走到 —— 因为遥控器那边仍留着旧配对关系，
 *   我们没法远程清掉它，之后可能要它恢复出厂才配得上。
 */
bool br_ble_unpair(int rem_idx)
{
    uint8_t addr[6];
    uint8_t type;
    char name[BR_MAX_NAME_LEN];
    if (!pl_get(rem_idx, addr, &type, name, sizeof(name))) {
        ESP_LOGW(TAG, "UNPAIR：配对表没有第 %d 条", rem_idx);
        return false;
    }

    /* 先断开 */
    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (s_peers[i].used && s_peers[i].connected &&
            memcmp(s_peers[i].addr, addr, 6) == 0) {
            ble_gap_terminate(s_peers[i].conn_handle,
                              BLE_ERR_REM_USER_CONN_TERM);
        }
    }

    /*
     * ★ 删 bond 必须按 **identity 地址** ★
     *
     *   NimBLE 的 bond 表里存的是 identity 地址（固定的那串 MAC），
     *   而配对表里存的是**当轮广播地址**（BLE 私有地址，会轮换）。
     *   以前拿广播地址去比对 identity 地址 —— 永远对不上，
     *   bond 删不掉（日志「bond删除=0」）。
     *   遥控器和板子两边都还留着旧 bond，重新配对时配对流程走不下去，
     *   表现为「删掉配对后再配对，刷新永远刷不出来」，
     *   只有断电重来（真机症状：必须两个 USB 口都拔掉重插）。
     */
    uint8_t id_addr[6];
    uint8_t id_type = 0;
    bool has_id = false;
    pl_get_id(rem_idx, id_addr, &id_type, &has_id);

    int deleted = 0;
    if (has_id) {
        ble_addr_t id;
        memset(&id, 0, sizeof(id));
        id.type = (uint8_t)id_type;
        memcpy(id.val, id_addr, 6);
        deleted = (ble_store_util_delete_peer(&id) == 0) ? 1 : 0;
    }
    if (!deleted) {
        /* 老记录没有 identity：退回到用广播地址试一次 */
        ble_addr_t addrs[MAX_BONDED];
        int num = 0;
        if (ble_store_util_bonded_peers(addrs, &num, MAX_BONDED) == 0) {
            for (int i = 0; i < num; i++) {
                if (memcmp(addrs[i].val, addr, 6) == 0) {
                    deleted = (ble_store_util_delete_peer(&addrs[i]) == 0) ? 1 : 0;
                    break;
                }
            }
        }
    }

    /*
     * ★ 释放这个地址占着的 peer 槽 ★
     *   不释放的话 is_tracked_addr() 还认得这个地址，
     *   配对窗口里的「没收过的新设备」分支就永远轮不到它，
     *   重新配对也进不了配对表。
     */
    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (!s_peers[i].used) {
            continue;
        }
        if (memcmp(s_peers[i].addr, addr, 6) == 0 ||
            (has_id && s_peers[i].has_id &&
             memcmp(s_peers[i].id_addr, id_addr, 6) == 0)) {
            if (s_peers[i].connected) {
                ble_gap_terminate(s_peers[i].conn_handle,
                                  BLE_ERR_REM_USER_CONN_TERM);
            }
            memset(&s_peers[i], 0, sizeof(s_peers[i]));
            ESP_LOGI(TAG, "UNPAIR：释放 peer 槽 %d", i);
        }
    }

    bool ok = pl_drop(rem_idx);
    /*
     * ★ 它的映射也一起删 ★
     *   留着的话，重新配对回来时板子上「已经有映射」，
     *   就不会铺出厂默认 —— 用户重配完还是什么都按不动。
     */
    br_mapping_clear(addr);
    /*
     * 名单里已经没有这台了 —— 如果这是最后一台，
     * 剩下的 bond 全是孤的（用户明确不要它们了），一并清掉，
     * 遥控器才能重新配对。
     */
    char addr_s[24];
    addr_to_str(addr, addr_s, sizeof(addr_s));
    ESP_LOGI(TAG, "UNPAIR %s：配对表删除=%d bond删除=%d", addr_s, ok ? 1 : 0, deleted);
    return ok;
}

int br_ble_list_bonded(void)
{
    ble_addr_t addrs[MAX_BONDED];
    int num = 0;

    int rc = ble_store_util_bonded_peers(addrs, &num, MAX_BONDED);
    if (rc != 0) {
        ESP_LOGE(TAG, "bonded_peers failed: %d", rc);
        br_kv_t kv;
        br_kv_init(&kv);
        br_kv_add_int(&kv, "rc", rc);
        br_proto_send_ev("BNDFAIL", &kv);
        return 0;
    }

    for (int i = 0; i < num; i++) {
        char addr[24];
        addr_to_str(addrs[i].val, addr, sizeof(addr));

        br_kv_t kv;
        br_kv_init(&kv);
        br_kv_add_int(&kv, "idx", i);
        br_kv_add(&kv, "addr", addr);
        br_kv_add_int(&kv, "type", addrs[i].type);
        br_proto_send_dt("BND", &kv);

        ESP_LOGI(TAG, "bonded[%d] %s type=%d", i, addr, addrs[i].type);
    }

    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "count", num);
    br_proto_send_ev("BNDEND", &kv);
    return num;
}

bool br_ble_connect_bonded(int idx)
{
    ble_addr_t addrs[MAX_BONDED];
    int num = 0;

    if (ble_store_util_bonded_peers(addrs, &num, MAX_BONDED) != 0) {
        return false;
    }
    if (idx < 0 || idx >= num) {
        return false;
    }

    char addr[24];
    addr_to_str(addrs[idx].val, addr, sizeof(addr));
    ESP_LOGI(TAG, "connecting to bonded[%d] %s", idx, addr);

    return br_ble_connect_addr(addrs[idx].val, addrs[idx].type, "bonded");
}

/*
 * ★ 已配对遥控器列表 ★
 *
 * 数据源是 **NVS 配对表**（pl_*），不是 BLE bond 表。
 * 理由见上面「已配对遥控器表」那段：我们的链路是不加密也能用的，
 * bond 表经常是空的，拿它当唯一真相就会「明明连上了，列表却是空的」。
 * bond 表只当作附加信息报给 PC（bonded=1 说明这台真握过密钥）。
 */
int br_ble_list_remotes(void)
{
    ble_addr_t bonded[MAX_BONDED];
    int n_bonded = 0;
    if (ble_store_util_bonded_peers(bonded, &n_bonded, MAX_BONDED) != 0) {
        n_bonded = 0;
    }

    int num = 0;
    for (int i = 0; i < PL_MAX; i++) {
        uint8_t addr[6];
        uint8_t type;
        char name[BR_MAX_NAME_LEN];
        if (!pl_get(i, addr, &type, name, sizeof(name))) {
            continue;
        }

        char addr_s[24];
        addr_to_str(addr, addr_s, sizeof(addr_s));

        /* identity（固定 MAC）：广播地址会轮换，认人靠它 */
        uint8_t id_addr[6];
        uint8_t id_type = 0;
        bool has_id = false;
        pl_get_id(i, id_addr, &id_type, &has_id);
        char id_s[24] = "";
        if (has_id) {
            addr_to_str(id_addr, id_s, sizeof(id_s));
        }

        /* 连接状态 / 语音能力来自在线槽位（地址或 identity 对上就算） */
        const char *live_name = name;
        bool conn = false;
        int voice = 0;
        for (int p = 0; p < BR_MAX_PEERS; p++) {
            if (!s_peers[p].used) {
                continue;
            }
            bool same = memcmp(s_peers[p].addr, addr, 6) == 0;
            if (!same && has_id && s_peers[p].has_id) {
                same = memcmp(s_peers[p].id_addr, id_addr, 6) == 0;
            }
            if (same) {
                if (s_peers[p].name[0]) {
                    live_name = s_peers[p].name;
                }
                conn = s_peers[p].connected;
                voice = s_peers[p].atvv_seen ? 1 : 0;
                break;
            }
        }
        /* 表里没存到名字时，退回到本轮扫描缓存 */
        if (live_name[0] == '\0') {
            for (int c = 0; c < s_candidate_count; c++) {
                if (memcmp(s_candidates[c].addr, addr, 6) == 0) {
                    live_name = s_candidates[c].name;
                    break;
                }
            }
        }

        /* bond 表里存的是 identity 地址，所以拿 identity 比 */
        int bonded_flag = 0;
        for (int b = 0; b < n_bonded; b++) {
            if (memcmp(bonded[b].val, addr, 6) == 0) {
                bonded_flag = 1;
                break;
            }
            if (has_id && memcmp(bonded[b].val, id_addr, 6) == 0) {
                bonded_flag = 1;
                break;
            }
        }

        br_kv_t kv;
        br_kv_init(&kv);
        br_kv_add_int(&kv, "idx", i);
        br_kv_add(&kv, "addr", addr_s);
        br_kv_add_int(&kv, "type", type);
        br_kv_add(&kv, "name", live_name);
        br_kv_add_int(&kv, "conn", conn ? 1 : 0);
        br_kv_add_int(&kv, "voice", voice ? 1 : 0);
        br_kv_add_int(&kv, "bonded", bonded_flag);
        /* identity（固定 MAC）：PC 端按它归档映射，刷完固件后能认出同一台 */
        br_kv_add(&kv, "id", id_s);
        /* 真实映射条数（以前写死 0，界面永远显示「已配 0 条映射」） */
        br_kv_add_int(&kv, "binds", br_mapping_count(addr));
        br_proto_send_dt("RM", &kv);
        num++;
    }

    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "count", num);
    br_kv_add_int(&kv, "bonds", n_bonded);
    /*
     * 客户端需要一个「列表到此为止」的信号才能把删掉的残留条目裁掉 ——
     * OK 行只给命令等待者，事件流里必须再来一次。
     */
    br_proto_send_ev("RPEND", &kv);
    br_proto_send_ok("REMOTES", &kv);
    return num;
}

/**
 * 开启/关闭按键学习。
 *
 * 开启后，用户按下的第一个键会被单独上报为 LEARN 事件并自动退出学习。
 */
void br_ble_set_learn(int idx, bool on)
{
    s_learn_mode = on;
    s_learn_idx = idx;
    ESP_LOGI(TAG, "learn mode %s (idx=%d)", on ? "on" : "off", idx);
}

/*
 * ★ 自主重连（P7 的核心）★
 *
 * 遥控器空闲时**不广播**（实测 40 秒扫不到），
 * 而且如果它已经连着电视/手机，我们主动连会被拒（实测 status=13 超时）。
 *
 * 所以策略是：**不停地重试已配对设备**。
 * 遥控器一旦醒来（按键唤醒或电视断开），下一次重试就能连上。
 */
#define RECONNECT_INTERVAL_MS 1500 /* 唤醒后 1.5 秒内必须抓住 */

static int64_t s_last_reconnect = 0;
static int     s_reconnect_idx = 0;

int br_ble_autoreconnect_tick(void)
{
    if (!s_auto || s_connect_in_progress) {
        return 0;
    }
    if (br_ble_connected_count() > 0) {
        return 0;
    }

    /*
     * ★ 配对流程优先 ★
     *
     * bond 兜底路径拿的是表里的旧地址 + 交替的地址类型，可靠性远不如
     * 「当轮新鲜广播」。让它先跑就会白占连接槽（s_connect_in_progress），
     * 把最可靠的那条路挤掉 —— 这就是「明明扫到了还是配不上」的根因。
     */
    if (s_pair_pending || s_pair_fresh_ready) {
        return 0;
    }

    /*
     * ★ 这里只管「用 bond 地址主动连」 ★
     *   扫描完全交给配对模式（PC 发 SCAN 2），
     *   两条路径不重叠，就不会互相抢控制器。
     */

    /*
     * ★ 扫描里已经找到目标了 -> 让调度器去连 ★
     *
     * 调度器用的是**扫描到的地址 + 地址类型**，那是最可靠的
     * （地址类型直接来自广播，不会像 bond 表那样记错）。
     *
     * ★ 之前这里不检查，导致 bond 兜底路径抢先占用连接槽 ★
     *   结果扫描明明认出遥控器了（score=100），连接却被一个
     *   用错地址类型的 bond 尝试抢走，必然超时。
     */
    /*
     * 兜底：用 bond 里的 identity 地址主动连。
     *
     * ★ 不能用「候选列表非空」当条件 ★
     *   周围永远有十几个 BLE 设备（手机、电视盒、耳机），
     *   候选列表永远非空 —— 拿它当条件，bond 路径就永远不会执行，
     *   结果一次连接都不发起。
     *   这里只按**时间**节流。
     */
    int64_t now = esp_timer_get_time();
    if (now - s_last_reconnect < RECONNECT_INTERVAL_MS * 1000) {
        return 0;
    }
    s_last_reconnect = now;

    /*
     * ★ 目标来源：NVS 配对表优先，bond 表只兜底 ★
     *
     * 配对表记的是**扫描时拿到的地址 + 地址类型**，那是权威值，
     * 不必再用「类型逐次交替」去猜。
     * 表为空（只有 bond 的旧状态）才退回 bond 表。
     */
    int num = pl_count();
    if (num > 0) {
        for (int k = 0; k < num; k++) {
            int idx = (s_reconnect_idx + k) % num;
            uint8_t paddr[6];
            uint8_t ptype;
            char pname[BR_MAX_NAME_LEN];
            if (!pl_get(idx, paddr, &ptype, pname, sizeof(pname))) {
                continue;
            }
            char paddr_s[24];
            addr_to_str(paddr, paddr_s, sizeof(paddr_s));
            ESP_LOGI(TAG, "autoreconnect try paired[%d] %s type=%d '%s'",
                     idx, paddr_s, ptype, pname);
            if (br_ble_connect_addr(paddr, ptype, pname)) {
                s_reconnect_idx = (idx + 1) % num;
                return 1;
            }
        }
        return 0;
    }

    ble_addr_t addrs[MAX_BONDED];
    num = 0;
    if (ble_store_util_bonded_peers(addrs, &num, MAX_BONDED) != 0 || num == 0) {
        return 0;
    }

    for (int k = 0; k < num; k++) {
        int idx = (s_reconnect_idx + k) % num;
        char addr[24];
        addr_to_str(addrs[idx].val, addr, sizeof(addr));
        /*
         * ★ 地址类型：先��� bond 记的类型，连续失败 3 次才试另一种 ★
         *
         * bond 表里记的类型不一定准（遥控器可能用随机地址，
         * 而 0x7E 开头按 BLE 规范属于 random static）。
         * 但**扫描到的类型才是权威的**，所以默认信 bond，
         * 只有连续失败才换另一种试。
         */
        /*
         * ★ 地址类型逐次交替 ★
         *   不能用「失败计数超过阈值就永久换类型」——
         *   那样一旦锁死在错误类型上，就再也不会试回正确的那个。
         *   逐次交替保证两种类型都被公平地试到。
         */
        uint8_t atype = s_type_flip ? (uint8_t)(1 - addrs[idx].type)
                                     : addrs[idx].type;
        s_type_flip = !s_type_flip;
        ESP_LOGI(TAG, "autoreconnect try bonded[%d] %s type=%d (streak=%d)",
                 idx, addr, atype, s_bond_fail_streak);
        if (br_ble_connect_addr(addrs[idx].val, atype, "bonded")) {
            s_reconnect_idx = (idx + 1) % num;
            return 1;
        }
    }
    return 0;
}

void br_ble_set_auto(bool on)
{
    s_auto = on;
    if (on) {
        s_next_pick = 0;
        s_next_blind = esp_timer_get_time() + 60000000;
        /* 自动运行靠新鲜广播地址连接，不能停掉这里的静默扫描。 */
    } else if (s_scanning && !s_pair_pending) {
        br_ble_scan_stop();
    }
    ESP_LOGI(TAG, "auto mode %s", on ? "on" : "off");
}

void br_ble_set_auto_dump(bool on)
{
    s_auto_dump = on;
}

void br_ble_set_target_name(const char *substr)
{
    if (substr) {
        snprintf(s_target_name, sizeof(s_target_name), "%s", substr);
    } else {
        s_target_name[0] = '\0';
    }
    ESP_LOGI(TAG, "target name filter = '%s'", s_target_name);
}

/* ------------------------------------------------------------ 诊断接口 */

void br_ble_gatt_dump(int peer_idx)
{
    if (peer_idx < 0 || peer_idx >= BR_MAX_PEERS) {
        return;
    }
    br_peer_t *p = &s_peers[peer_idx];
    if (!p->used || !p->connected) {
        return;
    }
    p->svc_count = 0;
    p->chr_count = 0;
    p->cur_svc = 0;
    p->atvv_seen = false;
    p->atvv_audio_handle = 0;
    p->atvv_ctl_handle = 0;
    p->atvv_tx_handle = 0;
    p->disc_state = BR_DISC_NEXT_SVC;
    int rc = ble_gattc_disc_all_svcs(p->conn_handle, disc_svc_cb,
                                     (void *)(intptr_t)peer_idx);
    if (rc != 0) {
        ESP_LOGE(TAG, "gatt dump: disc_all_svcs failed: %d", rc);
        p->disc_state = BR_DISC_NONE;
    }
}

void br_ble_dump_state(void)
{
    br_kv_t kv;
    br_kv_init(&kv);
    br_kv_add_int(&kv, "scanning", s_scanning ? 1 : 0);
    br_kv_add_int(&kv, "candidates", s_candidate_count);
    br_kv_add_int(&kv, "peers", s_peer_count);
    br_kv_add_int(&kv, "connecting", s_connect_in_progress ? 1 : 0);
    br_kv_add_int(&kv, "pair", s_pair_pending ? 1 : 0);
    br_kv_add_int(&kv, "fresh", s_pair_fresh_ready ? 1 : 0);
    /* NVS 配对表里有几台 —— 「已配对的遥控器」为空时先看这个 */
    br_kv_add_int(&kv, "paired", pl_count());

    /*
     * ★ 把**本机**身份地址打出来 ★
     *
     * 「扫得到广播、连接必然超时」还剩一个没被证据排除的可能：
     *   CONNECT_IND 里的 InitiatorA 是全 0 或无效，对端直接不理。
     * ble_gap_connect 用 BLE_OWN_ADDR_PUBLIC，用的就是这里打的 pub。
     * 如果 pub 是 00:00:00:00:00:00，配对永远不可能成功 —— 一次日志就能确认。
     */
    uint8_t id[6];
    /*
     * ★ 第三个参数是「是不是 NRPA」的标志，不是地址长度 ★
     *   上一版把它当长度、还要求 ==6，结果 rc=0（调用成功）也被判成失败，
     *   日志里 pub/rnd 全成了 "none" —— 差点把我带到
     *   「本机根本没有身份地址」这个结论上。
     *   API 用错的时候，连自己打出来的证据都是假的。
     */
    int nrpa = 0;
    char pub[32] = "none", rnd[32] = "none";
    int rc_p = ble_hs_id_copy_addr(BLE_ADDR_PUBLIC, id, &nrpa);
    if (rc_p == 0) {
        addr_to_str(id, pub, sizeof(pub));
        if (addr_is_all_zero(id)) {
            snprintf(pub, sizeof(pub), "ALL-ZERO");
        }
    } else {
        snprintf(pub, sizeof(pub), "rc=%d", rc_p);
    }
    nrpa = 0;
    int rc_r = ble_hs_id_copy_addr(BLE_ADDR_RANDOM, id, &nrpa);
    if (rc_r == 0) {
        addr_to_str(id, rnd, sizeof(rnd));
    } else {
        snprintf(rnd, sizeof(rnd), "rc=%d", rc_r);
    }
    br_kv_add(&kv, "pub", pub);
    br_kv_add(&kv, "rnd", rnd);
    br_kv_add_int(&kv, "own", s_own_addr_type);

    br_proto_send_ev("STATE", &kv);

    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (!s_peers[i].used) {
            continue;
        }
        char addr[24];
        addr_to_str(s_peers[i].addr, addr, sizeof(addr));
        br_kv_t pkv;
        br_kv_init(&pkv);
        br_kv_add_int(&pkv, "idx", i);
        br_kv_add(&pkv, "addr", addr);
        br_kv_add_int(&pkv, "conn", s_peers[i].connected ? 1 : 0);
        br_kv_add_int(&pkv, "disc", s_peers[i].disc_state);
        br_kv_add_int(&pkv, "svcs", s_peers[i].svc_count);
        br_kv_add_int(&pkv, "chrs", s_peers[i].chr_count);
        br_kv_add_int(&pkv, "atvv", s_peers[i].atvv_seen ? 1 : 0);
        br_kv_add_int(&pkv, "enc", s_peers[i].encrypted ? 1 : 0);
        br_kv_add_int(&pkv, "sub", s_peers[i].channels_open ? 1 : 0);
        br_proto_send_dt("PEER", &pkv);
    }
}

int br_ble_peer_count(void)
{
    return s_peer_count;
}

int br_ble_connected_count(void)
{
    int n = 0;
    for (int i = 0; i < BR_MAX_PEERS; i++) {
        if (s_peers[i].used && s_peers[i].connected) {
            n++;
        }
    }
    return n;
}

const char *br_ble_peer_name(int idx)
{
    if (idx < 0 || idx >= BR_MAX_PEERS) {
        return "";
    }
    return s_peers[idx].name;
}

bool br_ble_peer_addr(int idx, uint8_t *addr, uint8_t *type)
{
    if (idx < 0 || idx >= BR_MAX_PEERS || !s_peers[idx].used) {
        return false;
    }
    if (addr) {
        memcpy(addr, s_peers[idx].addr, 6);
    }
    if (type) {
        *type = s_peers[idx].addr_type;
    }
    return true;
}

uint16_t br_ble_peer_conn_handle(int idx)
{
    if (idx < 0 || idx >= BR_MAX_PEERS) {
        return 0;
    }
    return s_peers[idx].conn_handle;
}

int br_ble_peer_char_count(int peer_idx)
{
    if (peer_idx < 0 || peer_idx >= BR_MAX_PEERS) {
        return 0;
    }
    return s_peers[peer_idx].chr_count;
}

bool br_ble_peer_char_at(int peer_idx, int i, br_chr_info_t *out)
{
    if (out == NULL || peer_idx < 0 || peer_idx >= BR_MAX_PEERS) {
        return false;
    }
    br_peer_t *p = &s_peers[peer_idx];
    if (i < 0 || i >= p->chr_count) {
        return false;
    }
    const br_chr_t *c = &p->chrs[i];
    memset(out, 0, sizeof(*out));
    out->def_handle = c->def_handle;
    out->val_handle = c->val_handle;
    out->cccd_handle = c->cccd_handle;
    out->ref_handle = c->ref_handle;
    out->props = c->props;
    out->uuid_type = c->uuid_type;
    /* 只回传 16 位 UUID；HID 的特征（0x2A4D/0x2A4B）全是 16 位的 */
    if (c->uuid_type == 16) {
        out->uuid16 = (uint16_t)((c->uuid[0] << 8) | c->uuid[1]);
    }
    return true;
}

/* -------------------------------------------------------------- 主循环 */

/**
 * ★ 发现流程驱动 ★
 *
 * NimBLE 不允许在过程回调里再发起另一个过程，所以：
 *   回调只改状态 → 主循环看到状态再发起下一个过程。
 * 这也是为什么 CCCD 一开始一个都发现不到（之前在 chr 回调里直接发 disc_all_dscs）。
 */
static void disc_drive(int peer_idx)
{
    br_peer_t *p = &s_peers[peer_idx];
    if (!p->used || !p->connected) {
        return;
    }

    if (p->disc_state == BR_DISC_NEXT_SVC) {
        p->disc_state = BR_DISC_SVC;
        int rc = ble_gattc_disc_all_svcs(p->conn_handle, disc_svc_cb,
                                         (void *)(intptr_t)peer_idx);
        if (rc != 0) {
            ESP_LOGE(TAG, "disc_all_svcs failed: %d", rc);
            p->disc_state = BR_DISC_NONE;
        }
        return;
    }

    if (p->disc_state == BR_DISC_NEXT_CHR) {
        p->disc_state = BR_DISC_CHR;
        int rc = ble_gattc_disc_all_chrs(p->conn_handle,
                                        p->svcs[p->cur_svc].start_handle,
                                        p->svcs[p->cur_svc].end_handle,
                                        disc_chr_cb,
                                        (void *)(intptr_t)peer_idx);
        if (rc != 0) {
            ESP_LOGE(TAG, "disc_all_chrs failed: %d", rc);
            p->disc_state = BR_DISC_DONE;
        }
        return;
    }

    if (p->disc_state == BR_DISC_NEXT_DSC) {
        p->disc_state = BR_DISC_DSC;
        int rc = ble_gattc_disc_all_dscs(p->conn_handle,
                                         p->svcs[p->cur_svc].start_handle,
                                         p->svcs[p->cur_svc].end_handle,
                                         disc_dsc_cb,
                                         (void *)(intptr_t)peer_idx);
        if (rc != 0) {
            ESP_LOGE(TAG, "disc_all_dscs failed: %d", rc);
            /* 描述符发现失败也要往下走，不能卡住整个流程 */
            emit_service(peer_idx, &p->svcs[p->cur_svc]);
            p->cur_svc++;
            p->disc_state = (p->cur_svc >= p->svc_count)
                                ? BR_DISC_DONE : BR_DISC_NEXT_CHR;
        }
        return;
    }
}


static void br_ble_task(void *arg)
{
    (void)arg;
    int64_t last_state_log = 0;

    for (;;) {
        int64_t now = esp_timer_get_time();

        /* ★ 发现流程驱动（必须在回调之外发起过程）★ */
        for (int i = 0; i < BR_MAX_PEERS; i++) {
            if (s_peers[i].used && s_peers[i].connected) {
                /*
                 * ★ 规范映射地址在这里解析 ★
                 *   identity 可能在连上之后才到（BLE_GAP_EVENT_IDENTITY_RESOLVED），
                 *   那时才知道配对表里对应的是哪个地址。
                 *   解析要读 NVS，所以只能在主循环里做，不能在回调里。
                 */
                if (!s_peers[i].map_addr_valid) {
                    peer_resolve_map_addr(&s_peers[i]);
                }
                /*
                 * ★ 连接参数请求不能在 GAP 回调里发起 ★
                 *   NimBLE 的过程发起函数在回调里调用会踩到嵌套限制，
                 *   所以这里只置标志，由主循环来发。
                 */
                if (s_peers[i].params_pending) {
                    s_peers[i].params_pending = false;
                    request_conn_params(&s_peers[i]);
                }
                disc_drive(i);
                br_hid_drive(i, s_peers[i].conn_handle);
                /* ATVV 的 CCCD 也要真写（对端才知道要发） */
                atvv_sub_drive(i);
            }
        }

        /*
         * ★ 铺出厂默认（排队里的）★
         *   PC 正在写映射时这里会跳过（返回 -1），下一拍再来。
         */
        run_pending_defaults();

        /*
         * ★ 看门狗：按住超时的键强制抬起 ★
         *
         *   板子替 PC 按键时只要漏了一次「抬起」，那个键就永远按在 PC 上
         *   —— PC 自己的键盘会突然失灵（每个键都变成 Ctrl/Win+某键），
         *   或者「按下返回键，输入框文字一下全被删掉」。
         *   这里每 200 ms 扫一次，超时的强制抬起。
         */
        static int64_t last_hid_watchdog = 0;
        if (now - last_hid_watchdog > 200000) {
            last_hid_watchdog = now;
            bridge_usb_hid_watchdog();
            br_voice_watchdog();
        }

        /*
         * ★ 把按键事件发成协议行 ★
         */
        if (s_keyq) {
            br_hid_key_t k;
            int n = 0;
            while (n < 32 && xQueueReceive(s_keyq, &k, 0) == pdTRUE) {
                /*
                 * 学习模式：按下的那一个键单独上报为 LEARN，
                 * 并且**退出学习模式**（学一个就够）。
                 */
                if (s_learn_mode && k.down) {
                    s_learn_mode = false;
                    br_kv_t lkv;
                    br_kv_init(&lkv);
                    br_kv_add_int(&lkv, "idx", s_learn_idx);
                    br_kv_add_int(&lkv, "src", k.src);
                    br_kv_add_int(&lkv, "usage", k.usage);
                    br_proto_send_ev("LEARN", &lkv);
                    ESP_LOGI(TAG, "LEARN src=%u usage=0x%02X",
                             k.src, k.usage);
                    continue;
                }

                if (k.peer_idx >= 0 && k.peer_idx < BR_MAX_PEERS) {
                    /*
                     * ★ 用规范映射地址查表 ★
                     *   本次连接的广播地址会轮换，而 PC 写映射用的是
                     *   配对表里那个地址 —— 不用同一个地址就会出现
                     *   「板子上显示有 N 条，按下去没反应」。
                     */
                    br_peer_t *kp = &s_peers[k.peer_idx];
                    uint8_t addr[6];
                    bool have_addr = false;
                    if (kp->map_addr_valid) {
                        memcpy(addr, kp->map_addr, 6);
                        have_addr = true;
                    } else {
                        have_addr = br_ble_peer_addr(k.peer_idx, addr, NULL);
                    }
                    if (have_addr) {
                        br_mapping_key(addr, BR_KEY_MAKE(k.src, k.usage), k.down);
                    }
                }

                br_kv_t kv;
                br_kv_init(&kv);
                br_kv_add_int(&kv, "idx", k.peer_idx);
                br_kv_add_int(&kv, "src", k.src);
                br_kv_add_int(&kv, "usage", k.usage);
                br_kv_add(&kv, "state", k.down ? "down" : "up");
                /*
                 * ★ 带上是哪台遥控器 ★
                 *   idx 是板子的 peer 槽位号，PC 端对不上「哪台遥控器」；
                 *   有地址才能在人话日志里写「客厅遥控器 按下「主页键」」。
                 */
                if (k.peer_idx >= 0 && k.peer_idx < BR_MAX_PEERS) {
                    /*
                     * ★ 报的是规范映射地址 ★
                     *   PC 端的遥控器名单按配对表里的地址认人，
                     *   这里报同一个地址，PC 才能对上是哪一台。
                     */
                    const uint8_t *ka = s_peers[k.peer_idx].map_addr_valid
                                        ? s_peers[k.peer_idx].map_addr
                                        : s_peers[k.peer_idx].addr;
                    char kaddr[18];
                    addr_to_str(ka, kaddr, sizeof(kaddr));
                    br_kv_add(&kv, "addr", kaddr);
                }
                br_proto_send_ev("KEY", &kv);
                n++;
            }
        }

        /*
         * ★ 把扫描回调记下的候选发成协议行 ★
         */
        while (s_emit_done < s_emit_upto) {
            int i = s_emit_done;
            br_candidate_t *c = &s_candidates[i];
            char addr[24];
            addr_to_str(c->addr, addr, sizeof(addr));

            br_kv_t kv;
            br_kv_init(&kv);
            br_kv_add_int(&kv, "rssi", c->rssi);
            br_kv_add(&kv, "addr", addr);
            br_kv_add_int(&kv, "type", s_candidates[i].addr_type);
            br_kv_add(&kv, "name", c->name);
            br_kv_add(&kv, "why", c->why);
            br_kv_add_int(&kv, "score", c->score);
            br_kv_add_int(&kv, "idx", i);
            br_kv_add_int(&kv, "adv", c->adv_type);
            br_proto_send_ev("SCAN", &kv);
            s_emit_done++;
        }

        /*
         * ★ 把通知队列里的数据发成协议行 ★
         */
        if (s_auq) {
            au_item_t item;
            int drained = 0;
            while (drained < 32 && xQueueReceive(s_auq, &item, 0) == pdTRUE) {
                int idx = -1;
                for (int i = 0; i < BR_MAX_PEERS; i++) {
                    if (s_peers[i].used && s_peers[i].connected &&
                        s_peers[i].atvv_ctl_handle == item.attr) {
                        idx = i;
                        break;
                    }
                }
                const char *kind = "NT";
                for (int i = 0; i < BR_MAX_PEERS; i++) {
                    if (!s_peers[i].used || !s_peers[i].connected) {
                        continue;
                    }
                    if (s_peers[i].atvv_audio_handle == item.attr) {
                        kind = "AU";
                        idx = i;
                        break;
                    }
                    if (s_peers[i].atvv_ctl_handle == item.attr) {
                        kind = "CTL";
                        idx = i;
                        break;
                    }
                }

                br_kv_t kv;
                br_kv_init(&kv);
                br_kv_add_int(&kv, "idx", idx);
                br_kv_add_int(&kv, "h", item.attr);
                br_kv_add_int(&kv, "n", item.len);
                if (strcmp(kind, "CTL") == 0) {
                    bool voice_pressed = br_voice_control(item.data, item.len);
                    br_kv_add_hex(&kv, "hex", item.data, item.len);
                    br_proto_send_dt(kind, &kv);
                    /*
                     * ★ AUDIO_START = 遥控器的语音键真的按下了 ★
                     *   板子这会儿正替 PC 按语音快捷键（默认 左Ctrl+左Win）。
                     *   单独报一条事件，PC 端就能显示
                     *   「客厅遥控器 按下语音键 → 板子按出 左Ctrl+左Win」，
                     *   用户一眼看出到底是没收到还是输入法没反应。
                     *
                     * ★ sent / on 必报 ★
                     *   以前这条事件无条件发，语音通道被关掉时也照样发 ——
                     *   用户看日志以为板子按了热键，其实一个字都没按出去。
                     */
                    if (item.len > 0 && item.data[0] == 0x04 && idx >= 0) {
                        uint8_t vmods = 0, vcount = 0, vkeys[6] = {0};
                        br_voice_get_hotkey(&vmods, vkeys, &vcount);
                        char vaddr[18];
                        addr_to_str(s_peers[idx].addr, vaddr, sizeof(vaddr));
                        br_kv_t vkv; br_kv_init(&vkv);
                        br_kv_add_int(&vkv, "idx", idx);
                        br_kv_add(&vkv, "addr", vaddr);
                        br_kv_add_int(&vkv, "sent", voice_pressed ? 1 : 0);
                        br_kv_add_int(&vkv, "on", br_voice_enabled() ? 1 : 0);
                        br_kv_add_int(&vkv, "mods", vmods);
                        br_kv_add_hex(&vkv, "keys", vkeys, vcount);
                        br_kv_add_int(&vkv, "t",
                                      (int)(esp_timer_get_time() / 1000));
                        br_proto_send_ev("VOICE", &vkv);
                    }
                } else if (strcmp(kind, "AU") == 0) {
                    br_voice_audio(item.data, item.len);
                    if (bridge_usb_cdc_connected()) {
                        br_proto_send_dt(kind, &kv);
                    }
                }
                drained++;
            }
        }

        /*
         * ★ 调度器 ★
         *   连接动作一律发生在主循环里，绝不在 GAP 回调里。
         *
         * 优先级（这条顺序是修好配对的关键）：
         *   1. 新鲜广播命中 -> 立刻用当轮地址连（最可靠）
         *   2. 配对窗口内的瞎试（遥控器还没醒时的兜底）
         *   3. bond 自动重连 —— 必须让位给 1 和 2
         *
         * ★ 之前是 autoreconnect 排在最前面无条件执行 ★
         *   它拿 bond 里的地址 + 交替的地址类型去抢连接槽，
         *   一旦它先发起（s_connect_in_progress=true），
         *   新鲜广播那条最可靠的路径就再也挤不进来，
         *   表现就是「明明扫到遥控器了，配对还是失败」。
         */
        if (s_pair_fresh_ready && !s_connect_in_progress &&
            br_ble_connected_count() == 0) {
            s_pair_fresh_ready = false;
            char fresh[24];
            addr_to_str(s_pair_fresh_addr, fresh, sizeof(fresh));
            ESP_LOGI(TAG, "connect fresh adv: %s type=%d age=%lldms (%s)",
                     fresh, s_pair_fresh_type,
                     (long long)((esp_timer_get_time() - s_pair_fresh_us) / 1000),
                     s_pair_pending ? "pairing" : "tracked remote");
            br_ble_connect_addr(s_pair_fresh_addr, s_pair_fresh_type,
                                s_pair_pending ? s_pair_name : "");
        }

        /*
         * ★ 扫描必须在「配对瞎试」之前重新拉起来 ★
         *
         * 这段原来写在调度器的**最后面**，于是配对窗口里永远是这样：
         *   连接失败 -> 下一轮循环先跑到「瞎试」-> s_connect_in_progress=1
         *   -> 走到最后这句时条件 !s_connect_in_progress 不成立 -> 扫描永不重启
         * 实测证据（客户端日志 12:18:51~12:20:07 整整 2 分钟）：
         *   EV STATE scanning=0 connecting=1 pair=1 fresh=0  ← 一直是这副样子
         *   EV CONNFAIL status=13 每 5 秒一次，一次不落
         * 也就是说：一次广播都没看成，全程拿一个地址硬连。
         * 顺序调到这里，失败后先恢复扫描，瞎试只在「扫描没在跑」时才发生。
         */
        if (s_scan_continuous && !s_scanning && !s_connect_in_progress &&
            br_ble_connected_count() == 0) {
            /*
             * ★ 已经连上一台就别再扫了 ★
             *   扫描和连接抢控制器，遥控器会判定拥塞直接断链。
             */
            br_ble_scan_start(true);
        }

        /*
         * ★ 一个遥控器都没配 → 自动开着配对窗口 ★
         *
         *   蓝灯闪着就是在等人来配对，这时板子应该**来者不拒**，
         *   而不是等 PC 发 PAIRWINDOW 才收。
         *   （用户实测：删掉所有配对后板子闪蓝灯、遥控器进配对模式，
         *     两者一直建不起连接 —— 因为根本没人开窗，
         *     而且残留的旧 bond 还让双方都配不成。）
         */
        if (pl_count() == 0 && br_ble_connected_count() == 0) {
            bridge_led_set_pairing(true);
            s_auto_pair_until = esp_timer_get_time() + 30LL * 1000000LL;
            s_scan_continuous = true;
            if (!s_scanning) {
                br_ble_scan_start(true);
            }
        }

        /* 配对窗口到点了就收工 */
        if (s_auto_pair_until && esp_timer_get_time() >= s_auto_pair_until) {
            s_auto_pair_until = 0;
            if (!s_pair_pending) {
                s_scan_continuous = s_scan_continuous_before_pair;
                br_ble_scan_stop();
            }
            ESP_LOGI(TAG, "pair window closed");
        }

        /*
         * ★ 配对重试 ★
         *
         * 点「配对」后固件持续尝试，遥控器什么时候醒都接得住。
         */
        if (s_pair_pending && !s_connect_in_progress &&
            !s_pair_fresh_ready &&
            br_ble_connected_count() == 0) {
            int64_t now = esp_timer_get_time();
            if (now >= s_pair_retry_until) {
                pair_window_close();

                char detail[64];
                if (s_last_conn_status != 0) {
                    conn_status_desc(s_last_conn_status, detail, sizeof(detail));
                } else {
                    snprintf(detail, sizeof(detail),
                             "never-seen-in-%llds-window",
                             (long long)(PAIR_RETRY_WINDOW_US / 1000000LL));
                }
                br_kv_t pkv;
                br_kv_init(&pkv);
                br_kv_add_int(&pkv, "ok", 0);
                br_kv_add_int(&pkv, "status", s_last_conn_status);
                br_kv_add(&pkv, "why", detail);
                br_proto_send_ev("PAIR", &pkv);
                ESP_LOGW(TAG, "PAIR 放弃: %s", detail);
            } else if (!s_scanning && now >= s_next_pair_retry) {
                /*
                 * ★ 只在「现在没在扫描」时才瞎试 ★
                 *
                 * 之前每 1.5 秒无脑试一次，一次占 5 秒，
                 * 结果 s_connect_in_progress 几乎一直是 1，
                 * 「扫描停下来就重新拉起来」那条永远执行不到 ——
                 * 新鲜广播路径等于被自己饿死了（实测日志：
                 * 整个配对期间 scanning 一直是 0，只有连续 CONNFAIL）。
                 *
                 * 地址类型也不再交替试：交替是"猜 bond 记错了"的兜底，
                 * 但用错类型的连接**必然**超时，白占射频。
                 * 类型以扫描到的（或 PC 传进来的）为准。
                 */
                s_next_pair_retry = now + PAIR_RETRY_GAP_US;
                br_ble_connect_addr(s_pair_addr, s_pair_type, s_pair_name);
            }
        }

        /*
         * ★ bond 自动重连 ★
         *
         * 以前这里是「无条件调 br_ble_autoreconnect_tick()」，
         * 结果下面的「扫一轮 / 连一次」两拍轮换**永远轮不到扫描那一拍**
         * —— 连接尝试一上来就占住 5 秒（超时），
         * 实测日志里一次 "scanning started" 都没有，全是 CONNFAIL。
         *
         * 现在统一交给下面那个自动模式块处理。
         */

        /*
         * 自动运行时持续静默扫描：低功耗遥控器常用轮换 RPA，
         * 旧 bond 地址无法唤醒它。扫描回调只记录已登记遥控器的新鲜地址，
         * 实际连接由上面的主循环分支发起；已有连接时停止扫描。
         */
        if (s_auto && br_ble_connected_count() == 0 &&
            !s_connect_in_progress && !s_scan_continuous) {
            /*
             * ★ 自动模式：只扫，看到广播才连 ★
             *
             * 低功耗遥控器（Chromecast 那种）只在被按键唤醒的那一两秒里广播，
             * 而且地址是会轮换的 RPA —— 拿旧地址盲连永远连不上
             * （实测 10 分钟 115 次全超时）。
             *
             * 所以自动模式下**一直扫**，等看到它醒来的那一下广播，
             * 立刻用「当轮的地址 + 地址类型」连过去
             * （scan_event 里 is_tracked_addr() 那条路径）。
             * 连的时候会自动停扫描，连上后也不再扫（避免抢控制器）。
             */
            if (now >= s_next_pick) {
                s_next_pick = now + 100000;   /* 每轮约 1 秒，歇 100ms 后续扫 */
                if (!s_scanning) {
                    br_ble_scan_round_quiet();
                }
            }
            /*
             * 兜底：60 秒一次盲连。
             * 对「地址固定、醒着也不广播」的遥控器有用；
             * 对地址会轮换的（Chromecast）没用，但代价很小。
             */
            if (now >= s_next_blind) {
                s_next_blind = now + 60000000;
                br_ble_autoreconnect_tick();
            }
        }

        if (now - last_state_log > 5000000) {
            last_state_log = now;
            br_ble_dump_state();
        }

        vTaskDelay(pdMS_TO_TICKS(30));
    }
}

/* ------------------------------------------------------------------ 初始化 */

/*
 * ★★★ 本机身份地址兜底 ★★★
 *
 * 起因（2026-11-06）：配对每次都 rc=0 发起成功、5 秒后必然 status=13，
 * 而扫描、收广播全正常 —— 唯一没验证过的假设是
 * 「CONNECT_IND 里的 InitiatorA 是不是有效的」。
 *
 * ★ 结论：这条假设**不成立**，已经用实测否掉了 ★
 *   EV STATE 实测：pub=28:84:85:92:44:2A rnd=rc=21(ENOENT) own=0
 *   本机公版地址是好的，发起连接用的就是它。
 *   （一开始这里显示 pub=none rnd=none，是我把
 *     ble_hs_id_copy_addr() 的第三个参数当成"长度"用、还要求 ==6，
 *     实际那是 NRPA 标志 —— API 用错时连自己打的证据都是假的。）
 *
 * 这个函数因此**只是兜底**，不是本次问题的解药，别当根因看待：
 *   - 公版地址可用 → 什么都不做（本机就是这种情况）
 *   - 公版地址全 0 / 全 FF（MAC 未烧录的山寨板）→
 *     从 eFuse 找一个可用 MAC 派生**固定**的 random static 装上。
 *     「固定」很关键：遥控器一旦跟我们绑定，地址再变它就不认识我们了，
 *     表现就是"每次重启都要重新配对"。
 *     旧版就是踩了这个坑：它把远端地址类型当第一个参数传，
 *     type=1 那次等于 own_addr_type=BLE_OWN_ADDR_RANDOM，
 *     NimBLE 发现没有 random 身份就自动生成一个 —— 能连，
 *     但每次开机地址都换，所以旧版的"配对成功"不可复现。
 */
static void setup_own_identity_addr(void)
{
    uint8_t id[6];
    int nrpa = 0;
    const char *how;

    /* 1) 公版地址本来可用就什么都不用做 */
    if (ble_hs_id_copy_addr(BLE_ADDR_PUBLIC, id, &nrpa) == 0 &&
        !addr_is_all_zero(id) && !addr_is_all_ff(id)) {
        char s[24];
        addr_to_str(id, s, sizeof(s));
        ESP_LOGI(TAG, "本机公版地址可用：%s —— 继续用 BLE_OWN_ADDR_PUBLIC", s);
        return;
    }
    ESP_LOGW(TAG, "本机没有可用的公版地址（BT MAC 未烧录？）；改配固定 random static");

    /* 2) 从 eFuse 里找一个可用的 MAC 当种子 */
    memset(id, 0, sizeof(id));
    how = "none";
    static const esp_mac_type_t try_types[] = {
        ESP_MAC_BT, ESP_MAC_BASE, ESP_MAC_WIFI_STA, ESP_MAC_WIFI_SOFTAP,
    };
    for (size_t i = 0; i < sizeof(try_types) / sizeof(try_types[0]); i++) {
        uint8_t m[6];
        if (esp_read_mac(m, try_types[i]) != ESP_OK) {
            continue;
        }
        if (addr_is_all_zero(m) || addr_is_all_ff(m)) {
            continue;
        }
        memcpy(id, m, 6);
        how = "efuse";
        break;
    }
    if (strcmp(how, "efuse") != 0) {
        /*
         * 一个 MAC 都没有 —— 只能现场生成。
         * 代价必须说清楚：每次开机地址不同，配对保不住。
         */
        esp_fill_random(id, 6);
        how = "random(!)";
        ESP_LOGE(TAG, "eFuse 里一个可用 MAC 都没有，本机地址每次开机都会变 —— "
                      "重启后必须重新配对");
    }

    /*
     * 3) 改成合法的 static random：最高字节（小端存放，即 id[5]）
     *    高 2 位必须是 11。
     */
    id[5] = (uint8_t)((id[5] & 0x3F) | 0xC0);

    int rc = ble_hs_id_set_rnd(id);
    char s[24];
    addr_to_str(id, s, sizeof(s));
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_hs_id_set_rnd(%s) 失败 rc=%d(%s)",
                 s, rc, hs_err_name(rc));
        return;
    }
    s_own_addr_type = BLE_OWN_ADDR_RANDOM;
    ESP_LOGI(TAG, "本机身份地址已设为 RANDOM %s（种子=%s）；"
                  "连接改用 own=%d", s, how, (int)s_own_addr_type);
}

static void on_sync(void)
{
    ESP_LOGI(TAG, "BLE host synced");
    setup_own_identity_addr();
    /* 开机后自主运行；扫描 API 只能在 host 同步完成后调用。 */
    br_ble_set_auto(true);
}

static void on_reset(int reason)
{
    ESP_LOGW(TAG, "BLE host reset: %d", reason);
}

/** NimBLE 主机任务 —— 这个函数在 nimble_port_stop() 之前不会返回 */
static void nimble_host_task_fn(void *param)
{
    (void)param;
    ESP_LOGI(TAG, "BLE host task started");
    nimble_port_run();
    nimble_port_freertos_deinit();
}

esp_err_t br_ble_init(void)
{
    memset(s_peers, 0, sizeof(s_peers));
    memset(s_candidates, 0, sizeof(s_candidates));
    s_peer_count = 0;
    s_candidate_count = 0;

    /*
     * ★★★ 顺序很重要：先配 ble_hs_cfg，再 nimble_port_init() ★★★
     *   nimble_port_init() 会触发 host 与控制器同步，
     *   同步完成时调用 ble_hs_cfg.sync_cb。
     *   如果那时 sync_cb 还没设置，就永远收不到同步通知，
     *   ble_hs_start() 也不会被调用 —— 所有 GAP API 返回 EDISABLED(30)。
     */
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;

    /*
     * ★ IO 能力必须是 NO_INPUT_OUTPUT（Just Works）★
     *
     * 遥控器既没有屏幕也没有键盘 —— 它**没法**完成
     * 「主机显示 PIN、遥控器确认」这种需要用户交互的配对方式。
     * 设成 KEYBOARD_DISPLAY 的话，遥控器收到配对请求后无法完成流程，
     * 连接就被丢弃 —— 表现出来就是 status=13 连接超时。
     *
     * 只有 Just Works（NO_INPUT_OUTPUT + MITM 关闭）才能和这种遥控器配对。
     */
    ble_hs_cfg.sm_io_cap = BLE_HS_IO_NO_INPUT_OUTPUT;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 0;   /* 遥控器没有屏幕可显示 passkey，MITM 会导致配不上 */
    ble_hs_cfg.sm_sc = 1;    /* 优先 LE Secure Connections */

    esp_err_t ret = nimble_port_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed: %d", ret);
        return ret;
    }

    /* 通知队列（ATT 回调只入队，主循环负责发协议） */
    if (!s_auq) {
        s_auq = xQueueCreate(AUQ_LEN, sizeof(au_item_t));
        if (!s_auq) {
            ESP_LOGE(TAG, "notify queue alloc failed");
            return ESP_ERR_NO_MEM;
        }
    }
    if (!s_keyq) {
        s_keyq = xQueueCreate(KEYQ_LEN, sizeof(br_hid_key_t));
        if (!s_keyq) {
            ESP_LOGE(TAG, "key queue alloc failed");
            return ESP_ERR_NO_MEM;
        }
        br_hid_set_key_handler(on_hid_key, NULL);
    }
    br_hid_reset();

    /* 本固件只做 Central，不需要 GATT 服务端 */
    nimble_port_freertos_init(nimble_host_task_fn);

    xTaskCreate(br_ble_task, "br_ble", 6144, NULL, 5, NULL);
    ESP_LOGI(TAG, "BLE host initialised");
    return ESP_OK;
}
