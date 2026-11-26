/*
 * encode_main.c —— 测试用：把 WAV 编码成 G.722.1 裸码流
 *
 * 用法： encode.exe in.wav out.raw [bitrate]
 *
 * 这是**测试辅助工具**，不参与运行时链路。
 * 运行时只有解码方向（decode.exe）。
 *
 * 许可：本文件由本项目编写（MIT 许可的项目内工具）。
 * 它链接了 GPL-2.0 的 libg722_1，所以这个二进制按 GPL-2.0 分发。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "g722_1/g722_1.h"

int main(int argc, char *argv[])
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s in.wav out.raw [bitrate]\n", argv[0]);
        return 1;
    }

    int bit_rate = 16000;
    if (argc > 3) {
        bit_rate = atoi(argv[3]);
        if (bit_rate <= 0) {
            bit_rate = 16000;
        }
    }

    /* Windows 上用 fopen 读 wav（wave.h 需要 FILE*，自己解析 RIFF） */
    FILE *f = fopen(argv[1], "rb");
    if (!f) {
        perror("open wav");
        return 1;
    }

    /* 解析 RIFF/WAVE，找 fmt 与 data 块 */
    unsigned char hdr[12];
    if (fread(hdr, 1, 12, f) != 12 ||
        memcmp(hdr, "RIFF", 4) != 0 || memcmp(hdr + 8, "WAVE", 4) != 0) {
        fprintf(stderr, "not a RIFF/WAVE file\n");
        fclose(f);
        return 1;
    }

    uint16_t channels = 1, bits = 16, format = 1;
    uint32_t rate = 16000;
    long data_pos = -1, data_len = 0;
    int have_fmt = 0;

    for (;;) {
        unsigned char ch[8];
        if (fread(ch, 1, 8, f) != 8) {
            break;
        }
        uint32_t sz = (uint32_t)ch[4] | ((uint32_t)ch[5] << 8) |
                      ((uint32_t)ch[6] << 16) | ((uint32_t)ch[7] << 24);
        long pos = ftell(f);
        if (memcmp(ch, "fmt ", 4) == 0) {
            unsigned char fmt[16];
            if (fread(fmt, 1, 16, f) < 16) {
                break;
            }
            format    = (uint16_t)(fmt[0] | (fmt[1] << 8));
            channels  = (uint16_t)(fmt[2] | (fmt[3] << 8));
            rate      = (uint32_t)(fmt[4] | (fmt[5] << 8) | (fmt[6] << 16) | ((uint32_t)fmt[7] << 24));
            bits      = (uint16_t)(fmt[14] | (fmt[15] << 8));
            have_fmt = 1;
            fseek(f, pos + sz, SEEK_SET);
        } else if (memcmp(ch, "data", 4) == 0) {
            data_pos = ftell(f);
            data_len = (long)sz;
            fseek(f, pos + sz, SEEK_SET);
        } else {
            fseek(f, pos + sz + (sz & 1), SEEK_SET);
        }
    }

    if (!have_fmt || data_pos < 0) {
        fprintf(stderr, "missing fmt/data chunk\n");
        fclose(f);
        return 1;
    }
    if (format != 1 || bits != 16) {
        fprintf(stderr, "only 16-bit PCM supported (format=%u bits=%u)\n", format, bits);
        fclose(f);
        return 1;
    }
    if (rate != 16000) {
        fprintf(stderr, "warning: resampling %u -> 16000 not implemented\n", rate);
    }

    fseek(f, data_pos, SEEK_SET);

    FILE *fo = fopen(argv[2], "wb");
    if (!fo) {
        perror("open output");
        fclose(f);
        return 1;
    }

    g722_1_encode_state_t *st = g722_1_encode_init(NULL, bit_rate, 16000);
    if (!st) {
        fprintf(stderr, "encode init failed\n");
        fclose(f); fclose(fo);
        return 1;
    }

    int frame_samples = 16000 / 50;          /* 20ms */
    int frame_bytes = (bit_rate / 50) / 8;   /* 16kbps -> 20 字节 */
    int16_t pcm[2048];
    uint8_t out[512];

    for (;;) {
        /* 读一帧采样（多声道取第一声道） */
        int16_t mono[2048];
        int got = 0;
        while (got < frame_samples) {
            int16_t s[2048];
            size_t want = (size_t)(frame_samples - got) * channels;
            size_t n = fread(s, 2, want, f);
            if (n == 0) {
                break;
            }
            for (size_t i = 0; i < n / channels; i++) {
                mono[got++] = s[i * channels];
            }
        }
        if (got == 0) {
            break;
        }
        if (got < frame_samples) {
            memset(mono + got, 0, (size_t)(frame_samples - got) * sizeof(int16_t));
        }

        /* len 是采样数，不是字节数 */
        int nsamp = g722_1_encode(st, out, mono, frame_samples);
        if (nsamp <= 0) {
            break;
        }
        fwrite(out, 1, (size_t)nsamp, fo);
    }

    g722_1_encode_release(st);
    fclose(f);
    fclose(fo);
    fprintf(stderr, "encoded -> %s\n", argv[2]);
    return 0;
}
