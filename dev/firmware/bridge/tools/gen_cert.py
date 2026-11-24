"""生成板子 HTTPS 服务端用的自签证书。

为什么需要自签证书：
    movo 的 APK 禁止明文 HTTP（network_security_config.xml）。
    板子没有 CA 签发的证书可用，只能自签 ——
    APK 自带的 onReceivedSslError 会问用户「信任这台电脑？」，点一次即记住。

CN 写什么都行：
    APK 那边是 handler.proceed()，不校验主机名。

用法（dev/firmware/bridge 下）：
    python tools/gen_cert.py [--ip 192.168.2.203] [--dns esp32s3-remote]

★ 为什么必须有 SAN ★
    Chromium（Android WebView / Chrome）**强制校验主机名**，
    只看 CN、没有 SAN 的证书一律判「名字不匹配」——
    这时应用就算调了 SslErrorHandler.proceed() 也救不回来，
    连接会被直接掐掉（板子那边看到的是客户端发的致命 alert，-0x7780）。
    自签证书的唯一出路就是：把 IP 写进 subjectAltName。
    ⇒ 板子的 IP 变了（DHCP 换了地址）就要用新 IP 重跑本脚本再刷固件。

产物：
    main/br_cert.c  —— 证书与私钥的 PEM 字节数组
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)
OUT_C = os.path.join(BRIDGE, "main", "br_cert.c")

CN = "esp32s3-remote"
DAYS = 3650   # 十年 —— 免得几年后还要重签


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.2.203",
                    help="板子当前的局域网 IP —— 会写进证书的 SAN")
    ap.add_argument("--dns", default="esp32s3-remote",
                    help="额外写进 SAN 的域名（将来接了 mDNS 就能用）")
    args = ap.parse_args()

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    except ImportError:
        print("需要 cryptography：pip install cryptography", file=sys.stderr)
        return 1

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CN),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ESP32S3 Remote Bridge"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)

    # ---- CA（装到手机上的用户证书库用）----
    # 为什么要有 CA：Android 只允许把 basicConstraints CA:TRUE 的证书
    # 装进「CA 证书」槽；而 movo 的 network_security_config.xml 里
    # 明确写了 <certificates src="user"/> ⇒ 用户证书是被信任的。
    # 于是板子发叶子证书、手机信任这张 CA，Chromium 直接认，
    # 不必再依赖 WebView 的 onReceivedSslError → proceed()。
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ESP32S3 Remote Bridge CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ESP32S3 Remote Bridge"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False,
                          key_agreement=False, key_cert_sign=True,
                          crl_sign=True, encipher_only=False,
                          decipher_only=False),
            critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    # 叶子证书：由 CA 签发，带 IP 的 SAN
    leaf = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(args.ip)),
                x509.DNSName(args.dns),
            ]),
            critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()),
            critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = leaf.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    ca_path = os.path.join(HERE, "esp32s3-remote-ca.crt")
    with open(ca_path, "wb") as fh:
        fh.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_key_path = os.path.join(HERE, "esp32s3-remote-ca.key")
    with open(ca_key_path, "wb") as fh:
        fh.write(ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))

    def emit(name: str, blob: bytes) -> None:
        # ★ 末尾必须补一个 NUL，且长度要把它算进去 ★
        #   mbedTLS 4.1 的 mbedtls_x509_crt_parse / mbedtls_pk_parse_key
        #   处理 PEM 时要求缓冲区是「以 NUL 结尾的字符串」，
        #   且 buflen 必须包含那个 NUL 字节 —— 否则报
        #   -0x2180 (MBEDTLS_ERR_X509_INVALID_FORMAT)。
        blob = blob + b"\x00"
        with open(OUT_C, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"const uint8_t {name}[] = {{\n")
            for i in range(0, len(blob), 16):
                chunk = blob[i:i + 16]
                fh.write("    " + "".join(f"0x{b:02X}, " for b in chunk) + "\n")
            fh.write("};\n")
            fh.write(f"const size_t {name}_len = sizeof({name});\n\n")

    with open(OUT_C, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("/*\n")
        fh.write(" * br_cert - 自签证书（由 tools/gen_cert.py 生成，勿手改）\n")
        fh.write(" *\n")
        fh.write(" * 局域网自用：手机第一次连会弹「信任这台电脑？」，用户点一次即记住。\n")
        fh.write(" * ★ 私钥进了仓库 ★ 换设备/换网络时重新跑一次 tools/gen_cert.py。\n")
        fh.write(" */\n")
        fh.write('#include "br_cert.h"\n\n')

    emit("s_cert_pem", cert_pem)
    emit("s_key_pem", key_pem)

    with open(os.path.join(BRIDGE, "main", "br_cert.h"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("/*\n")
        fh.write(" * br_cert - 自签证书（由 tools/gen_cert.py 生成，勿手改）\n")
        fh.write(" */\n")
        fh.write("#pragma once\n\n")
        fh.write("#include <stddef.h>\n#include <stdint.h>\n\n")
        fh.write("extern const uint8_t s_cert_pem[];\n")
        fh.write("extern const size_t s_cert_pem_len;\n")
        fh.write("extern const uint8_t s_key_pem[];\n")
        fh.write("extern const size_t s_key_pem_len;\n")

    print(f"已生成自签证书 CN={CN}，SAN: IP={args.ip}, DNS={args.dns}，"
          f"有效期 {DAYS} 天")
    print(f"  {OUT_C}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
