package com.dsnaaa.movo;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.SystemClock;
import android.os.Looper;
import android.os.ParcelUuid;
import android.util.Log;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * 手机 ↔ 板子 的 BLE 客户端。
 *
 * 板子是 GATT 外设（广播名 remote-bridge）：
 *   C2P_CTL  手机→板子 事件（Write Without Response）
 *   C2P_BIN  手机→板子 语音（Write Without Response）
 *   P2C_EVT  板子→手机 事件（Notify）
 *
 * ★ 为什么要 BLE 而不是 WiFi ★
 *   板子的 WiFi 与 BLE 共用一根天线，WiFi 常开时多条 BLE 链路撑不住。
 *   手机也走 BLE 之后板子可以不开 WiFi，射频全给 BLE。
 *
 * 帧格式（与固件 br_ble_gatt.c 一致）：
 *   [0]=type(1 JSON / 2 紧凑指针 / 3 ADPCM) [1]=evid [2]=flags [3]=len + payload
 */
public final class BleClient {
    private static final String TAG = "MovoBle";

    public static final UUID SVC =
            UUID.fromString("6d6f766f-0001-4d4f-9a00-00000000abf0");
    public static final UUID C2P_CTL =
            UUID.fromString("6d6f766f-0001-4d4f-9a00-00000000abf1");
    public static final UUID C2P_BIN =
            UUID.fromString("6d6f766f-0001-4d4f-9a00-00000000abf2");
    public static final UUID P2C_EVT =
            UUID.fromString("6d6f766f-0001-4d4f-9a00-00000000abf3");
    private static final UUID CCCD =
            UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    public static final int ST_IDLE = 0, ST_SCANNING = 1, ST_CONNECTING = 2,
            ST_DISCOVERING = 3, ST_READY = 4;

    private final Context appCtx;
    private final Activity activity;
    private final WebView webView;
    private final Handler ui = new Handler(Looper.getMainLooper());

    private BluetoothAdapter adapter;
    private BluetoothLeScanner scanner;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic ctl, bin, evt;

    private int state = ST_IDLE;
    private String lastAddr;
    /* 页面要求暂停自动连接（设置页用：不能一进来就连上又跳走） */
    private boolean autoPaused;
    /* 电脑端「删除手机」后，板子会拒绝连接；退避重试，别 2 秒一次猛敲 */
    private volatile long retryNotBefore;

    /* 发送队列：WWR 有 inflight 上限，必须串行 */
    private final ArrayDeque<byte[]> txq = new ArrayDeque<>();
    private boolean txBusy;
    /* 连续写失败计数：蓝牙被关掉时系统不一定回调 onConnectionStateChange，
       状态会卡在 READY，只能靠「写不进去」判定链路已死。 */
    private int txFail;
    private static final int MAX_INFLIGHT_BYTES = 244;

    /* 扫描回调去重用的地址集合 */
    private final ArrayDeque<String> foundAddrs = new ArrayDeque<>();

    public BleClient(Activity act, WebView wv) {
        this.activity = act;
        this.appCtx = act.getApplicationContext();
        this.webView = wv;
        requestBlePerms();
    }

    /*
     * ★ 蓝牙权限必须在运行时申请 ★
     *   Android 12+ 的 BLUETOOTH_SCAN / CONNECT 只在 Manifest 里声明是不够的，
     *   不申请就连扫描接口都调不动。
     *   这里在构造时顺手申请；用户点了「允许」之后，页面再点一次「扫描」即可。
     */
    private void requestBlePerms() {
        try {
            List<String> need = new ArrayList<>();
            if (Build.VERSION.SDK_INT >= 31) {
                if (activity.checkSelfPermission("android.permission.BLUETOOTH_SCAN")
                        != PackageManager.PERMISSION_GRANTED) {
                    need.add("android.permission.BLUETOOTH_SCAN");
                }
                if (activity.checkSelfPermission("android.permission.BLUETOOTH_CONNECT")
                        != PackageManager.PERMISSION_GRANTED) {
                    need.add("android.permission.BLUETOOTH_CONNECT");
                }
            } else if (activity.checkSelfPermission(
                    android.Manifest.permission.ACCESS_FINE_LOCATION)
                    != PackageManager.PERMISSION_GRANTED) {
                need.add(android.Manifest.permission.ACCESS_FINE_LOCATION);
            }
            if (!need.isEmpty()) {
                activity.requestPermissions(need.toArray(new String[0]), 0x2711);
            }
        } catch (Exception e) {
            Log.w(TAG, "requestBlePerms: " + e);
        }
    }

    /*
     * ★ 静态安装入口 ★
     *   一句话就把桥注册好，而且用静态引用自保（防 GC）——
     *   smali 那边只需要一行 invoke-static，不用加字段、不用动 .locals。
     */
    private static BleClient sInst;

    public static void install(Activity act, WebView wv) {
        if (sInst != null) {
            return;
        }
        sInst = new BleClient(act, wv);
        wv.addJavascriptInterface(sInst, "MovoBle");
        Log.i(TAG, "MovoBle 桥已注册");
        /* ★ 开机自连：地址存在偏好里，页面一加载就开始连（设置页会下发 autopause 挡住）★ */
        sInst.connectSaved();
        /* ★ 重连看门狗 ★ */
        sInst.ui.postDelayed(sInst.watchdog, 2000);
    }

    /*
     * ★ 常驻重连看门狗 ★
     *
     * 以前只在「断开回调」里 postDelayed 重连一次：
     * 用户把手机蓝牙关了再开，那一次尝试发生在蓝牙还关着的时候，
     * ensureAdapter() 直接返回 false，**之后再也没人重试** ——
     * 页面看着像连上过又断了，其实永远不会自己回来。
     * 现在每 2 秒看一眼：空闲 + 有地址 + 蓝牙开着 → 重连。
     */
    private final Runnable watchdog = new Runnable() {
        @Override
        public void run() {
            try {
                if (state == ST_IDLE && !autoConnectBlocked()) {
                    String a = savedAddr();
                    if (a != null && !a.isEmpty() && ensureAdapter()) {
                        Log.i(TAG, "看门狗重连 " + a);
                        connect(a);
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "watchdog: " + e);
            }
            ui.postDelayed(this, 2000);
        }
    };

    // ------------------------------------------------------------ 对外

    public int state() {
        return state;
    }

    public String lastAddr() {
        return lastAddr;
    }

    /* ---------------------------------------------------- 地址持久化 */

    private SharedPreferences prefs() {
        return appCtx.getSharedPreferences("movo", Context.MODE_PRIVATE);
    }

    private String savedAddr() {
        if (lastAddr != null && !lastAddr.isEmpty()) {
            return lastAddr;
        }
        try {
            String a = prefs().getString("ble_addr", "");
            if (a != null && !a.isEmpty()) {
                lastAddr = a;
                return a;
            }
        } catch (Exception ignored) {
        }
        return "";
    }

    private void saveAddr(String addr) {
        try {
            prefs().edit().putString("ble_addr", addr).apply();
        } catch (Exception ignored) {
        }
    }

    /** 把手机机型名报给板子（板子再转给 PC 显示） */
    private void sendPhoneInfo() {
        try {
            String model = (Build.MODEL == null ? "" : Build.MODEL).trim();
            String maker = (Build.MANUFACTURER == null ? "" : Build.MANUFACTURER).trim();
            String name = model;
            if (!maker.isEmpty() && !model.isEmpty()
                    && !model.toLowerCase().startsWith(maker.toLowerCase())) {
                name = maker + " " + model;
            }
            JSONObject data = new JSONObject();
            data.put("name", name);
            data.put("model", model);
            sendJson("{\"ev\":\"phone_info\",\"data\":" + data.toString() + "}");
            Log.i(TAG, "上报机型: " + name);
        } catch (Exception e) {
            Log.w(TAG, "sendPhoneInfo: " + e);
        }
    }

    /** 页面加载完自动连一次（有保存地址才连） */
    public void connectSaved() {
        if (autoConnectBlocked()) {
            return;
        }
        String a = savedAddr();
        if (a != null && !a.isEmpty() && state != ST_READY) {
            ui.postDelayed(() -> connect(a), 300);
        }
    }

    /**
     * 高频指针事件走紧凑二进制帧（type=2, evid=1, 两个 int16 LE）。
     * JSON 一帧约 30 字节且板子要解析；二进制 8 字节，120Hz 时差别很大。
     */
    public boolean sendMove(String arg) {
        if (arg == null) {
            return false;
        }
        int dx = 0, dy = 0;
        try {
            String s = arg.trim();
            int i = s.indexOf(',');
            if (i > 0) {
                dx = Integer.parseInt(s.substring(0, i).trim());
                dy = Integer.parseInt(s.substring(i + 1).trim());
            } else {
                dx = Integer.parseInt(s);
            }
        } catch (Exception e) {
            return false;
        }
        dx = Math.max(-32768, Math.min(32767, dx));
        dy = Math.max(-32768, Math.min(32767, dy));
        /*
         * ★ 指针位移绝不排队 ★
         *   链路一卡，队列里的 move 会在恢复后连着回放 ——
         *   用户看到的是「光标自己一路飞过去」。
         *   位移是可丢的：积压超过 8 帧就丢掉这一条，只保留最新的手感。
         */
        if (txq.size() > 8) {
            return true;
        }
        byte[] p = new byte[]{
                (byte) (dx & 0xff), (byte) ((dx >> 8) & 0xff),
                (byte) (dy & 0xff), (byte) ((dy >> 8) & 0xff)};
        enqueue(ctl, new byte[]{2, 1, 0, (byte) p.length}, p);
        return true;
    }

    /**
     * 现在该不该「自动」去连（页面显式要求连接时不受影响）。
     *
     * ★ 只看页面下发的 autopause，绝不碰 WebView 的任何方法 ★
     *   以前为了判断“当前是不是设置页”去调 webView.getUrl()，
     *   结果应用冷启动时会走异常分支、被当成连接失败弹回设置页
     *   （实测：换上新 dex 后每次启动都停在 setup.html?err=connect）。
     */
    private boolean autoConnectBlocked() {
        return autoPaused || SystemClock.uptimeMillis() < retryNotBefore;
    }

    /**
     * JS 桥入口：op = scan/connect/disconnect/state/emit/voice/move/addr/autopause
     * ★ 必须带 @JavascriptInterface ★ Android 4.4 之后只有显式标注的方法才会暴露给页面，
     *   漏了注解页面里 window.MovoBle 就是个空壳（现象：BLE 卡片根本不出现）。
     */
    @android.webkit.JavascriptInterface
    public String call(String op, String arg) {
        if (op == null) {
            return "";
        }
        switch (op) {
            case "scan":
                startScan();
                return "ok";
            case "connect":
                connect(arg);
                return "ok";
            case "disconnect":
                disconnect();
                return "ok";
            case "state":
                return Integer.toString(state);
            case "move":
                return sendMove(arg) ? "ok" : "no";
            case "addr":
                return savedAddr();
            case "autopause":
                autoPaused = "1".equals(arg);
                Log.i(TAG, "autoPaused=" + autoPaused);
                return "ok";
            case "emit":
                return sendJson(arg) ? "ok" : "no";
            case "voice":
                return sendVoice(arg) ? "ok" : "no";
            default:
                return "";
        }
    }

    // ------------------------------------------------------------ 扫描

    public void startScan() {
        if (!ensureAdapter()) {
            return;
        }
        if (scanner == null) {
            scanner = adapter.getBluetoothLeScanner();
        }
        if (scanner == null) {
            Log.w(TAG, "no scanner (蓝牙关着？)");
            return;
        }
        foundAddrs.clear();

        ScanFilter filter = new ScanFilter.Builder()
                .setServiceUuid(new ParcelUuid(SVC))
                .build();
        List<ScanFilter> filters = new ArrayList<>();
        filters.add(filter);
        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
                .build();
        try {
            scanner.startScan(filters, settings, scanCb);
        } catch (Exception e) {
            /* 有的机型按 service 过滤会抛，退回不过滤 */
            Log.w(TAG, "filtered scan failed, retry unfiltered: " + e);
            scanner.startScan(null, settings, scanCb);
        }
        setState(ST_SCANNING);
        ui.postDelayed(this::stopScan, 15000);
    }

    private void stopScan() {
        if (scanner != null) {
            try {
                scanner.stopScan(scanCb);
            } catch (Exception ignored) {
            }
        }
        if (state == ST_SCANNING) {
            setState(ST_IDLE);
        }
    }

    private final ScanCallback scanCb = new ScanCallback() {
        @Override
        public void onScanResult(int callbackType, ScanResult result) {
            BluetoothDevice d = result.getDevice();
            String name;
            try {
                name = result.getScanRecord() != null
                        ? result.getScanRecord().getDeviceName() : null;
            } catch (Exception e) {
                name = null;
            }
            boolean ours = "remote-bridge".equals(name) || "movo-bridge".equals(name);
            if (!ours && result.getScanRecord() != null
                    && result.getScanRecord().getServiceUuids() != null) {
                for (ParcelUuid u : result.getScanRecord().getServiceUuids()) {
                    if (SVC.equals(u.getUuid())) {
                        ours = true;
                        break;
                    }
                }
            }
            if (!ours) {
                return;
            }
            String addr = d.getAddress();
            if (foundAddrs.contains(addr)) {
                return;
            }
            foundAddrs.add(addr);
            pushScanResult(addr, name != null ? name : "remote-bridge",
                    result.getRssi());
        }

        @Override
        public void onScanFailed(int errorCode) {
            Log.w(TAG, "scan failed " + errorCode);
        }
    };

    private void pushScanResult(String addr, String name, int rssi) {
        try {
            JSONArray arr = new JSONArray();
            JSONObject o = new JSONObject();
            o.put("addr", addr);
            o.put("name", name);
            o.put("rssi", rssi);
            arr.put(o);
            final String js = "window.__movoScanResult && window.__movoScanResult("
                    + arr.toString() + ");";
            ui.post(() -> webView.evaluateJavascript(js, null));
        } catch (Exception ignored) {
        }
    }

    // ------------------------------------------------------------ 连接

    /** 连接。addr 为空时用偏好里保存的地址（页面无需自己记地址）。 */
    public void connect(String addr) {
        if (addr == null || addr.isEmpty()) {
            addr = savedAddr();
        }
        if (addr == null || addr.isEmpty()) {
            Log.w(TAG, "connect: 没有地址");
            return;
        }
        if (!ensureAdapter()) {
            return;
        }
        stopScan();
        disconnect();
        saveAddr(addr);
        BluetoothDevice dev;
        try {
            dev = adapter.getRemoteDevice(addr);
        } catch (Exception e) {
            Log.w(TAG, "bad addr " + addr);
            return;
        }
        lastAddr = addr;
        retryNotBefore = 0;
        setState(ST_CONNECTING);
        /* 需要 TRANSPORT_LE —— 默认会走 BR/EDR 导致连不上 */
        if (Build.VERSION.SDK_INT >= 23) {
            gatt = dev.connectGatt(appCtx, false, gattCb, BluetoothDevice.TRANSPORT_LE);
        } else {
            gatt = dev.connectGatt(appCtx, false, gattCb);
        }
    }

    public void disconnect() {
        if (gatt != null) {
            try {
                gatt.disconnect();
                gatt.close();
            } catch (Exception ignored) {
            }
            gatt = null;
        }
        ctl = bin = evt = null;
        txq.clear();
        txBusy = false;
        setState(ST_IDLE);
    }

    private final BluetoothGattCallback gattCb = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                Log.i(TAG, "connected, discovering");
                setState(ST_DISCOVERING);
                g.discoverServices();
            } else {
                Log.i(TAG, "disconnected status=" + status);
                if (status != 0 && status != 19) {
                    /* 连接阶段失败（133 之类）：退避 10 秒，别打成重连风暴 */
                    retryNotBefore = SystemClock.uptimeMillis() + 10000L;
                }
                disconnect();
                /* ★ 断开自动重连 ★ 手机上不能没人管：用户还要用触控板 */
                if (lastAddr != null) {
                    ui.postDelayed(() -> {
                        if (state == ST_IDLE && !autoConnectBlocked()) {
                            connect(lastAddr);
                        }
                    }, 800);
                }
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                Log.w(TAG, "discover failed " + status);
                return;
            }
            BluetoothGattService s = g.getService(SVC);
            if (s == null) {
                Log.w(TAG, "service not found");
                return;
            }
            ctl = s.getCharacteristic(C2P_CTL);
            bin = s.getCharacteristic(C2P_BIN);
            evt = s.getCharacteristic(P2C_EVT);
            if (ctl == null || bin == null || evt == null) {
                Log.w(TAG, "characteristic missing");
                return;
            }
            /* 协商 MTU（244 载荷最省协议开销） */
            try {
                g.requestMtu(247);
            } catch (Exception ignored) {
            }
            enableNotify(g);
        }

        @Override
        public void onMtuChanged(BluetoothGatt g, int mtu, int status) {
            Log.i(TAG, "mtu=" + mtu);
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d,
                                      int status) {
            if (CCCD.equals(d.getUuid())) {
                Log.i(TAG, "notify enabled status=" + status);
                setState(ST_READY);
                /* 连上就把机型名报给板子 —— 板子读不到安卓的 GATT 0x2A00，
                   这样 PC 的「手机」那一行才能显示真实机型（如 23013RK75C）。 */
                sendPhoneInfo();
            }
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g,
                                            BluetoothGattCharacteristic ch) {
            if (!P2C_EVT.equals(ch.getUuid())) {
                return;
            }
            byte[] v = ch.getValue();
            if (v == null) {
                return;
            }
            final String text = new String(v);
            if (text.contains("\"ev\":\"denied\"")) {
                /* 板子说「你已被移除」：15 秒后再试，并让页面提示重新配对 */
                retryNotBefore = SystemClock.uptimeMillis() + 15000L;
                ui.post(() -> {
                    try {
                        webView.evaluateJavascript(
                                "window.__movoDenied && window.__movoDenied();", null);
                    } catch (Exception ignored) {
                    }
                });
            }
            final String js = "window.__movoBleEvent && window.__movoBleEvent("
                    + quote(text) + ");";
            ui.post(() -> webView.evaluateJavascript(js, null));
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g,
                                          BluetoothGattCharacteristic ch,
                                          int status) {
            /* ★ 上一包写完了才放下一个 ★
               Android 对 write-no-response 也有 inflight 上限，
               不节流的话语音包会被系统悄悄丢掉。 */
            txBusy = false;
            pump();
        }
    };

    private void enableNotify(BluetoothGatt g) {
        try {
            g.setCharacteristicNotification(evt, true);
            BluetoothGattDescriptor d = evt.getDescriptor(CCCD);
            if (d == null) {
                Log.w(TAG, "no CCCD descriptor");
                setState(ST_READY);
                return;
            }
            d.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
            g.writeDescriptor(d);
        } catch (Exception e) {
            Log.w(TAG, "enableNotify: " + e);
            setState(ST_READY);
        }
    }

    // ------------------------------------------------------------ 发送

    /**
     * JSON 控制帧：{"ev":"...","data":{...}} 原样交给板子。
     *
     * ★ 超长要分帧 ★
     *   一帧载荷上限约 240 字节（MTU 256 - 帧头 4 - ATT 头 3）。
     *   中文 3 字节/字，一句长点的句子就超了 ——
     *   以前直接丢弃（现象：英文能打，中文多了完全没反应）。
     *   现在切成 type=5 分帧（flags: bit0=第一帧, bit1=最后一帧），
     *   板子按顺序拼回完整 JSON 再处理。
     */
    public boolean sendJson(String json) {
        if (json == null || json.isEmpty()) {
            return false;
        }
        final int MAX = 236;
        byte[] p = json.getBytes();
        if (p.length <= MAX) {
            enqueue(ctl, new byte[]{1, 0, 0, (byte) p.length}, p);
            return true;
        }
        int off = 0, idx = 0;
        while (off < p.length) {
            int n = Math.min(MAX, p.length - off);
            int flags = (idx == 0 ? 1 : 0) | ((off + n) >= p.length ? 2 : 0);
            byte[] part = java.util.Arrays.copyOfRange(p, off, off + n);
            enqueue(ctl, new byte[]{5, 0, (byte) flags, (byte) n}, part);
            off += n;
            idx++;
        }
        return true;
    }

    /**
     * 语音：ADPCM/PCM 字节，按 240 分帧（first/last 标记）。
     * 入参带前缀："f|" 表示这段是**整段录音的第一包**（板子据此清解码器状态），
     * "n|" 表示普通包 —— 板子只认第一包，不能每包都清。
     */
    public boolean sendVoice(String b64) {
        if (b64 == null || b64.isEmpty()) {
            return false;
        }
        boolean firstChunk = b64.charAt(0) == 'f';
        int bar = b64.indexOf('|');
        if (bar >= 0 && bar < 3) {
            b64 = b64.substring(bar + 1);
        }
        byte[] all;
        try {
            all = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
        } catch (Exception e) {
            return false;
        }
        int off = 0;
        int idx = 0;
        while (off < all.length) {
            int n = Math.min(240, all.length - off);
            int flags = 0;
            if (idx == 0 && firstChunk) {
                flags |= 1;
            }
            if (off + n >= all.length) {
                flags |= 2;
            }
            byte[] p = new byte[n];
            System.arraycopy(all, off, p, 0, n);
            enqueue(bin, new byte[]{3, 0, (byte) flags, (byte) n}, p);
            off += n;
            idx++;
        }
        return true;
    }

    private void enqueue(BluetoothGattCharacteristic ch, byte[] hdr, byte[] payload) {
        if (ch == null) {
            return;
        }
        byte[] f = new byte[4 + payload.length];
        System.arraycopy(hdr, 0, f, 0, 4);
        System.arraycopy(payload, 0, f, 4, payload.length);
        while (txq.size() > 96) {   /* 链路卡住时丢最旧的，别把内存吃光 */
            txq.poll();
        }
        txq.add(f);
        pump();
    }

    private void pump() {
        if (txBusy || txq.isEmpty() || gatt == null || ctl == null) {
            return;
        }
        byte[] f = txq.poll();
        if (f == null) {
            return;
        }
        BluetoothGattCharacteristic ch = (f[0] == 3) ? bin : ctl;
        if (ch == null) {
            return;
        }
        txBusy = true;
        try {
            ch.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
            ch.setValue(f);
            if (!gatt.writeCharacteristic(ch)) {
                txBusy = false;
                txFail++;
                Log.w(TAG, "writeCharacteristic returned false (#" + txFail + ")");
                if (txFail >= 3) {
                    /* 连续 3 次写不进去 = 链路已经死了（蓝牙关了/板子重启/出范围） */
                    txFail = 0;
                    Log.i(TAG, "连续写失败，判定断开并重连");
                    disconnect();
                }
            } else {
                txFail = 0;
            }
        } catch (Exception e) {
            txBusy = false;
            Log.w(TAG, "write: " + e);
        }
    }

    // ------------------------------------------------------------ 杂项

    private void setState(int s) {
        state = s;
        final String js = "window.__movoBleState && window.__movoBleState(" + s + ");";
        ui.post(() -> webView.evaluateJavascript(js, null));
    }

    private boolean ensureAdapter() {
        /* ★ 每次重新取 ★ 蓝牙关掉再打开时，缓存的 BluetoothAdapter 可能已经失效，
           拿它 connectGatt 会静默失败（看起来像「怎么都不重连」）。 */
        if (adapter == null || !adapter.isEnabled()) {
            adapter = null;
        }
        try {
            BluetoothManager m = (BluetoothManager) appCtx.getSystemService(
                    Context.BLUETOOTH_SERVICE);
            adapter = m != null ? m.getAdapter() : BluetoothAdapter.getDefaultAdapter();
        } catch (Exception e) {
            Log.w(TAG, "no adapter: " + e);
        }
        if (adapter == null) {
            return false;
        }
        if (!adapter.isEnabled()) {
            return false;
        }
        return true;
    }

    private static String quote(String s) {
        try {
            return JSONObject.quote(s);
        } catch (Exception e) {
            return "\"\"";
        }
    }
}
