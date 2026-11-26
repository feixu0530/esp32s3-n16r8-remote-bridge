#include "br_voice_input.h"

#define BR_VOICE_INPUT_RELEASE_GRACE_MS 80u

static int8_t s_peer = -1;
static bool s_active;
static bool s_down[2];
static bool s_release_pending;
static uint32_t s_release_at;

static void clear_session(void)
{
    s_peer = -1;
    s_active = false;
    s_down[0] = false;
    s_down[1] = false;
    s_release_pending = false;
    s_release_at = 0;
}

void br_voice_input_reset(void)
{
    clear_session();
}

br_voice_input_result_t br_voice_input_update(int8_t peer_idx,
                                               br_voice_input_source_t source,
                                               bool down,
                                               uint32_t now_ms)
{
    if (peer_idx < 0 || (source != BR_VOICE_INPUT_CONTROL &&
                         source != BR_VOICE_INPUT_KEYBOARD)) {
        return BR_VOICE_INPUT_NONE;
    }

    if (!s_active) {
        if (!down) {
            return BR_VOICE_INPUT_NONE;
        }
        clear_session();
        s_peer = peer_idx;
        s_active = true;
        s_down[source] = true;
        return BR_VOICE_INPUT_PRESSED;
    }

    if (peer_idx != s_peer || s_down[source] == down) {
        return BR_VOICE_INPUT_NONE;
    }

    s_down[source] = down;
    if (down) {
        s_release_pending = false;
        return BR_VOICE_INPUT_NONE;
    }

    if (s_down[BR_VOICE_INPUT_CONTROL] || s_down[BR_VOICE_INPUT_KEYBOARD]) {
        s_release_pending = true;
        s_release_at = now_ms + BR_VOICE_INPUT_RELEASE_GRACE_MS;
        return BR_VOICE_INPUT_NONE;
    }

    clear_session();
    return BR_VOICE_INPUT_RELEASED;
}

br_voice_input_result_t br_voice_input_tick(uint32_t now_ms)
{
    if (!s_active || !s_release_pending ||
        (int32_t)(now_ms - s_release_at) < 0) {
        return BR_VOICE_INPUT_NONE;
    }

    clear_session();
    return BR_VOICE_INPUT_RELEASED;
}

br_voice_input_result_t br_voice_input_peer_reset(int8_t peer_idx,
                                                  uint32_t now_ms)
{
    (void)now_ms;
    if (!s_active || peer_idx != s_peer) {
        return BR_VOICE_INPUT_NONE;
    }
    clear_session();
    return BR_VOICE_INPUT_RELEASED;
}

bool br_voice_input_active(void)
{
    return s_active;
}

int8_t br_voice_input_peer(void)
{
    return s_active ? s_peer : -1;
}
