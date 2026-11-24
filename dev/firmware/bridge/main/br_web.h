/*
 * br_web - 手机端的 Web 服务端（明文 HTTP）
 * ---------------------------------------------------------------------------
 * 手机（movo 的 APK）通过 WiFi 连开发板，页面与事件都走这里。
 *
 * ★ 为什么是明文 HTTP 而不是 HTTPS ★
 *   最初照 movo 的原样做 HTTPS（自签证书 + APK 的「信任这台电脑？」弹窗）。
 *   实机（Redmi K60 / Android 15）上发现这条链路很别扭：
 *     - Chromium 强制校验主机名，证书没有 SAN 就直接拒，proceed() 都救不回来；
 *     - 证书补了 SAN 之后，应用自带的 proceed() 在这台机器上也不生效；
 *     - 最后得靠「把 CA 装进手机用户证书库」才通，而那一步要用户按指纹验证。
 *   而这一切的起因只是 movo 的 APK 禁止明文（network_security_config.xml）。
 *   既然 APK 是我们自己重打的（只改图标/名字/明文开关），
 *   局域网内就**直接用明文**：
 *     - 手机端零设置，不用信任任何证书；
 *     - 浏览器直接打开也不会有「不安全」提示；
 *     - 板子省掉整条 TLS 栈 —— 512KB 内部 DRAM 上这是实打实的一笔。
 *
 * ★ 想换回 HTTPS 的话 ★
 *   1) 板子：把这里改回 esp_https_server（tools/gen_cert.py 能签证书，
 *      记得带 --ip，Chromium 只认 SAN）；
 *   2) APK：network_security_config.xml 里 cleartextTrafficPermitted 改回 false，
 *      或者把 tools/esp32s3-remote-ca.crt 装进手机的用户证书库。
 *
 * 路由（与 movo 的服务端保持一致，页面一行都不用改）：
 *   GET  /                  触控板页
 *   GET  /k                 键盘页
 *   GET  /static/js 下的脚本
 *   POST /io                手机 → 板子的事件
 *   GET  /io/poll           板子 → 手机的事件（轮询）
 *
 * 事件协议见 web/static/js/socket.io.min.js（socket.io 的兼容替代品）。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 启动 Web 服务端 */
bool br_web_start(void);

/** 服务端是否已就绪 */
bool br_web_ready(void);

/**
 * 往「板子 → 手机」的事件队列里塞一条。
 *
 * @param name 事件名（voice_state / voice_partial / voice_final …）
 * @param data JSON 文本（形如 {"ready":true}），可为 NULL
 */
/**
 * ★ 事件转发钩子 ★
 *
 *   手机走 BLE 之后，「板子→手机」的事件不再走 HTTP 轮询队列，
 *   而是由 GATT 层直接 notify 出去。
 *   设了钩子之后 br_web_send_event 会**直接回调且不入队**
 *   （不入队是为了不让队列一直填满、静默丢掉真正的事件）。
 *   传 NULL 取消钩子（回到 HTTP 轮询那条路）。
 */
typedef void (*br_web_event_fn)(const char *name, const char *data);
void br_web_set_event_forward(br_web_event_fn fn);

void br_web_send_event(const char *name, const char *data);

#ifdef __cplusplus
}
#endif
