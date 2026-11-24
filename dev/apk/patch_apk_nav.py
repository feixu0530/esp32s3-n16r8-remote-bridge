"""
给反编译目录打「冷启动直达本地页面」补丁（幂等）。

背景：
    Android 版冷启动走 `tryConnectOrSetup(file:///android_asset/touchpad.html)`，
    原实现要过一遍 Socket 探针 + Kotlin Result。这个 Result 在 apktool 重建后
    **恒为 false**，于是每次启动都被弹回 setup.html?err=connect。

修法：
    在 `tryConnectOrSetup$lambda$9` 开头短路 `file:` 开头的 URL，
    直接复用原生那条「ok=true → runOnUiThread(Lambda12) → loadUrl」路径。

    ★ 不能在这里直接 loadUrl ★
      这个 lambda 跑在后台线程（Thread-N），WebView 跨线程调用会
      `RuntimeException: A WebView method was called on thread ...` 直接崩。

用法：python dev/apk/patch_apk_nav.py [APK_DIR]
      默认 APK_DIR = C:/tmp/apkdec3
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MARK = "nav_local_asset_shortcut"

SNIPPET = """    .line 215
    # ★ %s ★ BLE 模式：本地页面（file:///android_asset/…）直接 loadUrl 返回。
    #   不走 Socket 探针 + Kotlin Result：那套的 ok 在反编译重建后恒为 false，
    #   实测每次冷启动都被弹回设置页。
    #   必须走 runOnUiThread —— 本函数在后台线程，直接碰 WebView 会崩。
    const-string v0, "file:"

    invoke-virtual {p1, v0}, Ljava/lang/String;->startsWith(Ljava/lang/String;)Z

    move-result v0

    if-eqz v0, :nav_tcp_probe

    new-instance v1, Lcom/dsnaaa/movo/MainActivity$$ExternalSyntheticLambda12;

    const/4 v0, 0x1

    invoke-direct {v1, v0, p0, p1}, Lcom/dsnaaa/movo/MainActivity$$ExternalSyntheticLambda12;-><init>(ZLcom/dsnaaa/movo/MainActivity;Ljava/lang/String;)V

    invoke-virtual {p0, v1}, Lcom/dsnaaa/movo/MainActivity;->runOnUiThread(Ljava/lang/Runnable;)V

    return-void

    :nav_tcp_probe
""" % MARK

METHOD_HEAD = re.compile(
    r"(\.method private static final tryConnectOrSetup\$lambda\$9"
    r"\(Lcom/dsnaaa/movo/MainActivity;Ljava/lang/String;\)V\n"
    r")(\s*\.locals (\d+)\n)"
    r"((?:\s*\.param [^\n]*\n)*)"
)


def main() -> int:
    apk_dir = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:/tmp/apkdec3")
    target = apk_dir / "smali_classes3" / "com" / "dsnaaa" / "movo" / "MainActivity.smali"
    if not target.is_file():
        print("找不到 %s" % target)
        return 1

    text = target.read_text(encoding="utf-8")
    # 认标签而不是注释键：早期手工打的补丁没有 MARK，但同样已经有 :nav_tcp_probe
    if MARK in text or ":nav_tcp_probe" in text:
        print("已打过补丁，跳过：%s" % target.name)
        return 0

    m = METHOD_HEAD.search(text)
    if not m:
        print("没找到 tryConnectOrSetup$lambda$9 的方法头（APK 版本变了？）")
        return 1

    head, locals_line, locals_n, params = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    # 短路块要新开 v0/v1，且原实现也要走到 v8 —— 至少 9 个
    need = max(locals_n, 9)
    new_locals = "    .locals %d\n" % need
    text = text[:m.start()] + head + new_locals + params + SNIPPET + text[m.end():]

    target.write_text(text, encoding="utf-8", newline="\n")
    print("补丁已写入：%s（.locals %d → %d）" % (target.name, locals_n, need))
    return 0


if __name__ == "__main__":
    sys.exit(main())
