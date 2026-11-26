"""Print every ATT request/write and notification in a USBPcap session.

This is the Windows-vs-ESP32 differential tool: the reference voice pcap starts
after HID setup, so it cannot show what the Windows HID host wrote while
opening the remote.  Capture from before pairing, then run this parser.
"""
from __future__ import annotations

import argparse
import collections
import struct
from pathlib import Path

ATT_NAMES = {
    0x01: "ERROR_RSP", 0x02: "MTU_REQ", 0x03: "MTU_RSP",
    0x04: "FIND_INFO_REQ", 0x05: "FIND_INFO_RSP",
    0x06: "FIND_BY_TYPE_REQ", 0x07: "FIND_BY_TYPE_RSP",
    0x08: "READ_BY_TYPE_REQ", 0x09: "READ_BY_TYPE_RSP",
    0x0A: "READ_REQ", 0x0B: "READ_RSP",
    0x0C: "READ_BLOB_REQ", 0x0D: "READ_BLOB_RSP",
    0x0E: "READ_MULTIPLE_REQ", 0x0F: "READ_MULTIPLE_RSP",
    0x10: "WRITE_REQ", 0x11: "WRITE_RSP",
    0x12: "WRITE_CMD", 0x13: "PREPARE_WRITE_REQ",
    0x14: "PREPARE_WRITE_RSP", 0x15: "EXEC_WRITE_REQ",
    0x16: "EXEC_WRITE_RSP", 0x17: "READ_MULTIPLE_VARIABLE_REQ",
    0x18: "READ_MULTIPLE_VARIABLE_RSP", 0x1B: "NOTIFY",
    0x1D: "INDICATE", 0x1E: "CONFIRM", 0x1F: "READ_BY_GROUP_TYPE_REQ",
    0x20: "READ_BY_GROUP_TYPE_RSP", 0x21: "WRITE_MULTIPLE_REQ",
    0x22: "WRITE_MULTIPLE_RSP",
}
CLIENT_OPS = {0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x12,
              0x13, 0x15, 0x17, 0x1F, 0x21}


def packets(path: Path):
    data = path.read_bytes()
    if len(data) < 24 or data[:4] != b"\xd4\xc3\xb2\xa1":
        raise SystemExit("not a little-endian pcap")
    pos = 24
    while pos + 16 <= len(data):
        sec, usec, incl, _orig = struct.unpack_from("<IIII", data, pos)
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
        yield (sec + usec / 1_000_000, "IN" if info & 1 else "OUT",
               endpoint, raw[header_len:header_len + length])


def att_pdus(path: Path):
    fragments = {}
    for ts, direction, endpoint, acl in packets(path):
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
            prior = fragments.pop(key, None)
            if prior is None:
                continue
            body = prior + body
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
            yield ts, direction, conn, body[4:total]
        if len(body) > total:
            fragments[key] = body[total:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap", type=Path)
    ap.add_argument("--values", action="store_true", help="print full ATT values")
    args = ap.parse_args()

    first = last = None
    opcodes = collections.Counter()
    writes = []
    notifications = collections.Counter()
    for ts, direction, conn, att in att_pdus(args.pcap):
        if not att:
            continue
        first = first or ts
        last = ts
        opcode = att[0]
        opcodes[(direction, opcode)] += 1
        handle = struct.unpack_from("<H", att, 1)[0] if len(att) >= 3 else 0
        value = att[3:]
        if opcode in (0x10, 0x12):
            writes.append((ts, direction, handle, opcode, value))
        elif opcode in (0x1B, 0x1D):
            notifications[(handle, len(value))] += 1

    if first is None:
        print("NO ATT PACKETS")
        return 1

    print(f"duration={last - first:.6f}s")
    print("opcodes:")
    for (direction, opcode), count in sorted(opcodes.items(),
                                             key=lambda x: (x[0][1], x[0][0])):
        print(f"  {direction} 0x{opcode:02X} {ATT_NAMES.get(opcode, '?'):28} {count}")
    print("client requests in order:")
    for ts, direction, conn, att in att_pdus(args.pcap):
        if not att or att[0] not in CLIENT_OPS:
            continue
        opcode = att[0]
        handle = struct.unpack_from("<H", att, 1)[0] if len(att) >= 3 else 0
        value = att[3:]
        shown = value.hex() if args.values or len(value) <= 32 else value[:32].hex() + "..."
        print(f"  {ts - first:8.3f} {direction:3} conn={conn} "
              f"{ATT_NAMES.get(opcode, '?'):20} h=0x{handle:04X} n={len(value)} {shown}")
    print("writes:")
    for ts, direction, handle, opcode, value in writes:
        shown = value.hex() if args.values or len(value) <= 64 else value[:64].hex() + "..."
        print(f"  {ts - first:8.3f} {direction:3} {ATT_NAMES[opcode]:10} "
              f"h=0x{handle:04X} n={len(value)} {shown}")
    print("notifications:")
    for (handle, length), count in sorted(notifications.items()):
        print(f"  h=0x{handle:04X} n={length:3} count={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
