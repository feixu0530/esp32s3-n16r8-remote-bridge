#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "br_types.h"

void br_mapping_init(void);
bool br_mapping_begin(const char *addr);
bool br_mapping_set(uint32_t key_id, uint8_t trigger, uint8_t output,
                    uint8_t mods, const uint8_t *keys, uint8_t nkeys);
bool br_mapping_commit(void);
bool br_mapping_dump(const char *addr);
/** 这台遥控器在板子上配了几条映射 */
int br_mapping_count(const uint8_t addr[6]);
/** 删掉这台遥控器的全部映射（解绑时用） */
bool br_mapping_clear(const uint8_t addr[6]);
/**
 * 给「板子上一条映射都没有」的遥控器铺一套出厂默认。
 *
 * 按遥控器名匹配默认档案（k_default_profiles）——
 * 名字对不上任何档案就什么都不铺，不给陌生遥控器乱配键位。
 * 用户配过（count>0）时也不动。
 *
 * 返回：>0 铺了几条；0 没铺（没有档案 / 已经配过）；-1 暂时忙
 *       （PC 正在写映射），调用方稍后重试。
 */
int br_mapping_apply_defaults(const char *addr, const char *name,
                              const char *profile_in, char *out_profile);

/**
 * 按遥控器名字找出它该用的默认档案 id（找不到返回 NULL）。
 *
 * ★ 档案 id 会记在板子的配对表里 ★
 *   同一台遥控器重新配对时，用回原来那套默认 —— 不因为换了地址就换一套。
 */
const char *br_mapping_profile_id_for_name(const char *name);
void br_mapping_key(const uint8_t addr[6], uint32_t key_id, bool down);

