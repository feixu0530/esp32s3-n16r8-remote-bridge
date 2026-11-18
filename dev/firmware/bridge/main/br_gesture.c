/*
 * br_gesture - 板端按键手势引擎（实现）
 */
#include "br_gesture.h"

#include <string.h>

#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#define GESTURE_TAG "br_gesture"

typedef struct {
    bool     used;
    uint8_t  addr[6];
    uint32_t key_id;
    bool     is_pressed;
    bool     is_second_press;
    bool     waiting_for_second_press;
    bool     long_press_triggered;
    bool     recognizes_double;
    bool     recognizes_long;
    bool     immediate_fired;   /* 快路径：按下即触发单击 */
    int64_t  long_deadline_ms;
    int64_t  double_deadline_ms;
} br_gesture_state_t;

static br_gesture_state_t s_states[BR_GESTURE_MAX];
static br_gesture_has_fn s_has;
static br_gesture_fire_fn s_fire;
static void *s_ctx;
static SemaphoreHandle_t s_lock;

static void gesture_task(void *arg);

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

/* ------------------------------------------------------------------ 状态表 */

static br_gesture_state_t *find_state(const uint8_t addr[6], uint32_t key_id)
{
    for (int i = 0; i < BR_GESTURE_MAX; i++) {
        if (s_states[i].used && s_states[i].key_id == key_id &&
            memcmp(s_states[i].addr, addr, 6) == 0) {
            return &s_states[i];
        }
    }
    return NULL;
}

static br_gesture_state_t *alloc_state(const uint8_t addr[6], uint32_t key_id)
{
    for (int i = 0; i < BR_GESTURE_MAX; i++) {
        if (!s_states[i].used) {
            memset(&s_states[i], 0, sizeof(s_states[i]));
            s_states[i].used = true;
            s_states[i].key_id = key_id;
            memcpy(s_states[i].addr, addr, 6);
            return &s_states[i];
        }
    }
    /* 满了就复用第一条（最老的那条） */
    memset(&s_states[0], 0, sizeof(s_states[0]));
    s_states[0].used = true;
    s_states[0].key_id = key_id;
    memcpy(s_states[0].addr, addr, 6);
    return &s_states[0];
}

static void drop_state(br_gesture_state_t *st)
{
    st->used = false;
}

/* ------------------------------------------------------------------ 回调 */

static bool has_trigger(const uint8_t addr[6], uint32_t key_id, uint8_t trig)
{
    if (s_has == NULL) {
        return false;
    }
    return s_has(s_ctx, addr, key_id, trig);
}

static void fire_trigger(const uint8_t addr[6], uint32_t key_id, uint8_t trig,
                         bool press)
{
    if (s_fire == NULL) {
        return;
    }
    s_fire(s_ctx, addr, key_id, trig, press);
}

/* ------------------------------------------------------------------ 对外 */

void br_gesture_init(br_gesture_has_fn has_fn, br_gesture_fire_fn fire_fn,
                      void *ctx)
{
    if (!s_lock) {
        s_lock = xSemaphoreCreateMutex();
    }
    memset(s_states, 0, sizeof(s_states));
    s_has = has_fn;
    s_fire = fire_fn;
    s_ctx = ctx;
    xTaskCreate(gesture_task, "gesture", 3072, NULL, 3, NULL);
    ESP_LOGI(GESTURE_TAG, "gesture engine ready (double=%ums long=%ums)",
             BR_GESTURE_DOUBLE_MS, BR_GESTURE_LONG_MS);
}

void br_gesture_press(const uint8_t addr[6], uint32_t key_id)
{
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }

    br_gesture_state_t *st = find_state(addr, key_id);
    if (st != NULL) {
        if (!st->waiting_for_second_press) {
            /* 已经在按住中（重复 down），忽略 */
            if (s_lock) {
                xSemaphoreGive(s_lock);
            }
            return;
        }
        /* 第二次按下 —— 双击成立 */
        st->is_pressed = true;
        st->is_second_press = true;
        st->waiting_for_second_press = false;
        st->double_deadline_ms = 0;
        if (st->recognizes_long) {
            st->long_deadline_ms = now_ms() + BR_GESTURE_LONG_MS;
        }
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        return;
    }

    bool recognizes_double = has_trigger(addr, key_id, BR_GESTURE_DOUBLE);
    bool recognizes_long = has_trigger(addr, key_id, BR_GESTURE_LONG);
    bool has_single = has_trigger(addr, key_id, BR_GESTURE_SINGLE);

    if (!recognizes_double && !recognizes_long) {
        /*
         * ★ 快路径 ★
         *   只配了单击的键按下就触发，不等任何窗口 ——
         *   这样现有的 Home → Ctrl+L 保持原来的手感。
         */
        if (!has_single) {
            if (s_lock) {
                xSemaphoreGive(s_lock);
            }
            return;
        }
        st = alloc_state(addr, key_id);
        st->is_pressed = true;
        st->immediate_fired = true;
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        fire_trigger(addr, key_id, BR_GESTURE_SINGLE, true);
        return;
    }

    st = alloc_state(addr, key_id);
    st->is_pressed = true;
    st->recognizes_double = recognizes_double;
    st->recognizes_long = recognizes_long;
    if (recognizes_long) {
        st->long_deadline_ms = now_ms() + BR_GESTURE_LONG_MS;
    }
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
}

void br_gesture_release(const uint8_t addr[6], uint32_t key_id)
{
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }

    br_gesture_state_t *st = find_state(addr, key_id);
    if (st == NULL || !st->is_pressed) {
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        /*
         * ★ 状态丢了也要补一次抬起 ★
         *
         *   以前这里直接 return —— 抬起事件就这么丢了，
         *   那个键永远按在 PC 上（系统自动重复）。
         *   用户看到的是「按下返回键，输入框的文字一下全被删掉」，
         *   而不是「按住缓慢连删、抬起停止」。
         */
        fire_trigger(addr, key_id, BR_GESTURE_SINGLE, false);
        fire_trigger(addr, key_id, BR_GESTURE_LONG, false);
        return;
    }
    st->is_pressed = false;

    if (st->immediate_fired) {
        /* 单击已经在按下时触发，这里只负责松开输出 */
        drop_state(st);
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        fire_trigger(addr, key_id, BR_GESTURE_SINGLE, false);
        return;
    }

    if (st->recognizes_long) {
        st->long_deadline_ms = 0;   /* 取消长按定时 */
    }

    if (st->long_press_triggered) {
        /* 长按已经触发过，补一次松开即可 */
        drop_state(st);
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        fire_trigger(addr, key_id, BR_GESTURE_LONG, false);
        return;
    }

    if (st->is_second_press) {
        drop_state(st);
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        fire_trigger(addr, key_id, BR_GESTURE_DOUBLE, true);
        fire_trigger(addr, key_id, BR_GESTURE_DOUBLE, false);
        return;
    }

    if (st->recognizes_double) {
        /* 单击推迟到双击窗口超时之后再触发 */
        st->waiting_for_second_press = true;
        st->double_deadline_ms = now_ms() + BR_GESTURE_DOUBLE_MS;
        if (s_lock) {
            xSemaphoreGive(s_lock);
        }
        return;
    }

    drop_state(st);
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
    fire_trigger(addr, key_id, BR_GESTURE_SINGLE, true);
    fire_trigger(addr, key_id, BR_GESTURE_SINGLE, false);
}

/**
 * 定时检查：长按超时、双击窗口超时。
 * 由内部任务每 10 ms 调一次。
 */
static void gesture_tick(void)
{
    int64_t now = now_ms();

    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }

    uint8_t fire_addr[6][6];
    uint32_t fire_key[6];
    uint8_t fire_trig[6];
    bool fire_press[6];
    int n = 0;

    for (int i = 0; i < BR_GESTURE_MAX && n < 6; i++) {
        br_gesture_state_t *st = &s_states[i];
        if (!st->used) {
            continue;
        }
        if (st->long_deadline_ms != 0 && now >= st->long_deadline_ms) {
            st->long_deadline_ms = 0;
            if (st->is_pressed && st->recognizes_long) {
                st->long_press_triggered = true;
                memcpy(fire_addr[n], st->addr, 6);
                fire_key[n] = st->key_id;
                fire_trig[n] = BR_GESTURE_LONG;
                fire_press[n] = true;
                n++;
            }
        }
        if (st->double_deadline_ms != 0 && now >= st->double_deadline_ms) {
            st->double_deadline_ms = 0;
            if (st->waiting_for_second_press && !st->is_pressed) {
                memcpy(fire_addr[n], st->addr, 6);
                fire_key[n] = st->key_id;
                fire_trig[n] = BR_GESTURE_SINGLE;
                fire_press[n] = true;
                n++;
                if (n < 6) {
                    memcpy(fire_addr[n], st->addr, 6);
                    fire_key[n] = st->key_id;
                    fire_trig[n] = BR_GESTURE_SINGLE;
                    fire_press[n] = false;
                    n++;
                }
                drop_state(st);
            }
        }
    }

    if (s_lock) {
        xSemaphoreGive(s_lock);
    }

    for (int i = 0; i < n; i++) {
        fire_trigger(fire_addr[i], fire_key[i], fire_trig[i], fire_press[i]);
    }
}

static void gesture_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10));
        gesture_tick();
    }
}

void br_gesture_reset(void)
{
    if (s_lock) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
    memset(s_states, 0, sizeof(s_states));
    if (s_lock) {
        xSemaphoreGive(s_lock);
    }
}

/* ------------------------------------------------------------------ 自检 */

/*
 * 自检用的假地址 / 假键号 —— 不会跟真实遥控器撞。
 */
static const uint8_t TEST_ADDR[6] = {0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA};
#define TEST_KEY_ID 0xDEADBEEFu

typedef struct {
    uint8_t allow_single;
    uint8_t allow_double;
    uint8_t allow_long;
    bool    fired_single;
    bool    fired_double;
    bool    fired_long;
    bool    fired_single_press;   /* 只记「按下」那一次 */
} test_probe_t;

static test_probe_t s_probe;

static bool test_has(void *ctx, const uint8_t addr[6], uint32_t key_id,
                     uint8_t trig)
{
    (void)ctx; (void)addr; (void)key_id;
    switch (trig) {
    case BR_GESTURE_SINGLE: return s_probe.allow_single != 0;
    case BR_GESTURE_DOUBLE: return s_probe.allow_double != 0;
    case BR_GESTURE_LONG:  return s_probe.allow_long != 0;
    default: return false;
    }
}

static void test_fire(void *ctx, const uint8_t addr[6], uint32_t key_id,
                      uint8_t trig, bool press)
{
    (void)ctx; (void)addr; (void)key_id;
    if (!press) {
        return;   /* 只关心「按下」那一次 */
    }
    switch (trig) {
    case BR_GESTURE_SINGLE: s_probe.fired_single = true; s_probe.fired_single_press = true; break;
    case BR_GESTURE_DOUBLE: s_probe.fired_double = true; break;
    case BR_GESTURE_LONG:  s_probe.fired_long = true; break;
    default: break;
    }
}

bool br_gesture_self_test(br_gesture_test_t *out)
{
    br_gesture_test_t result;
    memset(&result, 0, sizeof(result));

    /* 备份现场 */
    br_gesture_has_fn saved_has = s_has;
    br_gesture_fire_fn saved_fire = s_fire;
    void *saved_ctx = s_ctx;

    memset(&s_probe, 0, sizeof(s_probe));
    s_has = test_has;
    s_fire = test_fire;
    s_ctx = NULL;

    /* ---- 场景 1：只配单击 → 按下立即触发（快路径） ---- */
    s_probe.allow_single = 1;
    s_probe.allow_double = 0;
    s_probe.allow_long = 0;
    s_probe.fired_single = false;
    s_probe.fired_double = false;
    s_probe.fired_long = false;
    br_gesture_press(TEST_ADDR, TEST_KEY_ID);
    result.single_fired = s_probe.fired_single;
    br_gesture_release(TEST_ADDR, TEST_KEY_ID);

    /* ---- 场景 2：单击 + 双击 → 双击被识别，单击不补 ---- */
    s_probe.allow_single = 1;
    s_probe.allow_double = 1;
    s_probe.allow_long = 0;
    s_probe.fired_single = false;
    s_probe.fired_double = false;
    s_probe.fired_long = false;
    br_gesture_press(TEST_ADDR, TEST_KEY_ID);
    br_gesture_release(TEST_ADDR, TEST_KEY_ID);
    vTaskDelay(pdMS_TO_TICKS(60));      /* 间隔短于 300 ms 双击窗口 */
    br_gesture_press(TEST_ADDR, TEST_KEY_ID);
    br_gesture_release(TEST_ADDR, TEST_KEY_ID);
    result.double_fired = s_probe.fired_double;
    vTaskDelay(pdMS_TO_TICKS(BR_GESTURE_DOUBLE_MS + 120));
    result.single_after_double = s_probe.fired_single;

    /* ---- 场景 3：单击 + 长按 → 长按被识别，单击不补 ---- */
    s_probe.allow_single = 1;
    s_probe.allow_double = 0;
    s_probe.allow_long = 1;
    s_probe.fired_single = false;
    s_probe.fired_double = false;
    s_probe.fired_long = false;
    br_gesture_press(TEST_ADDR, TEST_KEY_ID);
    vTaskDelay(pdMS_TO_TICKS(BR_GESTURE_LONG_MS + 150));
    result.long_fired = s_probe.fired_long;
    br_gesture_release(TEST_ADDR, TEST_KEY_ID);
    vTaskDelay(pdMS_TO_TICKS(120));
    result.single_after_long = s_probe.fired_single;

    /* 恢复现场 */
    s_has = saved_has;
    s_fire = saved_fire;
    s_ctx = saved_ctx;
    br_gesture_reset();

    result.pass = result.single_fired && result.double_fired &&
                  result.long_fired &&
                  !result.single_after_double && !result.single_after_long;

    if (out) {
        *out = result;
    }
    ESP_LOGI(GESTURE_TAG,
             "selftest single=%d double=%d long=%d extra_single(db=%d lp=%d) pass=%d",
             result.single_fired, result.double_fired, result.long_fired,
             result.single_after_double, result.single_after_long,
             result.pass);
    return result.pass;
}
