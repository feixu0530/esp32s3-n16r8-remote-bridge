/*
 * br_cert - 自签证书（由 tools/gen_cert.py 生成，勿手改）
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

extern const uint8_t s_cert_pem[];
extern const size_t s_cert_pem_len;
extern const uint8_t s_key_pem[];
extern const size_t s_key_pem_len;
