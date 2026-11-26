#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/** True only for a 60-byte Xiaomi mSBC notification value. */
bool br_msbc_frame_ok(const uint8_t *data, size_t len);

/** Reset the decoder before each new voice session. */
bool br_msbc_reset(void);

/**
 * Decode one 60-byte notification into 120 PCM16 samples at 16 kHz.
 * data[1] is the H2 sequence byte; data[2..58] is the 57-byte mSBC frame.
 */
bool br_msbc_decode(const uint8_t *data, size_t len,
                    int16_t pcm[120], uint32_t *h2);
