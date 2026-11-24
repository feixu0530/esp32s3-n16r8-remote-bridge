"""
把 web/ 下的页面与 JS 打包进 APK 的 assets/（file:///android_asset/ 方式加载）。

为什么需要改写路径：
  页面原本由板子的 HTTP 服务提供，引用是绝对路径 /static/js/xxx.js、
  跳转是 location.replace('/touchpad')。打成 APK 资源以后这些都不存在，
  全部改成相对文件（js 平铺在 assets/ 根，页面是 xxx.html）。

用法：python tools/pack_apk_assets.py [APK_DIR]
      默认 APK_DIR = C:/tmp/apkdec3
"""
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)              # .../bridge
WEB = os.path.join(BRIDGE, "web")
JS = os.path.join(WEB, "static", "js")
APK = sys.argv[1] if len(sys.argv) > 1 else r"C:/tmp/apkdec3"
ASSETS = os.path.join(APK, "assets")

# 页面跳转：服务端路由 → 本地文件
ROUTES = {
    "/touchpad": "touchpad.html",
    "/k": "keyboard.html",
}

def fix_html(s: str) -> str:
    # /static/js/x.js → x.js（assets 根平铺）
    s = re.sub(r'(["\'\(])/static/js/', r'\1', s)
    # /static/touchpad.png 等图标：APK 里没有，删掉整行
    s = re.sub(r'[ \t]*<link[^>]*href="/static/[^"]*"[^>]*>\s*\n', '', s)
    # 跳转路由
    for route, target in ROUTES.items():
        s = s.replace("location.replace('%s')" % route, "location.replace('%s')" % target)
        s = s.replace('location.replace("%s")' % route, 'location.replace("%s")' % target)
        s = s.replace("location.href = '%s'" % route, "location.href = '%s'" % target)
    # CDN 兜底（file:// 下没网，留着只会多一条失败请求）
    s = re.sub(r'[ \t]*<script>if\(!window\.io\)\{document\.write\([^\n]*\n', '', s)
    return s

def main():
    os.makedirs(ASSETS, exist_ok=True)

    # 1) 页面
    for name in ("touchpad.html", "keyboard.html"):
        src = os.path.join(WEB, name)
        dst = os.path.join(ASSETS, name)
        with open(src, encoding="utf-8") as f:
            html = f.read()
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(fix_html(html))
        print("页面 %-16s %6d 字节" % (name, os.path.getsize(dst)))

    # 2) JS（全部平铺）
    for name in sorted(os.listdir(JS)):
        if not name.endswith(".js"):
            continue
        shutil.copyfile(os.path.join(JS, name), os.path.join(ASSETS, name))
        print("脚本 %-16s %6d 字节" % (name, os.path.getsize(os.path.join(ASSETS, name))))

    # 2.5) 图片（手机快捷键页背景）
    img = os.path.join(WEB, "static", "img")
    if os.path.isdir(img):
        for name in sorted(os.listdir(img)):
            src_f = os.path.join(img, name)
            if os.path.isfile(src_f):
                shutil.copyfile(src_f, os.path.join(ASSETS, name))
                print("图片 %-16s %6d 字节" % (name, os.path.getsize(src_f)))

    # 3) setup.html 以 apk/ 下的为准（带蓝牙扫描界面）
    src = os.path.join(BRIDGE, "apk", "setup.html")
    if os.path.exists(src):
        shutil.copyfile(src, os.path.join(ASSETS, "setup.html"))
        print("设置页 setup.html    %6d 字节" % os.path.getsize(src))

if __name__ == "__main__":
    main()
