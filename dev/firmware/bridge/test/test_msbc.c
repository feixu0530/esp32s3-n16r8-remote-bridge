#include "br_msbc.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FRAME_BYTES 60
#define DEFAULT_FIXTURE "dev/firmware/bridge/test/fixtures/xiaomi_voice_60b.bin"

static void fail(const char *why)
{
    fprintf(stderr, "FAIL %s\n", why);
    exit(1);
}

int main(int argc, char **argv)
{
    const char *path = argc > 1 ? argv[1] : DEFAULT_FIXTURE;
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "FAIL open %s\n", path);
        return 1;
    }
    if (fseek(f, 0, SEEK_END) != 0) fail("seek");
    long size = ftell(f);
    if (size <= 0 || size % FRAME_BYTES != 0) fail("fixture size");
    rewind(f);
    uint8_t *blob = malloc((size_t)size);
    if (!blob || fread(blob, 1, (size_t)size, f) != (size_t)size) fail("read");
    fclose(f);

    int frames = (int)(size / FRAME_BYTES);
    if (frames < 50) fail("fewer than 50 frames");
    if (!br_msbc_reset()) fail("decoder reset");

    double energy = 0.0;
    int errors = 0;
    int h2[4] = {0};
    for (int i = 0; i < frames; i++) {
        const uint8_t *frame = blob + i * FRAME_BYTES;
        if (!br_msbc_frame_ok(frame, FRAME_BYTES)) fail("frame signature");
        int16_t pcm[120];
        uint32_t seq = 0;
        if (!br_msbc_decode(frame, FRAME_BYTES, pcm, &seq)) {
            errors++;
            continue;
        }
        switch (seq) {
        case 0x08: h2[0]++; break;
        case 0x38: h2[1]++; break;
        case 0xC8: h2[2]++; break;
        case 0xF8: h2[3]++; break;
        default: fail("unexpected H2");
        }
        for (int j = 0; j < 120; j++) {
            energy += (double)pcm[j] * (double)pcm[j];
        }
    }
    if (errors != 0) fail("valid reference frames did not decode");
    double rms = sqrt(energy / ((double)frames * 120.0));
    if (rms < 1.0) fail("decoded audio is silent");

    uint8_t corrupt[FRAME_BYTES];
    memcpy(corrupt, blob, sizeof(corrupt));
    corrupt[3] ^= 0x01;   /* mSBC header CRC covers this byte */
    int16_t discarded[120];
    if (br_msbc_decode(corrupt, sizeof(corrupt), discarded, NULL)) {
        fail("corrupted frame was accepted");
    }

    free(blob);
    printf("fixture_frames=%d decode_errors=%d rms=%.2f h2=%d/%d/%d/%d\n",
           frames, errors, rms, h2[0], h2[1], h2[2], h2[3]);
    puts("全部通过");
    return 0;
}
