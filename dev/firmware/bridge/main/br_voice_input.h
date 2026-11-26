#ifndef BR_VOICE_INPUT_H
#define BR_VOICE_INPUT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    BR_VOICE_INPUT_NONE = 0,
    BR_VOICE_INPUT_PRESSED,
    BR_VOICE_INPUT_RELEASED
} br_voice_input_result_t;

typedef enum {
    BR_VOICE_INPUT_CONTROL = 0,
    BR_VOICE_INPUT_KEYBOARD = 1
} br_voice_input_source_t;

void br_voice_input_reset(void);
br_voice_input_result_t br_voice_input_update(int8_t peer_idx,
                                               br_voice_input_source_t source,
                                               bool down,
                                               uint32_t now_ms);
br_voice_input_result_t br_voice_input_tick(uint32_t now_ms);
br_voice_input_result_t br_voice_input_peer_reset(int8_t peer_idx,
                                                  uint32_t now_ms);
bool br_voice_input_active(void);
int8_t br_voice_input_peer(void);

#ifdef __cplusplus
}
#endif

#endif
