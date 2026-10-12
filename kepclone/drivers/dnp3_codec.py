"""DNP3 (IEEE 1815) master-side encoding and decoding. Pure functions, no I/O."""
from __future__ import annotations

import struct
from typing import NamedTuple


class ProtocolError(Exception):
    pass


# ---- link layer --------------------------------------------------------------------

def crc16(data: bytes) -> int:
    """CRC-16/DNP: poly 0x3D65 reflected (0xA6BC), init 0, final xor 0xFFFF."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
    return ~crc & 0xFFFF


def _with_crc(block: bytes) -> bytes:
    return block + crc16(block).to_bytes(2, "little")


# link control: DIR (from master) 0x80, PRM (primary) 0x40, function code in the low nibble
LINK_FROM_MASTER, LINK_PRIMARY = 0x80, 0x40
LINK_UNCONFIRMED_DATA = LINK_FROM_MASTER | LINK_PRIMARY | 0x04
LINK_ACK = LINK_FROM_MASTER | 0x00
LINK_STATUS = LINK_FROM_MASTER | 0x0B
HEADER_LEN = 10


def link_frame(ctrl: int, dest: int, src: int, data: bytes = b"") -> bytes:
    if len(data) > 250:
        raise ValueError("link user data is limited to 250 bytes")
    header = bytes([0x05, 0x64, 5 + len(data), ctrl]) + dest.to_bytes(2, "little") + src.to_bytes(2, "little")
    return _with_crc(header) + b"".join(_with_crc(data[i : i + 16]) for i in range(0, len(data), 16))


class LinkHeader(NamedTuple):
    ctrl: int
    dest: int
    src: int
    data_len: int  # user data bytes, excluding block CRCs
    body_len: int  # bytes still to read after the header, including block CRCs


def parse_link_header(h: bytes) -> LinkHeader:
    if h[:2] != b"\x05\x64":
        raise ProtocolError(f"bad link start bytes {h[:2].hex()}")
    if crc16(h[:8]) != int.from_bytes(h[8:10], "little"):
        raise ProtocolError("link header CRC mismatch")
    n = h[2] - 5
    if n < 0:
        raise ProtocolError(f"bad link length {h[2]}")
    return LinkHeader(h[3], int.from_bytes(h[4:6], "little"), int.from_bytes(h[6:8], "little"), n, n + 2 * -(-n // 16))


def parse_link_body(body: bytes, data_len: int) -> bytes:
    data, pos = b"", 0
    while len(data) < data_len:
        size = min(16, data_len - len(data))
        block, check = body[pos : pos + size], body[pos + size : pos + size + 2]
        if len(check) != 2 or crc16(block) != int.from_bytes(check, "little"):
            raise ProtocolError("link data block CRC mismatch")
        data += block
        pos += size + 2
    return data


# ---- transport layer ---------------------------------------------------------------

T_FIN, T_FIR = 0x80, 0x40
MAX_SEGMENT = 249


def segment(apdu: bytes, seq: int) -> tuple[list[bytes], int]:
    """Split an APDU into transport segments. Returns (segments, next sequence number)."""
    chunks = [apdu[i : i + MAX_SEGMENT] for i in range(0, len(apdu), MAX_SEGMENT)] or [b""]
    out = []
    for i, chunk in enumerate(chunks):
        header = (T_FIR if i == 0 else 0) | (T_FIN if i == len(chunks) - 1 else 0) | seq
        out.append(bytes([header]) + chunk)
        seq = (seq + 1) % 64
    return out, seq


class Reassembler:
    def __init__(self):
        self._buf: bytes | None = None
        self._next = 0

    def add(self, seg: bytes) -> bytes | None:
        """Feed one transport segment; returns a complete APDU on FIN."""
        if not seg:
            return None
        header, payload = seg[0], seg[1:]
        seq = header & 0x3F
        if header & T_FIR:
            self._buf = payload
        elif self._buf is not None and seq == self._next:
            self._buf += payload
        else:  # out of sequence or no FIR seen: drop the partial APDU
            self._buf = None
            return None
        self._next = (seq + 1) % 64
        if header & T_FIN:
            apdu, self._buf = self._buf, None
            return apdu
        return None


# ---- application layer -------------------------------------------------------------

A_FIR, A_FIN, A_CON, A_UNS = 0x80, 0x40, 0x20, 0x10
FC_CONFIRM, FC_READ, FC_DISABLE_UNSOLICITED = 0x00, 0x01, 0x15
FC_RESPONSE, FC_UNSOLICITED_RESPONSE = 0x81, 0x82

CLASS_123 = bytes([60, 2, 0x06, 60, 3, 0x06, 60, 4, 0x06])
CLASS_1230 = CLASS_123 + bytes([60, 1, 0x06])


def request(seq: int, fc: int, objects: bytes = b"") -> bytes:
    return bytes([A_FIR | A_FIN | (seq & 0x0F), fc]) + objects


def confirm(seq: int, unsolicited: bool) -> bytes:
    return bytes([A_FIR | A_FIN | (A_UNS if unsolicited else 0) | (seq & 0x0F), FC_CONFIRM])


class Point(NamedTuple):
    type: str  # BI, DBI, BO, CTR, FCTR, AI, AO
    index: int
    value: bool | int | float
    online: bool


# Layout letters: F flags byte, I/H u32/u16, i/h i32/i16, f/d float32/64, T 48-bit time,
# t 16-bit relative time. Event groups map to the same point type as their static group.
_STRUCT = {"F": "B", "I": "I", "H": "H", "i": "i", "h": "h", "f": "f", "d": "d", "T": "6s", "t": "2s"}
_LAYOUTS = {
    (1, 2): ("BI", "F"), (2, 1): ("BI", "F"), (2, 2): ("BI", "FT"), (2, 3): ("BI", "Ft"),
    (3, 2): ("DBI", "F"), (4, 1): ("DBI", "F"), (4, 2): ("DBI", "FT"), (4, 3): ("DBI", "Ft"),
    (10, 2): ("BO", "F"), (11, 1): ("BO", "F"), (11, 2): ("BO", "FT"),
    (20, 1): ("CTR", "FI"), (20, 2): ("CTR", "FH"), (20, 3): ("CTR", "FI"), (20, 4): ("CTR", "FH"),
    (20, 5): ("CTR", "I"), (20, 6): ("CTR", "H"), (20, 7): ("CTR", "I"), (20, 8): ("CTR", "H"),
    (21, 1): ("FCTR", "FI"), (21, 2): ("FCTR", "FH"), (21, 3): ("FCTR", "FI"), (21, 4): ("FCTR", "FH"),
    (21, 5): ("FCTR", "FIT"), (21, 6): ("FCTR", "FHT"), (21, 7): ("FCTR", "FIT"), (21, 8): ("FCTR", "FHT"),
    (21, 9): ("FCTR", "I"), (21, 10): ("FCTR", "H"), (21, 11): ("FCTR", "I"), (21, 12): ("FCTR", "H"),
    (30, 1): ("AI", "Fi"), (30, 2): ("AI", "Fh"), (30, 3): ("AI", "i"), (30, 4): ("AI", "h"),
    (30, 5): ("AI", "Ff"), (30, 6): ("AI", "Fd"),
    (40, 1): ("AO", "Fi"), (40, 2): ("AO", "Fh"), (40, 3): ("AO", "Ff"), (40, 4): ("AO", "Fd"),
    (50, 1): (None, "T"), (50, 3): (None, "T"), (51, 1): (None, "T"), (51, 2): (None, "T"),
    (52, 1): (None, "H"), (52, 2): (None, "H"),
}
for _g, _t in ((22, "CTR"), (23, "FCTR")):  # counter / frozen counter events
    for _v, _l in enumerate(("FI", "FH", "FI", "FH", "FIT", "FHT", "FIT", "FHT"), 1):
        _LAYOUTS[(_g, _v)] = (_t, _l)
for _g, _t in ((32, "AI"), (42, "AO")):  # analog input / output events
    for _v, _l in enumerate(("Fi", "Fh", "FiT", "FhT", "Ff", "Fd", "FfT", "FdT"), 1):
        _LAYOUTS[(_g, _v)] = (_t, _l)
LAYOUTS = {k: (t, layout, struct.Struct("<" + "".join(_STRUCT[c] for c in layout))) for k, (t, layout) in _LAYOUTS.items()}
PACKED = {(1, 1): ("BI", 1), (10, 1): ("BO", 1), (3, 1): ("DBI", 2), (80, 1): (None, 1)}

_START_STOP = {0: 1, 1: 2, 2: 4}  # range code -> width of start and stop
_COUNT = {7: 1, 8: 2, 9: 4}  # range code -> width of count
_PREFIX = {0: 0, 1: 1, 2: 2, 3: 4}  # prefix code -> width of index prefix


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ProtocolError("truncated object data")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "little")


def _point(ptype: str, index: int, layout: str, fields: tuple) -> Point:
    flags = fields[0] if layout[0] == "F" else None
    online = flags is None or bool(flags & 0x01)
    if ptype in ("BI", "BO"):
        value = bool(flags & 0x80)
    elif ptype == "DBI":
        value = (flags >> 6) & 0x03
    else:
        value = fields[1] if flags is not None else fields[0]
        if ptype in ("AI", "AO"):
            value = float(value)
    return Point(ptype, index, value, online)


def parse_objects(data: bytes) -> tuple[list[Point], str | None]:
    """Parse response object headers + data. Returns (points, error).

    On an unsupported object or qualifier, parsing stops (object sizes are unknown past
    it) and the points read so far are returned with an error message.
    """
    r, points = _Reader(data), []
    try:
        while r.pos < len(data):
            g, v, q = r.take(3)
            prefix, rng = (q >> 4) & 0x07, q & 0x0F
            indices = None
            if rng in _START_STOP and prefix == 0:
                start, stop = r.uint(_START_STOP[rng]), r.uint(_START_STOP[rng])
                if stop < start:
                    return points, f"g{g}v{v}: stop {stop} < start {start}"
                indices = range(start, stop + 1)
                count = len(indices)
            elif rng in _COUNT and prefix in _PREFIX:
                count = r.uint(_COUNT[rng])
            else:
                return points, f"g{g}v{v}: unsupported qualifier 0x{q:02x}"

            if (g, v) in PACKED:
                ptype, bits = PACKED[(g, v)]
                if prefix:
                    return points, f"g{g}v{v}: packed object with index prefix"
                raw = r.take((count * bits + 7) // 8)
                for k in range(count):
                    bit = k * bits
                    val = (raw[bit // 8] >> (bit % 8)) & ((1 << bits) - 1)
                    if ptype:
                        points.append(Point(ptype, indices[k] if indices is not None else k, bool(val) if bits == 1 else val, True))
                continue

            if (g, v) in LAYOUTS:
                ptype, layout, fmt = LAYOUTS[(g, v)]
            elif g in (110, 111) and v:  # octet strings: variation is the length; skipped
                ptype, layout, fmt = None, "", struct.Struct(f"{v}s")
            else:
                return points, f"unsupported object g{g}v{v}"
            width = _PREFIX[prefix]
            for k in range(count):
                index = r.uint(width) if width else (indices[k] if indices is not None else k)
                fields = fmt.unpack(r.take(fmt.size))
                if ptype:
                    points.append(_point(ptype, index, layout, fields))
    except ProtocolError as e:
        return points, str(e)
    return points, None
