/*
 * br_https - 手机端 HTTPS 服务端（实现）
 */
#include "br_web.h"
#include "br_cert.h"
#include "br_json_min.h"
#include "br_phone.h"
#include "br_ble_core.h"
#include "br_proto.h"
#include "br_web_assets.h"

#include <string.h>
#include <stdlib.h>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>

#include <esp_http_server.h>
#include <esp_https_server.h>
#include <esp_http_server.h>
#include <esp_https_server.h>
#include <esp_log.h>
#include <esp_system.h>
#include <esp_timer.h>

static const char *TAG = "br_web";

#define BR_WEB_PORT     5888
/* HTTPS 端口：给陀螺仪用（传感器只在安全上下文里才有数据）。
   ★ 默认不开 ★
     浏览器只在**安全上下文**（证书真被信任的 https）里才给传感器数据，
     而让每个用户去装一张自签 CA 才能用局域网功能 —— 太麻烦，不值当。
     真要陀螺仪，正确做法是「在 APK 里加原生传感器桥」（见 DEV_PROGRESS）。
     留这段代码是为了以后需要时能一键打开。 */
#define BR_WEB_TLS_PORT 5889
#define BR_WEB_ENABLE_TLS 0
#define BR_WEB_MAX_URI  12
/*
 * ★ 语音 PCM 单包上限 ★
 *   板子 lwIP 的 TCP 窗口是 5760 字节（CONFIG_LWIP_TCP_WND_DEFAULT）。
 *   请求体超过窗口 → 传输停在半路 → httpd 被拖死且不会自恢复。
 *   4096 留了点余量（3200 字节的包 + 请求头还在窗口里）。
 */
#define BR_WEB_MAX_PCM_BODY 4096

static httpd_handle_t s_httpd;
static httpd_handle_t s_tlsd;
static bool s_ready;

/* ---------------------------------------------------------------- 事件队列 */

/*
 * 「板子 → 手机」的事件队列。
 *
 * 只有语音相关的事件会走这条路（voice_state / voice_partial / voice_final）。
 * 队列很小（8 条）—— 满了就丢最老的，实时性比完整性重要。
 */
#define EVQ_SIZE 8
typedef struct {
    char name[24];
    char data[256];
} ev_item_t;

static ev_item_t s_evq[EVQ_SIZE];
static int s_evq_head;
static int s_evq_count;
static SemaphoreHandle_t s_evq_lock;

static void evq_push(const char *name, const char *data)
{
    if (s_evq_lock) {
        xSemaphoreTake(s_evq_lock, portMAX_DELAY);
    }
    int idx = (s_evq_head + s_evq_count) % EVQ_SIZE;
    if (s_evq_count == EVQ_SIZE) {
        /* 满了 → 丢最老的一条 */
        s_evq_head = (s_evq_head + 1) % EVQ_SIZE;
        s_evq_count--;
        idx = (s_evq_head + s_evq_count) % EVQ_SIZE;
    }
    snprintf(s_evq[idx].name, sizeof(s_evq[idx].name), "%s", name ? name : "");
    snprintf(s_evq[idx].data, sizeof(s_evq[idx].data), "%s", data ? data : "{}");
    s_evq_count++;
    if (s_evq_lock) {
        xSemaphoreGive(s_evq_lock);
    }
}

static int evq_pop_all(char *out, size_t cap)
{
    int n = 0;
    size_t used = 0;

    if (cap < 2) {
        if (cap) {
            out[0] = '\0';
        }
        return 0;
    }
    if (s_evq_lock) {
        xSemaphoreTake(s_evq_lock, portMAX_DELAY);
    }
    /*
     * ★ 返回值必须**始终是 JSON 数组** ★
     *   手机 shim 的 _poll 按 `list.length` 遍历事件。以前这里只拼
     *   `{...},{...}`：单条事件是裸对象（length 为 undefined → 被丢弃），
     *   多条更是非法 JSON（r.json() 抛错 → 被判成断线）。
     *   结果 voice_state / voice_partial / voice_final 一条都到不了手机：
     *   语音永远「识别引擎还在加载」、识别出的文字也回不来。
     *   所以首尾必须补 [ ]，空队列自然就是 "[]"。
     */
    out[0] = '[';
    used = 1;
    for (int i = 0; i < s_evq_count; i++) {
        const ev_item_t *it = &s_evq[(s_evq_head + i) % EVQ_SIZE];
        /* 末尾留一个字节给 ']'（snprintf 自己还要放 NUL） */
        int written = snprintf(out + used, cap - used - 1,
                               "%s{\"ev\":\"%s\",\"data\":%s}",
                               n ? "," : "", it->name, it->data);
        if (written < 0 || (size_t)written >= cap - used - 1) {
            break;
        }
        used += (size_t)written;
        n++;
    }
    out[used] = ']';
    out[used + 1] = '\0';
    s_evq_head = (s_evq_head + s_evq_count) % EVQ_SIZE;
    s_evq_count = 0;
    if (s_evq_lock) {
        xSemaphoreGive(s_evq_lock);
    }
    return n;
}

/* ---------------------------------------------------------------- 静态资源 */

static const br_web_asset_t *find_asset(const char *path)
{
    for (size_t i = 0; i < br_web_assets_count; i++) {
        if (strcmp(br_web_assets[i].route, path) == 0) {
            return &br_web_assets[i];
        }
    }
    return NULL;
}

/**
 * 静态资源处理器。
 *
 * 带 ?v=20261028m 的查询串要剥掉 —— movo 的页面用版本号防缓存，
 * 路由匹配时只看路径部分。
 */
static void log_access(httpd_req_t *req);

static esp_err_t http_static(httpd_req_t *req)
{
    char path[96];
    log_access(req);
    const char *uri = req->uri;
    const char *q = strchr(uri, '?');
    size_t len = q ? (size_t)(q - uri) : strlen(uri);

    if (len >= sizeof(path)) {
        httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "path too long");
        return ESP_OK;
    }
    memcpy(path, uri, len);
    path[len] = '\0';

    const br_web_asset_t *asset = find_asset(path);
    if (!asset) {
        /* 根路径 → 触控板页 */
        if (strcmp(path, "/") == 0) {
            asset = find_asset("/touchpad");
        }
    }
    if (!asset) {
        httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "not found");
        return ESP_OK;
    }

    httpd_resp_set_type(req, "text/html");
    if (strstr(path, ".js") != NULL) {
        httpd_resp_set_type(req, "application/javascript");
    }
    /*
     * ★ Content-Encoding: gzip ★
     *   资源在构建期就压好了（tools/gen_web_assets.py），
     *   这里原样发出并声明 gzip —— 浏览器自己解压。
     *   板子上一个字节都不用解。
     */
    httpd_resp_set_hdr(req, "Content-Encoding", "gzip");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, (const char *)asset->data, asset->len);
}

/* ---------------------------------------------------------------- 访问日志 */

/**
 * 记一行 HTTP 访问。
 *
 * ★ 为什么要记 ★
 *   验收「手机真的能用」不能只靠 PC 侧发请求自证 ——
 *   板端留一行访问日志，就能从设备侧确认「是谁、什么时候、取的什么」。
 *   User-Agent 只留前面一段：手机浏览器一眼能认出来。
 */
static const char *method_name(httpd_method_t m)
{
    switch (m) {
    case HTTP_GET:     return "GET";
    case HTTP_POST:    return "POST";
    case HTTP_PUT:     return "PUT";
    case HTTP_DELETE:  return "DELETE";
    case HTTP_HEAD:    return "HEAD";
    case HTTP_OPTIONS: return "OPTIONS";
    case HTTP_PATCH:   return "PATCH";
    default:           return "?";
    }
}

static void log_access(httpd_req_t *req)
{
    char ua[52] = { 0 };
    if (httpd_req_get_hdr_value_str(req, "User-Agent", ua, sizeof(ua)) != ESP_OK) {
        ua[0] = '\0';
    }
    /*
     * ★ req->method 是枚举，不是字符串 ★
     *   以前直接用 %s 传枚举，printf 会把那个整数当指针解引用 ——
     *   HTTP_GET 就是整数 1，等于去读地址 1 → LoadProhibited，当场崩板重启。
     *   （堆栈回溯：vfprintf → vsnprintf → bridge_log_vprintf → log_access）
     */
    /* ★ 轮询请求每秒好几条，单独降级成 DEBUG ★
     *   其它请求留 INFO：验收时要看「谁在什么时候取了什么」。 */
    if (strstr(req->uri, "/io/poll") != NULL) {
        /* 轮询每秒好几条，全打出来又是刷屏 ——
           每 4 条留一条就够看出「手机还连着」。 */
        static int poll_seq;
        if ((poll_seq++ & 3) == 0) {
            ESP_LOGI(TAG, "HTTP %s %s", method_name(req->method), req->uri);
        }
    } else {
        ESP_LOGD(TAG, "HTTP %s %s  ← %s", method_name(req->method), req->uri, ua);
    }
}

/* ---------------------------------------------------------------- 事件 */

/**
 * POST /io —— 手机发来一个事件。
 *
 * 请求体有两种形态：
 *   - JSON  {"ev":"move","data":{"dx":1,"dy":2}}
 *   - 二进制（语音 PCM），事件名在 X-Event 头里
 */
static esp_err_t http_io(httpd_req_t *req)
{
    char body[1024];
    uint32_t t_io0 = (uint32_t)(esp_timer_get_time() / 1000);
    log_access(req);
    char evname[48];
    int total = 0;

    /*
     * ★ 语音 PCM 必须先于 1024 上限处理 ★
     *   手机每 100ms 发一包 16kHz 单声道 int16 = 3200 字节，
     *   若走下面 JSON 的 1024 缓冲会被 413 "too large" 整包丢掉 ——
     *   表现就是「麦克风按得下去、但电脑永远收不到声音」。
     *   这里按 content_len 单独收满再推进语音缓冲（堆上收，不吃会话栈）。
     *
     * ★ 包长上限 4096 ★
     *   板子 lwIP 的 TCP 窗口只有 5760 字节（CONFIG_LWIP_TCP_WND_DEFAULT）。
     *   手机的请求体一旦超过这个窗口，传输会停在半路：
     *   实测 9600 字节的包，板子只收到 4320 字节，剩下的再也发不过来，
     *   httpd 被这个请求一直拖住，而且**不会自己恢复**（网页全挂）。
     *   所以这里对超过窗口上限的包直接拒收，客户端必须发小包。
     */
    char vhdr[48] = {0};
    if (httpd_req_get_hdr_value_str(req, "X-Event", vhdr, sizeof(vhdr)) == ESP_OK &&
        (strcmp(vhdr, "voice_audio") == 0 || strcmp(vhdr, "voice_adpcm") == 0 ||
         strcmp(vhdr, "voice_adpcm_first") == 0)) {
        uint32_t t_enter = (uint32_t)(esp_timer_get_time() / 1000);
        size_t len = (size_t)req->content_len;
        if (len == 0 || len > BR_WEB_MAX_PCM_BODY) {
            ESP_LOGW(TAG, "PCM 包长 %u 超过上限 %u，拒收",
                     (unsigned)len, (unsigned)BR_WEB_MAX_PCM_BODY);
            httpd_resp_send_err(req, HTTPD_413_CONTENT_TOO_LARGE, "pcm too large");
            return ESP_OK;
        }
        uint8_t *pcm = malloc(len);
        if (!pcm) {
            httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no mem");
            return ESP_OK;
        }
        size_t got = 0;
        while (got < len) {
            int n = httpd_req_recv(req, (char *)pcm + got, len - got);
            if (n <= 0) {
                break;
            }
            got += (size_t)n;
        }
        if (got < len) {
            /*
             * ★ 没收满 = 请求体卡在半路 ★
             *   绝大多数情况是手机发的包超过了板子的 TCP 窗口（5760），
             *   剩下的字节再也发不过来。这种包必须丢掉 ——
             *   半截音频进缓冲，识别出来的是噪声。
             */
            ESP_LOGW(TAG, "PCM 包没收满：要 %u 字节只收到 %u 字节，丢弃",
                     (unsigned)len, (unsigned)got);
            free(pcm);
            httpd_resp_set_type(req, "application/json");
            httpd_resp_sendstr(req, "{\"ok\":false,\"msg\":\"short body\"}");
            return ESP_OK;
        }
        if (strcmp(vhdr, "voice_audio") == 0) {
            br_phone_push_pcm(pcm, got);
        } else {
            /* ADPCM：和遥控器同一套解码器 */
            br_phone_push_adpcm(pcm, got, strcmp(vhdr, "voice_adpcm_first") == 0);
        }
        free(pcm);
        {
            static int s_cnt;
            static uint32_t s_last_log;
            uint32_t dt = (uint32_t)(esp_timer_get_time() / 1000) - t_enter;
            s_cnt++;
            /* 每秒最多一条，别把日志本身变成压力源 */
            if (dt == 0 || (uint32_t)(esp_timer_get_time() / 1000) - s_last_log > 1000) {
                s_last_log = (uint32_t)(esp_timer_get_time() / 1000);
                ESP_LOGI(TAG, "收到手机PCM 第 %d 包 %u 字节 耗时 %u ms",
                         s_cnt, (unsigned)got, (unsigned)dt);
            }
        }
        httpd_resp_set_type(req, "application/json");
        httpd_resp_sendstr(req, "{\"ok\":true}");
        return ESP_OK;
    }

    if (req->content_len >= (int)sizeof(body)) {
        httpd_resp_send_err(req, HTTPD_413_CONTENT_TOO_LARGE, "too large");
        return ESP_OK;
    }
    while (total < req->content_len) {
        int n = httpd_req_recv(req, body + total, req->content_len - total);
        if (n <= 0) {
            break;
        }
        total += n;
    }
    body[total > 0 ? total : 0] = '\0';

    evname[0] = '\0';

    /* 二进制形态：事件名在头里 */
    char hdr[48] = {0};
    if (httpd_req_get_hdr_value_str(req, "X-Event", hdr, sizeof(hdr)) == ESP_OK &&
        hdr[0]) {
        snprintf(evname, sizeof(evname), "%s", hdr);
        /* 语音 PCM：直接推进语音缓冲 */
        if (strcmp(evname, "voice_audio") == 0) {
            br_phone_push_pcm((const uint8_t *)body, (size_t)total);
            httpd_resp_set_type(req, "application/json");
            httpd_resp_sendstr(req, "{\"ok\":true}");
            return ESP_OK;
        }
    } else {
        /* JSON 形态 */
        char ev[48] = {0};
        char data[768] = {0};
        if (!br_json_get_str(body, "ev", ev, sizeof(ev))) {
            httpd_resp_set_type(req, "application/json");
            httpd_resp_sendstr(req, "{\"ok\":false,\"msg\":\"no ev\"}");
            return ESP_OK;
        }
        snprintf(evname, sizeof(evname), "%s", ev);
        br_json_get_raw(body, "data", data, sizeof(data));
        if (data[0] == '\0') {
            snprintf(data, sizeof(data), "{}");
        }

        /* ★ 手机一动就让 BLE 扫描让路 ★
         *   单射频，扫描占空比 37%，不抢的话手机上卡得没法用。
         *   ★ 只有指针类事件才算 ★
         *     hb 心跳每 3 秒一次、voice_status 也有 —— 把它们算进来，
         *     扫描会被一直按住，遥控器掉线后就再也找不回来了。 */
        if (strcmp(evname, "move") == 0 || strcmp(evname, "click") == 0 ||
            strcmp(evname, "scroll") == 0 || strcmp(evname, "zoom") == 0 ||
            strcmp(evname, "drag_start") == 0 || strcmp(evname, "drag_end") == 0 ||
            strcmp(evname, "mid_down") == 0 || strcmp(evname, "mid_up") == 0) {
            br_ble_note_phone_activity();
        }
        br_phone_event(evname, data);

        httpd_resp_set_type(req, "application/json");
        httpd_resp_sendstr(req, "{\"ok\":true}");
        return ESP_OK;
    }

    ESP_LOGD(TAG, "收到事件 %s", evname);
    br_phone_event(evname, NULL);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, "{\"ok\":true}");
    {
        /* 慢请求告警：HTTP 是单任务，一个慢请求会把所有会话都堵住 */
        uint32_t dt = (uint32_t)(esp_timer_get_time() / 1000) - t_io0;
        if (dt > 1000) {
            ESP_LOGW(TAG, "慢请求 %s 用了 %u ms", req->uri, (unsigned)dt);
        }
    }
    return ESP_OK;
}

/**
 * GET /io/poll —— 手机来取「板子 → 手机」的事件。
 *
 * 短轮询（不是长轮询）：手机端 250 ms 问一次。
 * 短轮询对板子更友好 —— 不用挂着一个连接（挂着的连接占内存，
 * 而且手机切后台时 WebView 会冻结 JS，连接就永远收不到了）。
 */
static esp_err_t http_io_poll(httpd_req_t *req)
{
    static char out[2048];
    log_access(req);
    int n;

    n = evq_pop_all(out, sizeof(out));
    if (n == 0) {
        snprintf(out, sizeof(out), "[]");
    }
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, out);
}

/* ---------------------------------------------------------------- 启动 */

bool br_web_start(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    static httpd_uri_t uris[BR_WEB_MAX_URI];
    int n = 0;

    s_evq_lock = xSemaphoreCreateMutex();
    if (!s_evq_lock) {
        ESP_LOGE(TAG, "事件队列锁创建失败");
        return false;
    }

    /*
     * 一次最多几个并发连接：手机一个轮询 + 一次 POST，够了。
     *
     * ★ 上限受 LWIP_MAX_SOCKETS 约束 ★
     *   HTTP 服务器自己要占 3 个 socket（listen + 2），
     *   max_open_sockets 最大只能是 LWIP_MAX_SOCKETS - 3。
     *   设大了 httpd_start 直接失败（日志里会明说）。
     */
    /* ★ 端口必须显式设 ★
     *   httpd_config_t 的默认端口是 80 —— 不设的话手机按 5888 找就找不到。 */
    cfg.server_port = BR_WEB_PORT;
    /*
     * ★ 并发连接数不能只给 3 ★
     *   浏览器的 HTTP/1.1 每个源最多 6 条连接，它同时握着
     *   一条轮询连接 + 若干 POST 连接 —— 板子只给 3 个 socket 的话，
     *   轮询会被 POST 挤掉（实测 4.6 秒只轮询了 1 次），
     *   页面就表现成「卡住不动」。
     */
    cfg.max_open_sockets = 8;
    cfg.lru_purge_enable = true;
    /*
     * ★ 必须显式指定通配符匹配函数 ★
     *   httpd 默认用「完全相等」比较 URI，通配路由（斜杠+星号）
     *   注册了也不会匹配任何请求 —— 表现是所有静态资源都 404，
     *   只有精确路径能通。
     */
    cfg.uri_match_fn = httpd_uri_match_wildcard;
    /*
     * ★ 会话任务栈不能省 ★
     *   4096 会崩：处理函数里只要有一条 ESP_LOGI，
     *   picolibc 的 vsnprintf 就要吃掉好几 KB 栈
     *   → LoadProhibited（Guru Meditation），板子直接重启。
     *   IDF 默认的 4096 是给「handler 里别打日志」的场景准备的。
     *   8192 是实测能扛住日志的尺寸。
     */
    cfg.stack_size = 8192;
    /*
     * ★ HTTP 会话任务的优先级必须提上去 ★
     *   esp_http_server 默认 task_priority=5，而 WiFi 任务是 23、
     *   tcpip 是 18 —— 手机端一旦有流量（语音 PCM、触控板事件），
     *   HTTP 处理任务就是**优先级最低的那个**，一忙就被饿着：
     *   实测手机语音包每包要等 5 秒（正好是 recv_wait_timeout），
     *   语音会话刚建立就被看门狗掐掉。
     *   12 高于默认的 5，又低于 WiFi/tcpip，不会反过来把网络栈饿死。
     */
    cfg.task_priority = 12;
    /*
     * ★ 收数据不能等太久 ★
     *   HTTP 是单任务：一个会话在等 recv，所有会话都排队。
     *   手机的请求体要是卡在半路（超过 TCP 窗口就会这样），
     *   这里最多等 2 秒就放弃 —— 宁可丢一个包，也不能把整台板子拖死。
     */
    cfg.recv_wait_timeout = 2;
    cfg.send_wait_timeout = 5;

    esp_err_t err = httpd_start(&s_httpd, &cfg);
    if (err != ESP_OK) {
        /* 消息要短：USB CDC 那条线上有多路日志，长行会被截断看不全 */
        ESP_LOGE(TAG, "E=%d", (int)err);
        s_httpd = NULL;
        return false;
    }

    memset(uris, 0, sizeof(uris));

    uris[n].uri = "/io";
    uris[n].method = HTTP_POST;
    uris[n].handler = http_io;
    n++;

    uris[n].uri = "/io/poll";
    uris[n].method = HTTP_GET;
    uris[n].handler = http_io_poll;
    n++;

    uris[n].uri = "/*";
    uris[n].method = HTTP_GET;
    uris[n].handler = http_static;
    n++;

    httpd_register_uri_handler(s_httpd, &uris[0]);
    httpd_register_uri_handler(s_httpd, &uris[1]);
    httpd_register_uri_handler(s_httpd, &uris[2]);

    /*
     * ★ 再起一个 HTTPS 的（端口 5889）★
     *
     *   为什么要有：浏览器的**传感器只在安全上下文里才给数据**。
     *   实测（Redmi K60 / Android 15，页面探针）：
     *       {"secure":false,"motions":0,"hasHandler":true}
     *   —— 明文 HTTP 下 devicemotion 事件根本不会派发，
     *   所以陀螺仪飞鼠在 http 页面里永远是死的。
     *   而 https 要证书**真被信任**才算安全上下文
     *   （点「继续前往」带着证书错误不算），
     *   所以手机里要装一次我们的 CA（tools/esp32s3-remote-ca.crt）。
     *
     *   两个端口并存：5888 明文（零设置，触控板/键盘够用），
     *   5889 HTTPS（装了 CA 之后陀螺仪可用）。
     */
#if BR_WEB_ENABLE_TLS
    {
        httpd_ssl_config_t ssl = HTTPD_SSL_CONFIG_DEFAULT();
        httpd_config_t scfg = cfg;

        scfg.server_port = 0;             /* HTTPS 用 port_secure */
        scfg.max_open_sockets = 3;       /* 两个服务器分 socket */
        /*
         * ★ 控制端口必须跟明文那个错开 ★
         *   httpd 的 ctrl socket 是绑在一个 UDP 端口上的
         *   （默认 32768）—— 两个服务器用同一个就撞端口，
         *   第二个直接起不来（error in creating ctrl socket）。
         */
        scfg.ctrl_port = 32769;
        ssl.httpd = scfg;
        ssl.servercert = s_cert_pem;
        ssl.servercert_len = s_cert_pem_len;
        ssl.prvtkey_pem = s_key_pem;
        ssl.prvtkey_len = s_key_pem_len;
        ssl.port_secure = BR_WEB_TLS_PORT;
        ssl.transport_mode = HTTPD_SSL_TRANSPORT_SECURE;

        esp_err_t serr = httpd_ssl_start(&s_tlsd, &ssl);
        if (serr == ESP_OK) {
            httpd_register_uri_handler(s_tlsd, &uris[0]);
            httpd_register_uri_handler(s_tlsd, &uris[1]);
            httpd_register_uri_handler(s_tlsd, &uris[2]);
            ESP_LOGI(TAG, "HTTPS 服务端已启动，端口 %d（陀螺仪要用它）", BR_WEB_TLS_PORT);
        } else {
            /* 起不来也不影响明文那套 —— 触控板照样能用 */
            ESP_LOGW(TAG, "HTTPS 起不来 E=%d（陀螺仪不可用，明文照常）", (int)serr);
        }
    }
#endif /* BR_WEB_ENABLE_TLS */

    s_ready = true;
    ESP_LOGI(TAG, "Web 服务端已启动（明文 HTTP），端口 %d", BR_WEB_PORT);
    return true;
}

bool br_web_ready(void)
{
    return s_ready;
}

static br_web_event_fn s_forward;

void br_web_set_event_forward(br_web_event_fn fn)
{
    s_forward = fn;
}

void br_web_send_event(const char *name, const char *data)
{
    /* 有人接管（BLE 模式）就直接发，不再进 HTTP 轮询队列 */
    if (s_forward) {
        s_forward(name ? name : "", data ? data : "{}");
        return;
    }
    evq_push(name, data);
}
