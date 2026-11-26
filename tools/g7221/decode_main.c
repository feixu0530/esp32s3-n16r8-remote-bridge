/*
 * decode_main.c —— G.722.1 / Siren 7 裸码流解码器
 *
 * 用法： decode.exe in.raw out.wav [bitrate]
 *   in.raw    裸码流，每帧 bitrate/50/8 字节（16 kbps 时 20 字节）
 *   out.wav   16 kHz / 单声道 / 16 bit PCM
 *   bitrate   码率，默认 16000（注意是码率，不是采样率；采样率固定 16 kHz）
 *
 * 许可说明：
 *   本文件（文件读写 + WAV 头 + 逐帧调用）是本项目自己写的。
 *   libg722_1 是 GPL-2.0（freeswitch/libg7221，Steve Underwood 改编自
 *   Polycom 的 ITU-T G.722.1 参考代码），所以 decode.exe 这个二进制
 *   整体按 GPL-2.0 分发。
 *
 *   主程序只以**独立进程**调用本解码器（传文件路径，不链接），
 *   这样 GPL-2.0 只覆盖 decode.exe 本身，不会传染主程序 ——
 *   这是 libg722_1 README 的明确要求。
 *
 * G.722.1 的专利：
 *   Polycom 持有相关专利，但提供免版税许可条件。商业分发前
 *   请自行向 Polycom 确认。见 reference/README.md 第二节。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "g722_1/g722_1.h"

static void put32(FILE *f, uint32_t v)
{
    fputc(v & 0xFF, f);
    fputc((v >> 8) & 0xFF, f);
    fputc((v >> 16) & 0xFF, f);
    fputc((v >> 24) & 0xFF, f);
}

static void put16(FILE *f, uint16_t v)
{
    fputc(v & 0xFF, f);
    fputc((v >> 8) & 0xFF, f);
}

int main(int argc, char *argv[])
{
    FILE *fi, *fo;
    g722_1_decode_state_t *st;
    int bit_rate = 16000;
    int err = 0;
    size_t frame_bytes, done = 0, total = 0;
    uint8_t buf[1024];
    int16_t pcm[4096];
    uint32_t data_bytes = 0;
    long nsamples = 0;

    if (argc < 3) {
        fprintf(stderr, "usage: %s in.raw out.wav [bitrate]\n", argv[0]);
        return 1;
    }
    if (argc > 3) {
        bit_rate = atoi(argv[3]);
        if (bit_rate <= 0) {
            bit_rate = 16000;
        }
    }

    fi = fopen(argv[1], "rb");
    if (!fi) {
        perror("open input");
        return 1;
    }
    fo = fopen(argv[2], "wb");
    if (!fo) {
        perror("open output");
        fclose(fi);
        return 1;
    }

    st = g722_1_decode_init(NULL, bit_rate, 16000);
    if (!st) {
        fprintf(stderr, "decode init failed for rate %d\n", bit_rate);
        fclose(fi);
        fclose(fo);
        return 1;
    }

    /* G.722.1 每帧 20ms：帧字节数 = bitrate / 50 / 8 */
    frame_bytes = (size_t)((bit_rate / 50) / 8);
    if (frame_bytes == 0) {
        frame_bytes = 20;
    }
    fprintf(stderr, "bit_rate=%d frame_bytes=%zu samples/frame=%d\n",
            bit_rate, frame_bytes, (int)MAX_FRAME_SIZE / 2);

    /* WAV 头占位，最后回填 */
    fwrite("RIFF", 1, 4, fo);
    put32(fo, 0);
    fwrite("WAVEfmt ", 1, 8, fo);
    put32(fo, 16);
    put16(fo, 1);            /* PCM */
    put16(fo, 1);            /* mono */
    put32(fo, 16000);        /* sample rate */
    put32(fo, 16000 * 2);    /* byte rate */
    put16(fo, 2);            /* block align */
    put16(fo, 16);           /* bits per sample */
    fwrite("data", 1, 4, fo);
    put32(fo, 0);

    /*
     * 解码循环。
     *
     * g722_1_decode(s, amp, data, len) 的 len 是**码流字节数**，
     * 内部会按帧长循环解码（number_of_bits_per_frame/8 字节一帧），
     * 返回值是解出的**采样数**。
     *
     * 所以这里一次读一大块（比如 1 秒的码流）交给它，
     * 而不是逐帧调用 —— 逐帧调用会让最后一帧的边界处理变复杂。
     */
    size_t chunk_bytes = (size_t)frame_bytes * 50;   /* 50 帧 = 1 秒 */
    if (chunk_bytes > sizeof(buf)) {
        chunk_bytes = sizeof(buf);
    }

    while (!done) {
        size_t n = fread(buf, 1, chunk_bytes, fi);
        if (n == 0) {
            break;
        }
        done = 1;
        if (n < chunk_bytes) {
            /* 不足一块：补零到整帧边界，保证最后一帧能被解出来 */
            size_t rem = n % frame_bytes;
            if (rem != 0) {
                memset(buf + n, 0, frame_bytes - rem);
                n += frame_bytes - rem;
            }
        }

        int nsamp = g722_1_decode(st, pcm, buf, (int)n);
        if (nsamp < 0) {
            fprintf(stderr, "decode error\n");
            err = 1;
            break;
        }
        if (nsamp == 0) {
            break;
        }

        fwrite(pcm, 2, (size_t)nsamp, fo);
        nsamples += nsamp;
        data_bytes += (uint32_t)(nsamp * 2);
        total += (size_t)(nsamp / (MAX_FRAME_SIZE / 2));
    }

    /* 回填 WAV 头 */
    if (!err) {
        fseek(fo, 4, SEEK_SET);
        put32(fo, (uint32_t)(36 + data_bytes));
        fseek(fo, 40, SEEK_SET);
        put32(fo, data_bytes);
    }

    fprintf(stderr, "decoded %zu frames, %ld samples (%u bytes)\n",
            total, nsamples, data_bytes);

    g722_1_decode_release(st);
    fclose(fi);
    fclose(fo);
    return err;
}
