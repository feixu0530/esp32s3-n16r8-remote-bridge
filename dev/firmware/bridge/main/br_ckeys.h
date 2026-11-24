/*
 * br_ckeys - 手机「自定义快捷键页」的按键清单（板子侧存储与上报）
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

esp_err_t br_ckeys_init(void);

/* PC 侧下发：CKBEGIN n → CKADD id name × n → CKCOMMIT */
bool br_ckeys_begin(int count);
bool br_ckeys_add(int id, int col, int row, const char *name);
bool br_ckeys_commit(void);

int br_ckeys_count(void);

/* 逐条推 EV ck / ckdone 给手机（订阅事件、连上、提交后都会调） */
void br_ckeys_report(void);
