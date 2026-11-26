#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "br_voice_input.h"

static void expect(br_voice_input_result_t got, br_voice_input_result_t want,
                   const char *name)
{
    if (got != want) {
        fprintf(stderr, "FAIL %s: got=%d want=%d\n", name, (int)got, (int)want);
        exit(1);
    }
}

static void test_dual_source_grace(void)
{
    br_voice_input_reset();
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, true, 100),
           BR_VOICE_INPUT_PRESSED, "control press");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, true, 101),
           BR_VOICE_INPUT_NONE, "keyboard duplicate press");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, false, 102),
           BR_VOICE_INPUT_NONE, "wait for keyboard release");
    expect(br_voice_input_tick(181), BR_VOICE_INPUT_NONE, "grace not elapsed");
    expect(br_voice_input_tick(182), BR_VOICE_INPUT_RELEASED, "grace elapsed");
    expect(br_voice_input_active(), false, "inactive after grace");
}

static void test_single_source_and_duplicates(void)
{
    br_voice_input_reset();
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, true, 200),
           BR_VOICE_INPUT_PRESSED, "keyboard fallback press");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, true, 201),
           BR_VOICE_INPUT_NONE, "duplicate down");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, false, 202),
           BR_VOICE_INPUT_RELEASED, "single source release");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, false, 203),
           BR_VOICE_INPUT_NONE, "duplicate up");
}

static void test_reverse_release_and_grace_cancel(void)
{
    br_voice_input_reset();
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, true, 300),
           BR_VOICE_INPUT_PRESSED, "keyboard press");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, true, 301),
           BR_VOICE_INPUT_NONE, "control duplicate press");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, false, 302),
           BR_VOICE_INPUT_NONE, "reverse grace starts");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, false, 310),
           BR_VOICE_INPUT_RELEASED, "second release in grace");

    br_voice_input_reset();
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, true, 400),
           BR_VOICE_INPUT_PRESSED, "press before grace cancel");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, true, 401),
           BR_VOICE_INPUT_NONE, "second source down");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, false, 402),
           BR_VOICE_INPUT_NONE, "grace starts");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, true, 403),
           BR_VOICE_INPUT_NONE, "repress cancels grace");
    expect(br_voice_input_tick(500), BR_VOICE_INPUT_NONE, "cancelled grace");
    expect(br_voice_input_active(), true, "still active after cancel");
}

static void test_peer_ownership(void)
{
    br_voice_input_reset();
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, true, 500),
           BR_VOICE_INPUT_PRESSED, "peer zero press");
    expect(br_voice_input_peer(), 0, "owner peer");
    expect(br_voice_input_update(1, BR_VOICE_INPUT_KEYBOARD, true, 501),
           BR_VOICE_INPUT_NONE, "other peer ignored");
    expect(br_voice_input_peer_reset(1, 502), BR_VOICE_INPUT_NONE,
           "other peer reset ignored");
    expect(br_voice_input_peer_reset(0, 503), BR_VOICE_INPUT_RELEASED,
           "owner peer reset releases");
    expect(br_voice_input_peer(), -1, "no owner after reset");
    expect(br_voice_input_update(1, BR_VOICE_INPUT_KEYBOARD, true, 504),
           BR_VOICE_INPUT_PRESSED, "other peer can press later");
}

static void test_time_wrap(void)
{
    uint32_t start = UINT32_MAX - 10u;
    uint32_t release_at = start + 20u;
    uint32_t grace_end = release_at + 80u;

    br_voice_input_reset();
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, true, start),
           BR_VOICE_INPUT_PRESSED, "wrap press");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_KEYBOARD, true, start + 1u),
           BR_VOICE_INPUT_NONE, "wrap second source");
    expect(br_voice_input_update(0, BR_VOICE_INPUT_CONTROL, false, release_at),
           BR_VOICE_INPUT_NONE, "wrap grace starts");
    expect(br_voice_input_tick(grace_end - 1u), BR_VOICE_INPUT_NONE,
           "wrap grace not elapsed");
    expect(br_voice_input_tick(grace_end), BR_VOICE_INPUT_RELEASED,
           "wrap grace elapsed");
}

int main(void)
{
    test_dual_source_grace();
    test_single_source_and_duplicates();
    test_reverse_release_and_grace_cancel();
    test_peer_ownership();
    test_time_wrap();
    puts("全部通过");
    return 0;
}
