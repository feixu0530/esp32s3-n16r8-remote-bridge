/*
 * br_web_assets - 内置网页资源（由 tools/gen_web_assets.py 生成，勿手改）
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    const char *route;   /* 请求路径 */
    const uint8_t *data;   /* gzip 后的字节 */
    size_t len;            /* 压缩后长度 */
    size_t raw_len;        /* 原始长度（诊断用） */
} br_web_asset_t;

extern const br_web_asset_t br_web_assets[];
extern const size_t br_web_assets_count;
