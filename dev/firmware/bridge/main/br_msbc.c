#include "br_msbc.h"

#include <string.h>

#include "oi_codec_sbc.h"

#define XIAOMI_MSBC_PAYLOAD 57
#define XIAOMI_PCM_BYTES    240

static OI_CODEC_SBC_DECODER_CONTEXT s_ctx;
static OI_CODEC_SBC_CODEC_DATA_MONO s_codec_data;
static bool s_ready;
static uint8_t s_bad_run;

static bool h2_valid(uint8_t value)
{
    return value == 0x08 || value == 0x38 || value == 0xC8 || value == 0xF8;
}

bool br_msbc_frame_ok(const uint8_t *data, size_t len)
{
    return data && len == 60 && data[0] == 0x01 && h2_valid(data[1]) &&
           data[2] == OI_mSBC_SYNCWORD;
}

bool br_msbc_reset(void)
{
    OI_STATUS status = OI_CODEC_SBC_DecoderReset(
        &s_ctx, s_codec_data.data, sizeof(s_codec_data.data),
        1, 1, FALSE, TRUE);
    s_ready = OI_SUCCESS(status);
    s_bad_run = 0;
    return s_ready;
}

bool br_msbc_decode(const uint8_t *data, size_t len,
                    int16_t pcm[120], uint32_t *h2)
{
    if (!pcm || !br_msbc_frame_ok(data, len)) {
        return false;
    }
    if (!s_ready && !br_msbc_reset()) {
        return false;
    }

    const OI_BYTE *frame = data + 2;
    OI_UINT32 frame_bytes = XIAOMI_MSBC_PAYLOAD;
    OI_UINT32 pcm_bytes = XIAOMI_PCM_BYTES;
    OI_STATUS status = OI_CODEC_SBC_DecodeFrame(
        &s_ctx, &frame, &frame_bytes, pcm, &pcm_bytes);

    if (!OI_SUCCESS(status) || pcm_bytes != XIAOMI_PCM_BYTES ||
        frame_bytes != 0) {
        if (++s_bad_run >= 3) {
            (void)br_msbc_reset();
        }
        return false;
    }

    s_bad_run = 0;
    if (h2) {
        *h2 = data[1];
    }
    return true;
}
