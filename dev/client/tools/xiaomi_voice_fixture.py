"""Extract real Xiaomi 60-byte mSBC frames from the reference USBPcap file.

Run from the repository root:
    python dev/client/tools/xiaomi_voice_fixture.py

The fixture is the exact ATT notification value, 60 bytes per frame.  No audio
is invented if the pcap does not contain the expected signature.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PCAP = ROOT / "dev" / "client" / "temp" / "vibecoding-xiaomi-voice.pcap"
DEFAULT_OUT = ROOT / "dev" / "firmware" / "bridge" / "test" / "fixtures" / "xiaomi_voice_60b.bin"
H2 = {0x08, 0x38, 0xC8, 0xF8}


def packets(path: Path):
    data = path.read_bytes()
    if data[:4] != b"\xd4\xc3\xb2\xa1":
        raise SystemExit("not a little-endian pcap")
    pos = 24
    while pos + 16 <= len(data):
        _sec, _usec, incl, _orig = struct.unpack_from("<IIII", data, pos)
        pos += 16
        raw = data[pos:pos + incl]
        pos += incl
        if len(raw) < 27:
            continue
        header_len = struct.unpack_from("<H", raw, 0)[0]
        if header_len < 27 or header_len > len(raw):
            continue
        info = raw[16]
        endpoint = raw[21]
        length = struct.unpack_from("<I", raw, 23)[0]
        yield "IN" if info & 1 else "OUT", endpoint, raw[header_len:header_len + length]


def att_pdus(path: Path):
    fragments = {}
    for direction, endpoint, acl in packets(path):
        if len(acl) < 4:
            continue
        flags, length = struct.unpack_from("<HH", acl, 0)
        if 4 + length > len(acl):
            continue
        conn = flags & 0x0FFF
        pb = (flags >> 12) & 3
        body = acl[4:4 + length]
        key = (direction, conn)
        if pb == 1:
            prior = fragments.get(key)
            if prior is None:
                continue
            body = prior + body
            fragments.pop(key, None)
        elif pb != 2:
            continue
        if len(body) < 4:
            fragments[key] = body
            continue
        pdu_len, cid = struct.unpack_from("<HH", body, 0)
        total = 4 + pdu_len
        if len(body) < total:
            fragments[key] = body
            continue
        if cid == 4:
            yield direction, endpoint, body[4:total]
        if len(body) > total:
            fragments[key] = body[total:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcap", type=Path, default=DEFAULT_PCAP)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    frames: list[bytes] = []
    handles = collections.Counter()
    lengths = collections.Counter()
    for _direction, _endpoint, att in att_pdus(args.pcap):
        if len(att) < 3 or att[0] != 0x1B:
            continue
        handle = struct.unpack_from("<H", att, 1)[0]
        value = att[3:]
        lengths[len(value)] += 1
        if (len(value) == 60 and value[0] == 0x01 and value[1] in H2
                and value[2] == 0xAD):
            handles[handle] += 1
            frames.append(value)

    if len(frames) < 50:
        print(f"FAIL: only {len(frames)} valid 60-byte frames")
        return 1
    if any(frame[2] != 0xAD or frame[1] not in H2 for frame in frames):
        print("FAIL: frame signature changed while collecting")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    blob = b"".join(frames)
    args.out.write_bytes(blob)
    manifest = {
        "source": str(args.pcap.relative_to(ROOT)).replace("\\", "/"),
        "record_bytes": 60,
        "frame_count": len(frames),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "h2": dict(sorted(collections.Counter(f[1] for f in frames).items())),
        "handles": {f"0x{k:04x}": v for k, v in sorted(handles.items())},
        "notify_lengths": dict(sorted(lengths.items())),
    }
    args.out.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"PASS: {len(frames)} frames -> {args.out}")
    print(f"sha256: {manifest['sha256']}")
    print(f"handles: {manifest['handles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
