# 手机自定义快捷键页（v1.4.0）设计

日期：2026-11-23
状态：已与用户确认（入口=打开页面；图标=文字自动生成；上限 12；3 列）

## 目标

把手机端「底部一个快捷键」升级为**独立快捷键页面**：最多 12 个自定义按键，
由 PC 端「按键」页添加 / 删除 / 改名 / 配动作。动作沿用现有两类：

- **组合键**：板子直接发 HID，PC 客户端不在也能用；
- **打开软件**：由 PC 客户端执行（需要客户端在线）。

## 数据与键位

- 配置落在手机条目（`kind="phone"`）的 `custom_keys`：
  `[{ "id": 1, "name": "截图", "action": {kind, keys, uri} }, …]`，`id` 固定 1..12。
- 键位空间：`key_id = "6:<0x100+id>"`（257…268），与现有映射表 / `EV KEY` 完全复用。
- 组合键 → PC 推 `MAPSET` 到板子；打开软件 → 板子映射未命中，上报
  `EV KEY src=6 usage=0x10N`，PC 端按 `bindings` 执行。
- 旧的固定「快捷键」行（`6:6`）移除；板子里残留的 `6:6` 映射不再使用，不做迁移。

## 固件

- NVS 命名空间 `ckeys`，blob：`magic + version + count + entries[{id, name[24]}]`。
- CDC 命令：`CKBEGIN n` → `CKADD <id> <name>` × n → `CKCOMMIT`（仿 MAP 协议，commit 落 NVS 并回报手机）。
- 手机订阅事件通知时逐条推 `EV CK id=… name=…`，最后 `EV CKDONE n=…`。
- `quickbar_usage()` 接受 `ck<id>` → `usage = 0x100 + id`（1..12，越界拒绝）。
- 未命中映射时沿用 `quickbar_default`（不动作），按下/抬起照常上报 `EV KEY`。

## 手机页面 `shortcut.html`

- 3 列网格，最多 12 格；名字前 1~2 字生成文字图标（圆角底、按 id 取色）。
- 数据来自 `CK` 事件累积 + `CKDONE` 收尾；空列表显示「到电脑端『按键』页添加」。
- 点按 → `socket.emit('tap_key', {key:'ck<id>'})` + 震动。
- 返回键 → `touchpad.html`。
- 底部「快捷」键改为导航到本页；旧的「长按换图标」面板删除。

## PC 端「按键」页（手机布局）

- 固定行里删掉「快捷键」；新增「自定义快捷键」区块：
  每行 = 名字 + 动作编辑器（组合键 / 打开软件）+ 删除；底部「＋ 添加」。
- 保存动作走现有编辑器（`MappingCard` 的编辑弹窗逻辑复用）；
  `setMapping("6:257"... )` 之类的现有路径推 MAPSET。
- 改名 / 增删时重推 `CKBEGIN/CKADD/CKCOMMIT`（板子与手机同步）。
- 配置持久化 + `mapping_archive` 照旧。

## 契约闸门（§37）

- 上限 12 与 `usage = 0x100+id` 在固件、PC、手机三处一致；
- 固件存在 `CKBEGIN/CKADD/CKCOMMIT`、NVS 键 `ckeys`、`EV CK`/`EV CKDONE`；
- `shortcut.html` 存在、3 列、处理 `ck`/`ckdone`、无图片裁剪、有返回；
- 底部 `#shortcut-btn` 导航到 `shortcut.html`；
- PC 存在 `custom_keys` 读写与 `CKBEGIN` 推送；
- i18n 有页面标题 / 空态 / 保存提示。

## 发版

- 版本 **1.4.0**（功能新增）；固件 + firmware-wifi + APK + 客户端 + `check_release.py`。
- 真机验收：PC 添加 3 个键（组合键 / 打开软件 / 中文名）→ 手机出现 3 格；
  点组合键板子发 HID（关客户端仍有效）；点打开软件启动程序；改名/删除后手机同步；
  断线重连后列表仍在。
