#!/usr/bin/env python3
"""
小米蓝牙语音遥控器一代 —— 真机取证脚本。

只依赖 pyserial（标准库之外就这一个），不启动 PySide6 界面。

做三件事：
  1. 打开协议通道（COM12），把板子发出来的每一行都按时间戳记进日志文件；
  2. 同时打开固件日志口（COM6），把固件的串口输出也记进同一个文件；
  3. 开一个配对窗口，看到「小米蓝牙语音遥控器 / MI RC」就自动 PAIR；
     等服务发现、加密、HID 订阅全部完成后，只发一次只读 HIDREPORT。
     可选的特征读取/写入必须显式指定，并且逐条间隔执行。

用法：
    python tools/xiaomi_probe.py --port COM12 --serial COM6 --seconds 240

日志落在 client/temp/xiaomi-capture-<时间戳>.log。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time

import serial

# 板子上报的广播名（Windows 侧看到的两个名字）
XIAOMI_NAME_TOKENS = ("小米", "mi rc", "xiaomi")

# SP-v2 的 KV 行：EV SCAN addr=xx:xx:xx:xx:xx:xx type=0 name=xxx score=..
SCAN_RE = re.compile(r"^EV\s+SCAN\b(.*)$")
KV_RE = re.compile(r"(\w+)=([^\s]*)")


def parse_kv(rest: str) -> dict:
    return {m.group(1): m.group(2) for m in KV_RE.finditer(rest)}


def escape_name(name: str) -> str:
    """协议里空格转义成 %20。"""
    return name.replace(" ", "%20")


def unescape_name(name: str) -> str:
    """把 %20 还原成空格（「MI RC」这种名字带空格）。"""
    return name.replace("%20", " ")


class Probe:
    def __init__(self, args):
        self.args = args
        self.log_path = args.log
        self.log = open(self.log_path, "a", encoding="utf-8", newline="\n")
        self.lock = threading.Lock()
        self.paired = False
        self.connected = False
        self.gatt_done = False
        self.encrypted = False
        self.hid_done = False
        self.wrong_device = False
        self.dumped = False
        self.writing = False
        self.serial_port = None
        self.next_read = 0.0
        self.next_write = 0.0
        self.read_index = 0
        self.write_handle = "0"
        self.write_payload = "01"
        self.ready_logged = False

    # ------------------------------------------------------------ 日志
    def write(self, tag: str, line: str):
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.log.write("%s [%s] %s\n" % (stamp, tag, line))
            self.log.flush()

    def say(self, msg: str):
        print(msg, flush=True)

    # ------------------------------------------------------------ 串口
    def open_serial(self, port: str, baud: int, tag: str):
        s = serial.Serial()
        s.port = port
        s.baudrate = baud
        s.timeout = 0.2
        # ★ 打开前先置 DTR/RTS=False ★
        #   CH343（COM6）默认会拉 DTR/RTS → 板子被复位，
        #   复位期间 HTTP/CDC 全挂，看着像「板子死了」。
        try:
            s.dtr = False
            s.rts = False
        except Exception:
            pass
        s.open()
        return s

    def open_protocol(self, port: str):
        """等端口空出来。

        客户端占着 COM12 时直接打开会 PermissionError ——
        等它退出（最多等 3 分钟）比让人手动协调时序靠谱。
        """
        deadline = time.time() + self.args.wait_port
        while True:
            try:
                return self.open_serial(port, 115200, "CDC")
            except Exception as exc:
                if time.time() >= deadline:
                    raise
                self.say("端口 %s 还被占用（%s），等待中…"
                         % (port, exc.__class__.__name__))
                time.sleep(3)

    def pump_serial(self, port: str, baud: int, tag: str):
        try:
            s = self.open_serial(port, baud, tag)
        except Exception as exc:  # 串口打不开不该让整个取证失败
            self.write(tag, "打开 %s 失败: %s" % (port, exc))
            return
        self.serial_port = s
        buf = b""
        while not self.args.stop.is_set():
            try:
                data = s.read(4096)
            except Exception:
                break
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").rstrip("\r")
                if text:
                    self.write(tag, text)
        try:
            s.close()
        except Exception:
            pass

    def pump_stdin(self):
        """标准输入的命令直接转发给板子（联调时不用重启脚本）。"""
        for raw in sys.stdin:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line in ("q", "quit", "exit"):
                self.args.stop.set()
                return
            self.send(line)

    # ------------------------------------------------------------ 协议
    def send(self, cmd: str):
        self.write("TX", cmd)
        self.proto.write((cmd + "\n").encode("utf-8"))

    def ready(self):
        return (self.connected and self.gatt_done and self.encrypted
                and self.hid_done and not getattr(self, "wrong_device", False))

    def commands_due(self, now):
        if not self.ready():
            return []
        commands = []
        if not self.dumped:
            self.dumped = True
            commands.append("HIDREPORT 0")
        if self.args.write_voice and not self.writing:
            self.writing = True
            try:
                self.write_handle, self.write_payload = self.args.write_voice.split(":", 1)
            except ValueError:
                self.write_handle = self.args.write_voice
            self.say("[写] 每 %.1fs 往 handle %s 写 %s" %
                     (self.args.write_every, self.write_handle, self.write_payload))
        if self.read_index < len(self.args.read_handles) and now >= self.next_read:
            commands.append("HIDREAD 0 %d" % self.args.read_handles[self.read_index])
            self.read_index += 1
            self.next_read = now + self.args.read_every
        if self.writing and now >= self.next_write:
            commands.append("HIDWRITE 0 %s %s" %
                            (self.write_handle, self.write_payload))
            self.next_write = now + self.args.write_every
        return commands

    def handle(self, line: str):
        self.write("RX", line)

        if line.startswith("EV DISC") or line.startswith("EV CONNFAIL"):
            self.connected = False
            self.gatt_done = False
            self.encrypted = False
            self.hid_done = False
            self.wrong_device = False
            self.dumped = False
            self.read_index = 0
            self.ready_logged = False
            tag = "[连接失败] " if line.startswith("EV CONNFAIL") else "[已断开] "
            self.say(tag + line)
            return

        m = SCAN_RE.match(line)
        if m:
            kv = parse_kv(m.group(1))
            name = unescape_name(kv.get("name", ""))
            addr = kv.get("addr", "")
            if addr and any(tok in name.lower() for tok in XIAOMI_NAME_TOKENS):
                self.say("[候选] %s  %s" % (addr, name))
                if not self.paired:
                    self.paired = True
                    self.say("[动作] PAIR %s" % addr)
                    self.send("PAIR %s %s %s" %
                              (addr, kv.get("type", "0"),
                               escape_name(name)))
            return

        if line.startswith("EV CONN"):
            self.connected = True
            self.gatt_done = False
            self.encrypted = False
            self.hid_done = False
            self.wrong_device = False
            self.dumped = False
            self.read_index = 0
            self.say("[已连接，等待发现/加密/订阅] " + line)
            return

        if line.startswith("EV GATTDONE"):
            kv = parse_kv(line)
            self.wrong_device = kv.get("atvv") == "1"
            self.gatt_done = not self.wrong_device
            if self.wrong_device:
                self.say("[拒绝取证] 这是 ATVV/Google 遥控器，不是小米: " + line)
            else:
                self.say("[服务发现完成] " + line)
            return

        if line.startswith("EV ENC"):
            kv = parse_kv(line)
            self.encrypted = kv.get("status") == "0" and kv.get("enc") == "1"
            self.say(("[加密%s] " % ("完成" if self.encrypted else "未完成")) + line)
            return

        if line.startswith("EV HID"):
            kv = parse_kv(line)
            if kv.get("phase") == "done":
                self.hid_done = True
                self.say("[HID 订阅完成] " + line)
            return

        if line.startswith("OK PAIR"):
            self.say("[配对已受理] " + line)
            return

        if line.startswith("EV PAIR"):
            self.say("[配对成功] " + line)
            return

        if line.startswith("ER "):
            self.say("[错误] " + line)

    def run(self):
        self.say("日志文件: %s" % self.log_path)

        if self.args.serial:
            t = threading.Thread(target=self.pump_serial,
                                 args=(self.args.serial, 115200, "FW"),
                                 daemon=True)
            t.start()
            self.say("已打开固件日志口 %s" % self.args.serial)

        self.proto = self.open_protocol(self.args.port)
        self.say("已打开协议通道 %s" % self.args.port)

        self.send("INFO")
        self.send("PAIRWINDOW %d" % self.args.window)
        self.say("配对窗口已开 %d 秒 —— 请长按遥控器「主页 + 菜单」3 秒"
                 % self.args.window)

        t = threading.Thread(target=self.pump_stdin, daemon=True)
        t.start()

        deadline = time.time() + self.args.seconds
        buf = b""
        while time.time() < deadline and not self.args.stop.is_set():
            try:
                data = self.proto.read(4096)
            except Exception as exc:
                self.say("协议口读取失败: %s" % exc)
                break
            if data:
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace").rstrip("\r")
                    if text:
                        self.handle(text)

            now = time.time()
            for cmd in self.commands_due(now):
                self.send(cmd)
            if self.ready() and self.dumped and not getattr(self, "ready_logged", False):
                self.ready_logged = True
                self.say("[取证] 链路已就绪，保持被动监听 HIDRAW")
            time.sleep(0.05)

        self.say("会话结束，日志: %s" % self.log_path)
        try:
            self.proto.close()
        except Exception:
            pass
        self.log.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM12", help="协议通道")
    ap.add_argument("--serial", default="COM6", help="固件日志口（可空）")
    ap.add_argument("--seconds", type=int, default=240)
    ap.add_argument("--window", type=int, default=120, help="配对窗口秒数")
    ap.add_argument("--wait-port", type=int, default=180,
                    help="等协议端口空出来的秒数")
    ap.add_argument("--read-handles", default="",
                    help="逗号分隔的可选读取句柄，如 70,74；默认不读")
    ap.add_argument("--read-every", type=float, default=0.4)
    ap.add_argument("--write-voice", default="",
                    help="例：139:01 —— 显式指定后每 0.4 秒写该特征")
    ap.add_argument("--write-every", type=float, default=0.4)
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    client_dir = os.path.dirname(here)
    if not args.log:
        temp_dir = os.path.join(client_dir, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        args.log = os.path.join(
            temp_dir, "xiaomi-capture-%s.log" %
            time.strftime("%Y%m%d-%H%M%S"))

    args.stop = threading.Event()
    args.read_handles = [int(h) for h in args.read_handles.split(",") if h.strip()]
    Probe(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
