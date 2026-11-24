# APK 侧改动（手机遥控 → BLE）

movo 的安卓壳（WebView + 原生桥）没有源码，只有反编译出来的 smali。
这里的做法是：**新代码全部用 Java 写、javac+d8 编成 `classes4.dex` 塞进 APK**，
smali 只动两处最小的地方（各一行/一小段），避免手写 smali 带来的 `VerifyError`。

产物：`releases/<版本>/Fremote-<版本>.apk`

---

## 1. 工作目录（反编译产物）

```
C:/tmp/apkdec3/            ← apktool d 出来的目录
  AndroidManifest.xml
  smali/ smali_classes2/ smali_classes3/
  assets/
  javasrc/com/dsnaaa/movo/BleClient.java   ← 本目录的副本
```

## 2. 不要手写的部分：`BleClient.java`

负责：扫描 / 连接（TRANSPORT_LE）/ 服务发现 / MTU / 使能通知 /
发送队列（WWR，短写重试）/ 地址持久化 / 断线重连看门狗。

关键点（都是踩过的坑）：

| 点 | 为什么 |
|---|---|
| 方法必须 `@JavascriptInterface` | 漏了注解 `window.MovoBle` 就是空壳，页面里根本不出现蓝牙卡片 |
| `connectGatt(..., TRANSPORT_LE)` | 默认走 BR/EDR 连不上 |
| 发送队列串行 + 短写循环 | 一次 `writeCharacteristic` 装不下长行；丢尾巴会让协议解析乱掉 |
| 超长 JSON 切 `type=5` 分帧 | 一帧约 240 字节，中文 3 字节/字，长句必须分帧 |
| 连续 3 次写失败 → 判定断开 | 手机蓝牙被关掉时系统**不一定**回调 `onConnectionStateChange`，状态会卡在 READY |
| 2 秒看门狗重连 | 只在断开回调里重试一次的话，「关蓝牙再开」永远回不来 |

## 3. smali 只改两处

### 3.1 `smali_classes3/com/dsnaaa/movo/AppBridge.smali`

构造函数末尾（`iput-object p2, ... webView` 之后、`return-void` 之前）加一行：

```smali
invoke-static {p1, p2}, Lcom/dsnaaa/movo/BleClient;->install(Landroid/app/Activity;Landroid/webkit/WebView;)V
```

`install()` 内部会 `addJavascriptInterface(this, "MovoBle")`、开机自连、启动看门狗。

### 3.2 `smali_classes3/com/dsnaaa/movo/MainActivity.smali`

`tryConnectOrSetup$lambda$9` 的 try 块开头，加「`file:` 直接算连上」的短路：

```smali
const-string v0, "file:"
invoke-virtual {p1, v0}, Ljava/lang/String;->startsWith(Ljava/lang/String;)Z
move-result v0
if-nez v0, :cond_not_local_asset
const/4 v0, 0x1
invoke-static {v0}, Ljava/lang/Boolean;->valueOf(Z)Ljava/lang/Boolean;
move-result-object v0
invoke-static {v0}, Lkotlin/Result;->constructor-impl(Ljava/lang/Object;)Ljava/lang/Object;
move-result-object v0
goto :goto_1
:cond_not_local_asset
```

为什么：原来的启动流程拿 URL 去 `Socket.connect` 探活，
`file:///android_asset/...` 没有端口可探 → 永远判失败 → 手机永远弹回设置页。

## 4. AndroidManifest.xml 权限

```xml
<uses-permission android:name="android.permission.BLUETOOTH_SCAN" android:usesPermissionFlags="neverForLocation"/>
<uses-permission android:name="android.permission.BLUETOOTH_CONNECT"/>
<uses-permission android:name="android.permission.BLUETOOTH" android:maxSdkVersion="30"/>
<uses-permission android:name="android.permission.BLUETOOTH_ADMIN" android:maxSdkVersion="30"/>
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" android:maxSdkVersion="30"/>
<uses-feature android:name="android.hardware.bluetooth_le" android:required="true"/>
```

`BleClient` 构造时还会再申请一次运行时权限（SDK≥31 是 BLUETOOTH_SCAN/CONNECT，否则定位）。

## 5. 资源（页面）

`assets/` 里的页面来自 `dev/firmware/bridge/web/`，
由 `dev/firmware/bridge/tools/pack_apk_assets.py` 复制并改写：

- `/static/js/x.js` → `x.js`（assets 根平铺）
- `location.replace('/touchpad')` → `'touchpad.html'`、`/k` → `'keyboard.html'`
- 删掉 favicon 与 CDN socket.io 兜底

`setup.html` 以 `dev/firmware/bridge/apk/setup.html` 为准（带蓝牙扫描界面）。

## 6. 构建

```bash
python dev/apk/build_apk.py --src C:/tmp/apkdec3 --out C:/tmp/Fremote-1.2.4.apk
```

脚本做四件事：`javac+d8` 出 dex → `apktool b` → 把 dex 以 `classes4.dex`
塞进 APK 根 → `zipalign` + `apksigner`（movo.keystore）。

`apktool b` 之前会先跑 `patch_apk_nav.py`（幂等）：给
`tryConnectOrSetup$lambda$9` 打「`file:` 开头直接 loadUrl」的短路补丁。
不打这个补丁，重建出来的 APK 每次冷启动都会被弹回 `setup.html?err=connect`
——原实现里的 Kotlin `Result` 在 apktool 重建后恒为 false。
注意短路必须走 `runOnUiThread`（该 lambda 在后台线程，直接碰 WebView 会崩）。

签名库：`movo.keystore`（别名 `movo`，密码 `movo2026`），
证书 SHA-256 `4f9b60ba…f9e9e5` 必须与已发布版本一致，否则手机不让覆盖安装。
