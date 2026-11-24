"""
重打包 Movo APK：改图标 + 应用名 + 放开明文 HTTP。

★ 为什么连「明文开关」一起改 ★
    movo 的 network_security_config.xml 写着 cleartextTrafficPermitted="false"，
    manifest 里还有 usesCleartextTraffic="false" —— 系统直接拦掉明文 HTTP。
    板子已经改成了明文 HTTP（局域网内没必要较劲证书），
    所以 APK 这一侧必须放开，否则连不上。

★ 改哪些东西 ★
    1. res/xml/network_security_config.xml —— cleartextTrafficPermitted → true
       （二进制 AXML，就地改 4 个字节，文件大小不变）
    2. assets/setup.html —— 地址规范化不再强制 https
    3. res/mipmap-*/ic_launcher_fg.png / _bg.png —— 换成我们自己的图标
    4. resources.arsc —— 应用名（Movo → 手机遥控器桥）

用法：
    python dev/client/build/patch_movo_apk.py --apk <原APK> --out <新APK>
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent
ICON_SVG = CLIENT / "src" / "assets" / "icons" / "app.svg"

# 自适应图标：前景画在中间（安全区），背景纯色
ADAPTIVE_SIZES = {
    "mdpi": 108,
    "hdpi": 162,
    "xhdpi": 216,
    "xxhdpi": 324,
    "xxxhdpi": 432,
}
BG_COLOR = "#141A24"          # 与应用深色主题接近
# ★ 名字必须排得进字符串池里原来的位置 ★
#   「Movo」在池里的下标 1，它前面是 "%1$s ..."、后面是「Movo 运行中」，
#   所以首字符得落在 '%' 与 'M' 之间 —— 拿中文开头就排不进去（会破坏池的排序）。
APP_NAME = "ESP32 手机遥控器"


# --------------------------------------------------------------- 二进制 AXML
def _pool_strings(data, pool_off):
    """读 AXML 字符串池，返回 (字符串列表, 偏移数组)"""
    scount, _stylecount, flags, sstart, _stystart = struct.unpack_from("<IIIII", data, pool_off + 8)
    offs = struct.unpack_from("<%dI" % scount, data, pool_off + 28)
    base = pool_off + sstart
    out = []
    for o in offs:
        p = base + o
        n1 = data[p]; p += 1
        if n1 & 0x80:
            n1 = ((n1 & 0x7F) << 8) | data[p]; p += 1
        n2 = data[p]; p += 1
        if n2 & 0x80:
            n2 = ((n2 & 0x7F) << 8) | data[p]; p += 1
        out.append(data[p:p + n2].decode("utf-8", "replace"))
    return out, offs


def patch_nsc_cleartext(data):
    """把 network_security_config.xml 的 cleartextTrafficPermitted 改成 true。

    返回 (新数据, 是否改到)。
    """
    buf = bytearray(data)
    _t, _h, pool_size = struct.unpack_from("<HHI", buf, 8)
    p = 8 + pool_size
    while p < len(buf):
        t, _h, c = struct.unpack_from("<HHI", buf, p)
        if t != 0x0102:                       # 不是 START_TAG
            p += c
            continue
        _ns, _name = struct.unpack_from("<II", buf, p + 16)
        astart, asize, acount = struct.unpack_from("<HHH", buf, p + 24)
        strs, _offs = _pool_strings(buf, 8)
        for i in range(acount):
            q = p + 16 + astart + i * asize
            _ans, aname, _araw = struct.unpack_from("<III", buf, q)
            _ts, _res0, dtype, ddata = struct.unpack_from("<HBBi", buf, q + 12)
            if dtype != 0x12:                # 不是布尔属性
                continue
            attr = strs[aname] if aname != 0xFFFFFFFF else ""
            if attr == "cleartextTrafficPermitted" and (ddata & 0xFFFFFFFF) == 0:
                struct.pack_into("<i", buf, q + 16, -1)   # 0xFFFFFFFF = true
                return bytes(buf), True
        p += c
    return bytes(buf), False


def patch_resources_arsc_name(data, new_name):
    """把 resources.arsc 全局池里的 "Movo"（应用名）换成新名字。

    ★ Android 的字符串池：下标顺序 == 排序顺序 ★
      ResStringPool 用下标做二分查找，池里的字符串必须按下标递增排序。
      所以：
        - 不能把新字符串追加到池尾却挂到中间的下标上（那会破坏排序）；
        - 要在原下标处**原地替换**，且新名字必须排得进原来的位置。
      违反哪一条，AssetManager2 载入资源表时就失败 ——
      安装报 INSTALL_PARSE_FAILED_NOT_APK（"Failed to load asset path"）。

    做法：
      1) 读出每条字符串的**完整编码**（含长度前缀与结尾 NUL），原样保留
         （池里有超过 127 字节的字符串，长度前缀要用两字节形式，
           自己重新编码就要处理那套规则，保留原字节最保险）
      2) 把目标那条换成新名字的编码
      3) 校验新名字仍排得进原来的位置（前后邻居夹住它）
      4) 按原下标顺序重建数据区
    """
    buf = bytearray(data)
    scount, stylecount, _flags, _sstart, _stystart = struct.unpack_from("<IIIII", buf, 12 + 8)
    if stylecount:
        raise SystemExit("全局池里有 style，本脚本没处理")
    offs = list(struct.unpack_from("<%dI" % scount, buf, 12 + 28))
    base = 12 + _sstart

    entries = []          # [排序键, 原索引, 整条编码, 内容]
    for i, o in enumerate(offs):
        p = base + o
        entry_start = p
        n1 = buf[p]; p += 1
        if n1 & 0x80:
            n1 = ((n1 & 0x7F) << 8) | buf[p]; p += 1
        n2 = buf[p]; p += 1
        if n2 & 0x80:
            n2 = ((n2 & 0x7F) << 8) | buf[p]; p += 1
        raw = bytes(buf[p:p + n2])
        entry_end = p + n2
        while entry_end < len(buf) and buf[entry_end] != 0:
            entry_end += 1
        entry_end += 1                       # 含结尾 NUL
        entries.append([raw.decode("utf-8", "replace").encode("utf-16-be"),
                        i, bytes(buf[entry_start:entry_end]), raw])

    target = None
    for it in entries:
        if it[3].decode("utf-8", "replace") == "Movo":
            target = it
            break
    if target is None:
        return bytes(buf), False

    nb = new_name.encode("utf-8")
    if len(new_name) > 127 or len(nb) > 127:
        raise SystemExit("名字太长，二进制池补丁不支持")
    new_key = new_name.encode("utf-16-be")
    target[2] = bytes([len(new_name), len(nb)]) + nb + b"\x00"
    target[0] = new_key
    target[3] = nb

    # ★ 位置校验：新的名字必须还排得进原来的下标 ★
    idx = target[1]
    if idx > 0 and entries[idx - 1][0] >= new_key:
        raise SystemExit(
            "新名字「%s」排不进原位置（比前一条 %r 小）——"
            "换个首字符更靠前的名字" % (new_name, entries[idx - 1][3]))
    if idx + 1 < len(entries) and new_key >= entries[idx + 1][0]:
        raise SystemExit(
            "新名字「%s」排不进原位置（比后一条 %r 大）——"
            "换个首字符更靠后的名字" % (new_name, entries[idx + 1][3]))

    # ★ 按原下标顺序重建数据区（不改顺序，顺序本身就是排序）★
    data_area = bytearray()
    new_offs = [0] * scount
    for _key, i, entry, _raw in entries:
        new_offs[i] = len(data_area)
        data_area += entry

    # 字符串数没变 ⇒ 偏移数组长度不变 ⇒ stringsStart 不变
    header = bytearray(28)
    struct.pack_into("<I", header, 8, scount)          # stringCount
    struct.pack_into("<I", header, 12, 0)              # styleCount
    struct.pack_into("<I", header, 16, _flags)         # flags（UTF-8 标志）
    struct.pack_into("<I", header, 20, 28 + 4 * scount)  # stringsStart
    struct.pack_into("<I", header, 24, 0)              # stylesStart
    body = struct.pack("<%dI" % scount, *new_offs) + bytes(data_area)
    # ★ 补齐到 4 的倍数 ★
    #   名字变长会让池变长，后面紧跟的 package chunk 就被挤歪了 ——
    #   package chunk 里的字符串池要求 4 字节对齐，一歪整个资源表就解析失败
    #   （安装报 INSTALL_PARSE_FAILED_NOT_APK）。
    pad = (-len(body)) % 4
    body += b"\x00" * pad
    struct.pack_into("<HHI", header, 0, 0x0001, 28, 28 + len(body))
    pool_body = bytes(header) + body

    tail = bytes(buf[12 + struct.unpack_from("<I", buf, 16)[0]:])
    out = bytearray(bytes(buf[:12]) + pool_body + tail)
    struct.pack_into("<I", out, 4, len(out))           # arsc 顶层大小
    return bytes(out), True


# --------------------------------------------------------------- setup.html
def patch_setup_html(text):
    """改 APK 自带的设置页（assets/setup.html）。

    ★ 两件事 ★
    1. 地址规范化**保留用户填的协议**：
       原来写死 https —— 但板子的明文服务在 5888、HTTPS 在 5889，
       写死协议就没法选了（陀螺仪必须在 HTTPS 下才拿得到传感器数据）。
    2. 把页面上的 Movo 字样换成我们的名字。
       （只换独立的 "Movo"，MovoNative / MovoLang 是接口名，不能动。）
    """
    old = "    return 'https://' + host + ':' + port + '/touchpad';"
    new = """    return scheme + '://' + host + ':' + port + '/touchpad';"""
    if old not in text:
        raise SystemExit("setup.html 里没找到要改的那行，movo 版本可能变了")
    text = text.replace(old, new)

    # 记住用户填的协议（原来这行把它整个丢掉了）
    old_strip = "    s = s.replace(/^[a-z]+:\\/\\//i, '');"
    new_strip = """    var scheme = 'http';
    var m0 = s.match(/^(https?):\\/\\//i);
    if (m0) { scheme = m0[1].toLowerCase(); s = s.slice(m0[0].length); }"""
    if old_strip not in text:
        raise SystemExit("setup.html 里没找到去协议头那行，movo 版本可能变了")
    text = text.replace(old_strip, new_strip)

    text = text.replace('placeholder="192.168.31.12:5888"', 'placeholder="192.168.2.203:5888"')
    # 只换独立的 Movo（不碰 MovoNative / MovoLang）
    text = re.sub(r"Movo(?![A-Za-z])", "ESP32 遥控", text)

    # 设置页中间那个大图标：换成我们的手机轮廓（跟 PC 客户端同一个图标）
    old_logo = """      <svg viewBox="0 0 24 24">
        <path d="M9.5 3.6 18.9 12l-4.6 1.2 2.6 4.9-2.3 1.2-2.6-4.9-2.5 3.4z"/>
        <path d="M4.2 9.2c-.9-2.3-.4-4.6 1.2-6"/>
        <path d="M2.6 12.2C1.3 8.9 1.9 5.5 4.3 3.1" opacity=".45"/>
      </svg>"""
    new_logo = """      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <rect x="7" y="2.5" width="10" height="19" rx="3"/>
        <circle cx="12" cy="7" r="1.7"/>
        <path d="M9.6 12.4h4.8M9.6 15.6h4.8"/>
      </svg>"""
    if old_logo in text:
        text = text.replace(old_logo, new_logo)
    else:
        raise SystemExit("setup.html 里没找到 logo 的 SVG，movo 版本可能变了")

    # 输入框里的默认值也换掉（不是 placeholder，是 value）
    text = text.replace('value="192.168.31.12:5888"', 'value="192.168.2.203:5888"')
    # 标签也别说「电脑」—— 服务跑在开发板上
    text = text.replace('data-i18n="set.addr">电脑服务地址<',
                        'data-i18n="set.addr">开发板地址<')
    return text


def patch_assets_i18n(text):
    """APK 自带的 i18n.js —— 设置页页脚的「Movo · v2.0.0」在里头。

    ★ 另外把「电脑」全改成「开发板」 ★
      服务跑在开发板上（手机 → WiFi → 板子 → USB HID → PC），
      说「电脑服务地址」会让人填成 PC 的 IP —— 用户就是这么填错、
      手机端一直报「连不上电脑」的。
    """
    text = text.replace("'set.foot': 'Movo'", "'set.foot': 'ESP32 遥控'")
    text = text.replace("'set.foot': \"Movo\"", "'set.foot': 'ESP32 遥控'")

    pairs = [
        ("'set.addr': '电脑服务地址'", "'set.addr': '开发板地址'"),
        ("'set.addr.ph': '192.168.31.12:5888'", "'set.addr.ph': '192.168.2.203:5888'"),
        ("'set.toast.badaddr': '地址格式不对，例如 192.168.31.12:5888'",
         "'set.toast.badaddr': '地址格式不对，例如 192.168.2.203:5888'"),
        ("'set.toast.timeout': '连接超时：检查电脑服务是否在运行'",
         "'set.toast.timeout': '连接超时：检查地址是否正确、开发板是否还连着 WiFi'"),
        ("'set.toast.connectfail': '连不上电脑：检查服务是否在运行、IP 是否正确'",
         "'set.toast.connectfail': '连不上开发板：检查地址是否正确、手机和开发板是否连同一个 WiFi'"),
        ("'set.addr': 'PC address'", "'set.addr': 'Board address'"),
        ("'set.toast.badaddr': 'Invalid address, e.g. 192.168.31.12:5888'",
         "'set.toast.badaddr': 'Invalid address, e.g. 192.168.2.203:5888'"),
        ("'set.toast.timeout': 'Connection timed out: is the PC service running?'",
         "'set.toast.timeout': 'Connection timed out: check the address and Wi-Fi'"),
        ("'set.toast.connectfail': 'Can\\'t reach your PC: check the service and the IP'",
         "'set.toast.connectfail': 'Can\\'t reach the board: check the address and Wi-Fi'"),
    ]
    for old, new in pairs:
        text = text.replace(old, new)
    return text


# --------------------------------------------------------------- 图标
def _png_bytes(image):
    from PySide6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


def make_icon_pngs(outdir):
    """生成自适应图标的前景/背景 PNG。"""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    outdir.mkdir(parents=True, exist_ok=True)
    renderer = QSvgRenderer(str(ICON_SVG))
    if not renderer.isValid():
        raise SystemExit(f"图标 SVG 读不出来：{ICON_SVG}")

    out = {}
    for density, size in ADAPTIVE_SIZES.items():
        fg = QImage(size, size, QImage.Format_ARGB32)
        fg.fill(0)
        p = QPainter(fg)
        inner = int(size * 0.62)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p, QRectF((size - inner) / 2.0, (size - inner) / 2.0, inner, inner))
        p.end()
        out["fg_" + density] = _png_bytes(fg)

        bg = QImage(size, size, QImage.Format_ARGB32)
        bg.fill(QColor(BG_COLOR))
        out["bg_" + density] = _png_bytes(bg)
    return out


# --------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default=APP_NAME)
    ap.add_argument("--skip", default="", help="要跳过的补丁，逗号分隔：arsc,nsc,setup,icons")
    args = ap.parse_args()

    src = Path(args.apk)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}

    icons = make_icon_pngs(out.parent / "_icons")

    zin = zipfile.ZipFile(src, "r")
    changed = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            name = item.filename

            # ★ 只扔掉 v1 签名的四件套 ★
            #   META-INF/ 下面还有 androidx 的版本标记和 META-INF/services/
            #   （协程的 SPI）—— 那些运行时要用，删了会崩。
            base = name.rsplit("/", 1)[-1].upper()
            if name.startswith("META-INF/") and (
                    base == "MANIFEST.MF" or base.endswith((".SF", ".RSA", ".DSA", ".EC"))):
                continue

            if name == "res/xml/network_security_config.xml" and "nsc" not in skip:
                data, ok = patch_nsc_cleartext(data)
                if ok:
                    changed += 1
                    print("  ✓ 放开明文 HTTP（network_security_config.xml）")
            elif name == "assets/setup.html" and "setup" not in skip:
                data = patch_setup_html(data.decode("utf-8")).encode("utf-8")
                print("  ✓ setup.html：保留协议 + 换图标 + 换名字")
            elif name == "assets/i18n.js" and "setup" not in skip:
                data = patch_assets_i18n(data.decode("utf-8")).encode("utf-8")
                print("  ✓ i18n.js：页脚换名")
            elif name == "resources.arsc" and "arsc" not in skip:
                data, ok = patch_resources_arsc_name(data, args.name)
                if ok:
                    changed += 1
                    print("  ✓ 应用名 → " + args.name)
            elif "icons" not in skip and name.startswith("res/mipmap-") and name.endswith("ic_launcher_fg.png"):
                data = icons["fg_" + name.split("-")[1]]
                print("  ✓ 图标前景 " + name.split("-")[1])
            elif "icons" not in skip and name.startswith("res/mipmap-") and name.endswith("ic_launcher_bg.png"):
                data = icons["bg_" + name.split("-")[1]]
                print("  ✓ 图标背景 " + name.split("-")[1])

            zi = zipfile.ZipInfo(name, date_time=item.date_time)
            zi.compress_type = item.compress_type
            zi.external_attr = item.external_attr
            zout.writestr(zi, data)
    zin.close()
    print("已改 %d 处 → %s（%d 字节）" % (changed, out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
