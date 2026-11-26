"""Task 2 gate: Xiaomi voice routing and host arbitration regression.

Run from the repository root:
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --python 3.13 --no-project \
        python dev/client/tests/test_xiaomi_voice.py

The host C test exercises dual-signal press/release arbitration.  The source
checks below protect the firmware routing order, queue edge preservation, and
owner-aware hotkey behavior without requiring ESP-IDF.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FW = ROOT / "dev" / "firmware" / "bridge"
MAIN = FW / "main"
TEMP = ROOT / "temp"
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")
        FAILURES.append(name)


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def function_body(text: str, name: str) -> str:
    """Return a C function body, including nested braces."""
    match = re.search(r"(?m)^[ \t]*(?:[A-Za-z_]\w*[ \t]+)+" +
                      re.escape(name) + r"\s*\([^;{}]*\)\s*\{", text)
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    for pos in range(start, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[start:pos + 1]
    return ""


def test_host_arbitration() -> None:
    TEMP.mkdir(exist_ok=True)
    exe = TEMP / "test_voice_input.exe"
    command = [
        "gcc", "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-I", str(MAIN),
        str(MAIN / "br_voice_input.c"),
        str(FW / "test" / "test_voice_input.c"),
        "-o", str(exe),
    ]
    built = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    check("host arbitration compiles with -Werror", built.returncode == 0,
          (built.stdout + built.stderr).strip())
    if built.returncode != 0:
        return
    ran = subprocess.run([str(exe)], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    output = (ran.stdout + ran.stderr).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    check("host arbitration passes with final 全部通过",
          ran.returncode == 0 and bool(lines) and lines[-1] == "全部通过",
          output)


def test_registration() -> None:
    cmake = source(MAIN / "CMakeLists.txt")
    check("CMake registers br_voice_input.c/.h",
          '"br_voice_input.c"' in cmake and '"br_voice_input.h"' in cmake)


def test_hid_routing() -> None:
    text = source(MAIN / "br_hid.c")
    header = source(MAIN / "br_hid.h")
    notify = function_body(text, "br_hid_on_notify")
    check("HID exposes a peer-aware voice callback",
          "br_hid_voice_fn" in header and "br_hid_set_voice_handler" in header
          and "s_voice_fn" in text)
    raw_at = notify.find('br_proto_send_ev("HIDRAW"')
    identify_at = notify.find("len == 20")
    guard_at = notify.find("c->xiaomi_mode && r->report_type != 1",
                           identify_at + 1)
    check("HIDRAW precedes Xiaomi identification and Feature guard",
          raw_at >= 0 and identify_at > raw_at and guard_at > identify_at,
          f"raw={raw_at} identify={identify_at} guard={guard_at}")
    identify_start = notify.rfind("if (", 0, identify_at)
    identify = notify[identify_start:guard_at] if 0 <= identify_start < guard_at else ""
    check("20-byte identification uses Xiaomi metadata, not handle 66",
          all(token in identify for token in
              ("c->xiaomi_mode", "len == 20", "r->report_type != 1",
               "data[0] <= 1"))
          and not re.search(r"attr_handle\s*==\s*66\b", notify),
          identify.strip())
    check("control callback maps only byte 1 to down",
          "xiaomi_voice_event(c, data[0] == 1)" in identify
          and "voice_down" in text)
    check("Feature guard still returns before key parsing",
          guard_at >= 0 and "r->has_last = true;" in notify[guard_at:]
          and "return;" in notify[guard_at:guard_at + 400])
    check("60-byte reports remain raw evidence only",
          "raw_stream" in notify and "if (raw_stream)" in notify
          and "BR_HID_RAW_HEX_MAX" in text)
    check("no A001/A002 or invented Output Report activation",
          not re.search(r"A001|A002|Output Report", text, re.IGNORECASE))


def test_voice_queue() -> None:
    text = source(MAIN / "br_ble_core.c")
    check("dedicated 16-entry voice queue exists",
          re.search(r"#define\s+VOICEQ_LEN\s+16\b", text) is not None
          and "static voice_item_t s_voice_items[VOICEQ_LEN];" in text
          and "SemaphoreHandle_t s_voice_process_lock" in text)
    check("voice process lock is created before BLE callbacks start",
          "s_voice_process_lock = xSemaphoreCreateMutex();" in text
          and "voice process lock alloc failed" in text)
    enqueue = function_body(text, "voice_queue_enqueue")
    check("enqueue serializes with disconnect and rejects stale peers",
          all(token in enqueue for token in
              ("xSemaphoreTake(s_voice_process_lock", "s_voice_items[s_voice_count++] = item",
               ".connected", ".xiaomi", "xSemaphoreGive(s_voice_process_lock")),
          enqueue)
    check("queue dedups only consecutive identical state per peer/source",
          "s_voice_items[i].peer_idx == item.peer_idx" in enqueue
          and "s_voice_items[i].source == item.source" in enqueue
          and "s_voice_items[i].down == item.down" in enqueue
          and "return;" in enqueue,
          enqueue)
    check("full queue drops oldest down, never a pending up",
          "for (int i = 0; i < VOICEQ_LEN; i++)" in enqueue
          and "if (s_voice_items[i].down)" in enqueue
          and "memmove" in enqueue
          and "if (drop < 0)" in enqueue
          and "rejected = true;" in enqueue,
          enqueue)
    check("opposite down/up edges are not overwritten",
          "s_voice_items[s_voice_count++] = item" in enqueue
          and "s_voice_items[s_voice_count - 1] = item" not in enqueue,
          enqueue)
    control_cb = function_body(text, "on_hid_voice")
    check("control callback preserves arrival order in the shared voice queue",
          "voice_queue_enqueue(peer_idx, BR_VOICE_INPUT_CONTROL, down);" in control_cb,
          control_cb)
    voice_drain = function_body(text, "drain_voice_queue")
    check("drain processes one edge under the disconnect lock, not a stale batch",
          all(token in voice_drain for token in
              ("xSemaphoreTake(s_voice_process_lock", "s_voice_items[0]",
               "memmove(&s_voice_items[0]", ".connected", ".xiaomi",
               "br_voice_input_update", "xSemaphoreGive(s_voice_process_lock"))
          and "voice_item_t items[VOICEQ_LEN]" not in voice_drain
          and "memcpy(items" not in voice_drain,
          voice_drain)
    key_drain_at = text.find("把按键事件发成协议行")
    check("voice queue drains before ordinary key queue",
          voice_drain and "drain_voice_queue(now_ms);" in text
          and text.find("drain_voice_queue(now_ms);") < key_drain_at)
    result_handler = function_body(text, "handle_xiaomi_voice_result")
    check("queued sources feed arbitration and every round handles 80ms tick",
          all(token in voice_drain for token in
              ("item.source", "item.down", "br_voice_input_tick"))
          and "br_voice_xiaomi_press" in result_handler
          and "br_voice_xiaomi_release" in result_handler,
          voice_drain)


def test_xiaomi_key_interception() -> None:
    text = source(MAIN / "br_ble_core.c")
    check("peer state has an explicit xiaomi flag",
          re.search(r"bool\s+xiaomi\s*;", text) is not None
          and "p->xiaomi = xiaomi;" in text)
    key_cb = function_body(text, "on_hid_key")
    voice_at = key_cb.find("k.usage == 0x3E")
    enqueue_at = key_cb.find("voice_queue_enqueue", voice_at)
    keyq_at = key_cb.find("xQueueSend(s_keyq", voice_at)
    led_at = key_cb.find("bridge_led_key_down")
    check("Xiaomi keyboard 0x3E joins the voice queue at HID callback time",
          0 <= led_at < voice_at < enqueue_at < keyq_at
          and "BR_VOICE_INPUT_KEYBOARD" in key_cb[enqueue_at:keyq_at]
          and "return;" in key_cb[enqueue_at:keyq_at],
          key_cb)
    check("keyboard interception is limited to connected Xiaomi keyboard reports",
          all(token in key_cb[max(0, key_cb.rfind("if (", 0, voice_at)):enqueue_at]
              for token in ("s_peers[k.peer_idx].connected", "s_peers[k.peer_idx].xiaomi",
                            "k.src == BR_SRC_KEYBOARD")),
          key_cb[max(0, key_cb.rfind("if (", 0, voice_at)):enqueue_at])
    key_at = text.find("while (n < 32 && xQueueReceive(s_keyq")
    tail = text[key_at:] if key_at >= 0 else ""
    intercept_at = tail.find("k.usage == 0x3E")
    learn_at = tail.find("if (s_learn_mode")
    mapping_at = tail.find("br_mapping_key(")
    event_at = tail.find('br_proto_send_ev("KEY"')
    check("stale Xiaomi 0x3E is consumed before LEARN/mapping/EV KEY",
          0 <= intercept_at < learn_at < mapping_at < event_at
          and "continue;" in tail[intercept_at:learn_at]
          and "br_voice_input_update" not in tail[intercept_at:learn_at],
          f"intercept={intercept_at} learn={learn_at} map={mapping_at} key={event_at}")
    check("non-Xiaomi F5 keeps the ordinary path",
          "s_peers[k.peer_idx].xiaomi" in
          tail[max(0, tail.rfind("if (", 0, intercept_at)):learn_at])


def test_voice_events() -> None:
    text = source(MAIN / "br_ble_core.c")
    helper = function_body(text, "emit_voice_event")
    check("shared EV VOICE helper reports truthful hotkey state",
          all(token in helper for token in
              ('br_proto_send_ev("VOICE"', '"idx"', '"addr"', '"sent"',
               '"on"', '"mods"', '"keys"', '"t"')),
          helper)
    check("Xiaomi source distinguishes control and keyboard",
          '"src"' in helper and "xiaomi_control" in text
          and "xiaomi_key" in text)
    check("canonical peer address is used in voice events",
          "map_addr_valid" in helper and "map_addr" in helper)


def test_owner_arbitration() -> None:
    text = source(MAIN / "br_voice.c")
    header = source(MAIN / "br_voice.h")
    check("owner enum covers NONE/ATVV/PHONE/XIAOMI",
          re.search(r"VOICE_OWNER_NONE\s*=\s*0", text) is not None
          and all(name in text for name in
                  ("VOICE_OWNER_ATVV", "VOICE_OWNER_PHONE", "VOICE_OWNER_XIAOMI")))
    check("session mutex serializes owner transitions with USB reports",
          "SemaphoreHandle_t s_voice_session_lock" in text
          and "s_voice_session_lock = xSemaphoreCreateMutex();" in text)
    acquire_locked = function_body(text, "voice_hotkey_acquire_locked")
    release_locked = function_body(text, "voice_hotkey_release_owner_locked")
    xiaomi_press = function_body(text, "br_voice_xiaomi_press")
    xiaomi_release = function_body(text, "br_voice_xiaomi_release")
    check("Xiaomi press holds the session mutex across locked acquisition",
          "xSemaphoreTake(s_voice_session_lock" in xiaomi_press
          and "voice_hotkey_acquire_locked(VOICE_OWNER_XIAOMI, peer_idx)" in xiaomi_press
          and "xSemaphoreGive(s_voice_session_lock)" in xiaomi_press,
          xiaomi_press)
    check("locked acquire publishes owner before emitting USB press",
          acquire_locked.find("s_hotkey_owner = owner;") >= 0
          and acquire_locked.find("voice_hotkey_press()") >
          acquire_locked.find("s_hotkey_owner = owner;"),
          acquire_locked)
    check("acquire is first-wins and records owner peer",
          "s_hotkey_owner != VOICE_OWNER_NONE" in acquire_locked
          and "s_hotkey_owner = owner;" in acquire_locked
          and "s_owner_peer = peer_idx;" in acquire_locked
          and "return false;" in acquire_locked)
    check("Xiaomi release holds the session mutex across owner release",
          "xSemaphoreTake(s_voice_session_lock" in xiaomi_release
          and "voice_hotkey_release_owner_locked(VOICE_OWNER_XIAOMI, peer_idx)"
          in xiaomi_release
          and "xSemaphoreGive(s_voice_session_lock)" in xiaomi_release,
          xiaomi_release)
    check("release requires matching owner and peer",
          "s_hotkey_owner != owner" in release_locked
          and "s_owner_peer != peer_idx" in release_locked,
          release_locked)
    check("Xiaomi press/release and peer-aware disconnect APIs exist",
          all(name in header for name in
              ("br_voice_xiaomi_press", "br_voice_xiaomi_release",
               "br_voice_on_disconnect"))
          and "int8_t peer_idx" in header)
    check("ATVV and phone use serialized owner-specific transitions",
          "xSemaphoreTake(s_voice_session_lock" in function_body(text, "br_voice_control")
          and "voice_hotkey_acquire_locked(VOICE_OWNER_ATVV" in text
          and "voice_hotkey_release_owner_locked(VOICE_OWNER_ATVV" in text
          and "xSemaphoreTake(s_voice_session_lock" in function_body(text, "br_voice_phone_start")
          and "voice_hotkey_acquire_locked(VOICE_OWNER_PHONE" in text
          and "voice_hotkey_release_owner_locked(VOICE_OWNER_PHONE" in text)
    disconnect = function_body(text, "br_voice_on_disconnect")
    check("disconnect serializes reset and releases only the matching peer",
          "xSemaphoreTake(s_voice_session_lock" in disconnect
          and "br_voice_input_peer_reset(peer_idx" in disconnect
          and "voice_hotkey_release_owner_locked(VOICE_OWNER_XIAOMI, peer_idx)" in disconnect
          and "voice_hotkey_release_owner_locked(VOICE_OWNER_ATVV, peer_idx)" in disconnect,
          disconnect)


def test_physical_hold() -> None:
    text = source(MAIN / "br_voice.c")
    watchdog = function_body(text, "br_voice_watchdog")
    check("physical hold has a 60000 ms maximum",
          re.search(r"#define\s+BR_VOICE_PHYSICAL_MAX_MS\s+60000u", text) is not None)
    check("watchdog revalidates and transitions owners under the session mutex",
          "xSemaphoreTake(s_voice_session_lock" in watchdog
          and "xSemaphoreGive(s_voice_session_lock)" in watchdog
          and "voice_hotkey_release_force();" not in watchdog,
          watchdog)
    physical_at = watchdog.find("s_physical_hold")
    no_stream_at = watchdog.find("BR_VOICE_NO_STREAM_GRACE_MS")
    check("physical-hold branch bypasses 1.5 s no-stream release",
          0 <= physical_at < no_stream_at
          and "BR_VOICE_PHYSICAL_MAX_MS" in watchdog
          and "return;" in watchdog[physical_at:no_stream_at],
          watchdog)
    check("physical timeout resets Xiaomi input and releases only its owner",
          "br_voice_input_reset();" in watchdog[physical_at:no_stream_at]
          and "voice_hotkey_release_owner_locked(VOICE_OWNER_XIAOMI, owner_peer);"
          in watchdog[physical_at:no_stream_at]
          if 0 <= physical_at < no_stream_at else False)
    check("stale ATVV/phone timeout cannot force-release a newer owner",
          "voice_hotkey_release_owner_locked(owner, owner_peer)" in watchdog
          and "voice_hotkey_release_force();" not in watchdog,
          watchdog)
    press = function_body(text, "br_voice_xiaomi_press")
    release = function_body(text, "voice_hotkey_release_owner_locked")
    check("Xiaomi press arms physical hold only on ownership",
          "voice_hotkey_acquire_locked(VOICE_OWNER_XIAOMI" in press
          and "s_physical_hold = true;" in text)
    check("Xiaomi release clears physical hold only for its owner",
          "s_hotkey_owner != owner || s_owner_peer != peer_idx" in release
          and "owner == VOICE_OWNER_XIAOMI" in release
          and "s_physical_hold = false;" in release)


def test_disable_cleanup() -> None:
    text = source(MAIN / "br_voice.c")
    disable = function_body(text, "br_voice_set_enabled")
    take_at = disable.find("xSemaphoreTake(s_voice_session_lock")
    give_at = disable.find("xSemaphoreGive(s_voice_session_lock")
    nvs_at = disable.find("nvs_open(")
    check("disable persists NVS outside the voice session mutex",
          0 <= take_at < give_at < nvs_at,
          f"take={take_at} give={give_at} nvs={nvs_at}")
    locked = disable[take_at:give_at] if 0 <= take_at < give_at else ""
    check("disable clears hotkey and every active audio session",
          all(token in locked for token in
              ("voice_hotkey_release_force_locked();", "br_voice_input_reset();",
               "s_streaming = false;", "s_phone_active = false;",
               "s_atvv_peer = -1;", "clear_pcm();", "clear_phone_pcm();")),
          locked)


def test_atvv_peer_isolation() -> None:
    text = source(MAIN / "br_voice.c")
    header = source(MAIN / "br_voice.h")
    control = function_body(text, "br_voice_control")
    audio = function_body(text, "br_voice_audio")
    check("ATVV control and audio APIs carry the originating peer",
          "br_voice_control(const uint8_t *data, size_t len, int8_t peer_idx)" in header
          and "br_voice_audio(const uint8_t *data, size_t len, int8_t peer_idx)" in header
          and "peer_idx < 0 || peer_idx >= BR_MAX_PEERS" in audio)
    check("ATVV decoder state is accepted only from its current owner peer",
          "s_atvv_peer" in text
          and "s_hotkey_owner != VOICE_OWNER_ATVV" in audio
          and "s_owner_peer != peer_idx" in audio
          and "s_atvv_peer != peer_idx" in audio,
          audio)
    check("CAPS metadata is per peer and SYNC cannot corrupt another stream",
          "s_atvv_frame_size[BR_MAX_PEERS]" in text
          and "s_atvv_frame_size[peer_idx]" in control
          and "case 0x0A:" in control
          and "s_hotkey_owner == VOICE_OWNER_ATVV" in control
          and "s_owner_peer == peer_idx && s_atvv_peer == peer_idx" in control,
          control)
    check("ATVV start/stop and phone takeover clear the active ATVV peer",
          control.count("s_atvv_peer = peer_idx;") >= 1
          and control.count("s_atvv_peer = -1;") >= 1
          and "s_atvv_peer = -1;" in function_body(text, "br_voice_phone_start"))


def test_xiaomi_reacquire_ruling() -> None:
    text = source(MAIN / "br_voice.c")
    watchdog = function_body(text, "br_voice_watchdog")
    press = function_body(text, "br_voice_xiaomi_press")
    check("failed Xiaomi acquisition does not invent or reset physical input",
          "br_voice_input_reset();" not in press
          and "voice_hotkey_acquire_locked(VOICE_OWNER_XIAOMI, peer_idx)" in press,
          press)
    reacquire = watchdog[watchdog.find("if (!down)"):]
    reacquire = reacquire[:reacquire.find("bridge_led_voice_active();")]
    check("watchdog reacquires only while Xiaomi physical input remains active",
          "br_voice_input_peer()" in reacquire
          and "voice_hotkey_acquire_locked(VOICE_OWNER_XIAOMI, peer)" in reacquire,
          reacquire)


def test_atvv_queue_generation() -> None:
    text = source(MAIN / "br_ble_core.c")
    check("peer slots and queued notifications carry a connection generation",
          re.search(r"uint32_t\s+generation\s*;", text) is not None
          and "uint32_t generation;" in text
          and "item.generation = s_peers[idx].generation;" in text)
    alloc = function_body(text, "alloc_peer")
    check("generation is monotonic across slot reuse and unpair/reset",
          "static uint32_t s_next_peer_generation;" in text
          and alloc.count("++s_next_peer_generation;") == 2,
          alloc)
    drain_at = text.find("while (drained < 32 && xQueueReceive(s_auq")
    drain = text[drain_at:text.find("/*", drain_at + 10)] if drain_at >= 0 else ""
    check("stale-generation notifications are discarded before ATVV routing",
          drain_at >= 0
          and "item.generation != s_peers[idx].generation" in text[drain_at:drain_at + 2500]
          and "continue;" in text[drain_at:drain_at + 2500],
          text[drain_at:drain_at + 2500])


def test_msbc_fixture() -> None:
    TEMP.mkdir(exist_ok=True)
    exe = TEMP / "test_msbc.exe"
    command = [
        "gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        "-I", str(MAIN),
        "-I", str(FW / "components" / "xiaomi_msbc" / "include"),
        "-I", str(FW / "components" / "xiaomi_msbc" / "shim"),
        str(MAIN / "br_msbc.c"),
        *sorted(str(p) for p in (FW / "components" / "xiaomi_msbc" / "srce").glob("*.c")),
        str(FW / "test" / "test_msbc.c"),
        "-lm", "-o", str(exe),
    ]
    built = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    check("mSBC fixture decoder compiles with -Werror", built.returncode == 0,
          (built.stdout + built.stderr).strip())
    if built.returncode != 0:
        return
    ran = subprocess.run([str(exe)], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    output = (ran.stdout + ran.stderr).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    check("909 real mSBC frames decode with final 全部通过",
          ran.returncode == 0 and "fixture_frames=909" in output
          and "decode_errors=0" in output
          and bool(lines) and lines[-1] == "全部通过",
          output)


def test_audio_routing() -> None:
    text = source(MAIN / "br_ble_core.c")
    check("XAU is classified by Xiaomi flag plus packet signature",
          'kind = "XAU"' in text and "br_msbc_frame_ok(item.data, item.len)" in text
          and "br_voice_xiaomi_audio(item.data, item.len" in text)
    notify = text.split("case BLE_GAP_EVENT_NOTIFY_RX:", 1)[-1].split(
        "default:", 1)[0]
    check("ATT callback never decodes mSBC",
          "br_msbc_decode" not in notify and "br_voice_xiaomi_audio" not in notify
          and "br_hid_on_notify" in notify)
    voice = source(MAIN / "br_voice.c")
    audio = function_body(voice, "br_voice_xiaomi_audio")
    check("Xiaomi audio requires the first-wins owner and peer",
          "s_hotkey_owner != VOICE_OWNER_XIAOMI" in audio
          and "s_owner_peer != peer_idx" in audio
          and "s_xau_rejected++" in audio, audio)
    check("Xiaomi audio decodes into the existing UAC1 PCM ring",
          "br_msbc_reset()" in audio and "br_msbc_decode(" in audio
          and "xRingbufferSend(s_pcm_rb" in audio, audio)


def test_xiaomi_audio_cleanup() -> None:
    voice = source(MAIN / "br_voice.c")
    end = function_body(voice, "xiaomi_stream_end_locked")
    check("audio end clears streaming and PCM when a session ends",
          all(token in end for token in
              ("s_xiaomi_stream = false;", "s_streaming = false;", "clear_pcm();")),
          end)
    check("release, disconnect, disable and watchdog all close Xiaomi audio",
          "xiaomi_stream_end_locked(\"物理键释放\")" in voice
          and "xiaomi_stream_end_locked(\"断链\")" in voice
          and "xiaomi_stream_end_locked(\"语音通道关闭\")" in voice
          and "xiaomi_stream_end_locked(\"无新帧\")" in voice
          and "xiaomi_stream_end_locked(\"60秒上限\")" in voice)
    wig = function_body(voice, "br_voice_watchdog")
    check("Xiaomi idle timeout keeps the hotkey but stops audio",
          "BR_VOICE_XIAOMI_IDLE_MS" in wig
          and wig.find("BR_VOICE_XIAOMI_IDLE_MS") < wig.find("voice_hotkey_release_owner_locked"),
          wig)
    phone = function_body(voice, "br_voice_phone_start")
    check("phone voice cannot tear down an active Xiaomi owner",
          "s_hotkey_owner == VOICE_OWNER_ATVV" in phone
          and "s_hotkey_owner == VOICE_OWNER_XIAOMI" not in phone, phone)


def test_hid_host_parity() -> None:
    text = source(MAIN / "br_hid.c")
    check("Protocol Mode is discovered by UUID, not a hardcoded handle",
          "UUID_PROTOCOL_MODE 0x2A4E" in text and "c->pm_handle = ci.val_handle" in text
          and not re.search(r"pm_handle\s*=\s*\d+", text))
    write = function_body(text, "pm_write_start")
    check("Xiaomi HID init writes standard Report Protocol 0x01",
          "report_mode[1] = {0x01}" in text and "pm_handle" in write
          and "ble_gattc_write" in write, write)
    check("Xiaomi exits suspend before CCCD subscription",
          "STEP_PROTO" in text and "STEP_CP" in text
          and "cp_pre_start(c);" in text and "post_proto_step" in text)
    check("no vendor A001/A002 activation writes",
          not re.search(r"A001|A002", text, re.IGNORECASE))


def test_peer_disconnect_call() -> None:
    text = source(MAIN / "br_ble_core.c")
    event = text.split("case BLE_GAP_EVENT_DISCONNECT:", 1)[-1].split(
        "case BLE_GAP_EVENT_ENC_CHANGE:", 1)[0]
    idx_at = event.find("find_peer_by_conn")
    lock_at = event.find("xSemaphoreTake(s_voice_process_lock")
    disconnected_at = event.find("s_peers[idx].connected = false;")
    reset_at = event.find("voice_queue_peer_reset_locked((int8_t)idx);")
    call_at = event.find("br_voice_on_disconnect(idx);")
    give_at = event.find("xSemaphoreGive(s_voice_process_lock)")
    check("BLE disconnect passes the disconnected peer index",
          0 <= idx_at < call_at and "br_voice_on_disconnect();" not in event)
    check("disconnect marks stale, purges and resets under the voice process lock",
          0 <= idx_at < lock_at < disconnected_at < reset_at < call_at < give_at,
          f"idx={idx_at} lock={lock_at} disconnected={disconnected_at} "
          f"reset={reset_at} call={call_at} give={give_at}")


def main() -> int:
    print("=== 小米语音 Task 2 · 路由闸门 ===")
    test_host_arbitration()
    test_registration()
    test_hid_routing()
    test_voice_queue()
    test_xiaomi_key_interception()
    test_voice_events()
    test_owner_arbitration()
    test_physical_hold()
    test_disable_cleanup()
    test_atvv_peer_isolation()
    test_xiaomi_reacquire_ruling()
    test_atvv_queue_generation()
    test_msbc_fixture()
    test_audio_routing()
    test_xiaomi_audio_cleanup()
    test_hid_host_parity()
    test_peer_disconnect_call()
    print()
    if FAILURES:
        print(f"✘ {len(FAILURES)} 项未通过：{'、'.join(FAILURES)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
