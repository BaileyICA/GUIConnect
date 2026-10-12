"""DNP3 master over TCP for RTU outstations (e.g. Brodersen RTU32). Read-only, polled."""
from __future__ import annotations

import logging
import socket
import time

from ..driver import ReadResult, TagDef
from . import dnp3_codec as c

log = logging.getLogger(__name__)

DEFAULT_PORT = 20000
TIMEOUT_S = 5.0
TYPES = {"BI": "Boolean", "DBI": "Byte", "BO": "Boolean", "CTR": "UInt32", "FCTR": "UInt32", "AI": "Double", "AO": "Double"}


def parse_point_ref(ref: str) -> tuple[str, int] | None:
    """'AI.5' -> ('AI', 5); None if malformed."""
    ptype, _, index = ref.partition(".")
    return (ptype, int(index)) if ptype in TYPES and index.isdigit() else None


class Dnp3Driver:
    def __init__(self, host: str, port: int, outstation: int, master: int = 1, names: dict[str, str] | None = None):
        self._addr = (host, port)
        self._outstation = outstation
        self._master = master
        self._names = {parse_point_ref(ref): name for name, ref in (names or {}).items()}  # (type, index) -> name
        self._sock: socket.socket | None = None
        self._points: dict[tuple[str, int], c.Point] = {}

    # ---- Driver interface ------------------------------------------------------------

    def connect(self) -> None:
        self._sock = socket.create_connection(self._addr, timeout=TIMEOUT_S)
        self._app_seq = 0
        self._transport_seq = 0
        self._reassembler = c.Reassembler()
        self._points = {}
        # We poll everything each scan; unsolicited reports would only need confirming.
        self._request(c.FC_DISABLE_UNSOLICITED, c.CLASS_123)
        self._poll()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def browse(self) -> list[TagDef]:
        tags = [TagDef(name, TYPES[ref[0]], writable=False) for ref, name in self._names.items()]
        for ptype in TYPES:
            members = [
                TagDef(str(index), TYPES[ptype], writable=False)
                for (t, index) in sorted(self._points)
                if t == ptype and (t, index) not in self._names
            ]
            if members:
                tags.append(TagDef(ptype, None, members=members, writable=False))
        return tags

    def read(self, tags: list[TagDef]) -> dict[str, ReadResult]:
        self._poll()
        by_name = {name: ref for ref, name in self._names.items()}
        out = {}
        for t in tags:
            if t.type is None:  # type folder: members are point indexes
                out[t.name] = ReadResult({m.name: self._value((t.name, int(m.name))) for m in t.members})
            else:
                out[t.name] = ReadResult(self._value(by_name[t.name]))
        return out

    def write(self, path: str, value) -> None:
        raise IOError("DNP3 points are read-only")

    # ---- protocol --------------------------------------------------------------------

    def _value(self, ref):
        p = self._points.get(ref)
        return p.value if p is not None and p.online else None  # None -> Bad quality

    def _poll(self) -> None:
        # Events first, static (class 0) last: the last update per point is its current value.
        for p in self._request(c.FC_READ, c.CLASS_1230):
            self._points[(p.type, p.index)] = p

    def _request(self, fc: int, objects: bytes) -> list[c.Point]:
        seq = self._app_seq
        self._app_seq = (seq + 1) % 16
        self._send_apdu(c.request(seq, fc, objects))
        points: list[c.Point] = []
        while True:  # one iteration per response fragment
            apdu = self._recv_apdu(time.monotonic() + TIMEOUT_S)
            if len(apdu) < 4:
                raise c.ProtocolError(f"short application response {apdu.hex()}")
            ac, rfc = apdu[0], apdu[1]
            if ac & c.A_CON:
                self._send_apdu(c.confirm(ac, unsolicited=rfc == c.FC_UNSOLICITED_RESPONSE))
            if rfc != c.FC_RESPONSE:
                continue  # unsolicited report: confirmed above, data ignored
            iin1, iin2 = apdu[2], apdu[3]
            if iin2 & 0x06 and fc == c.FC_READ:  # object unknown / parameter error
                log.warning("RTU %s: IIN2=0x%02x on read (object unknown or parameter error)", self._addr, iin2)
            parsed, error = c.parse_objects(apdu[4:])
            if error:
                log.warning("RTU %s: %s; %d points parsed from fragment", self._addr, error, len(parsed))
            points += parsed
            if ac & c.A_FIN:
                return points

    def _send_apdu(self, apdu: bytes) -> None:
        segments, self._transport_seq = c.segment(apdu, self._transport_seq)
        for seg in segments:
            self._sock.sendall(c.link_frame(c.LINK_UNCONFIRMED_DATA, self._outstation, self._master, seg))

    def _recv_apdu(self, deadline: float) -> bytes:
        while True:
            h = c.parse_link_header(self._recv(c.HEADER_LEN, deadline))
            data = c.parse_link_body(self._recv(h.body_len, deadline), h.data_len)
            if h.dest != self._master or h.src != self._outstation:
                continue
            if not h.ctrl & c.LINK_PRIMARY:
                continue  # secondary frames (ACK etc.) need no action
            fc = h.ctrl & 0x0F
            if fc == 9:  # REQUEST_LINK_STATUS (keep-alive)
                self._send_link(c.LINK_STATUS)
            elif fc in (0, 2):  # RESET_LINK_STATES, TEST_LINK
                self._send_link(c.LINK_ACK)
            elif fc in (3, 4):  # CONFIRMED / UNCONFIRMED_USER_DATA
                if fc == 3:
                    self._send_link(c.LINK_ACK)
                apdu = self._reassembler.add(data)
                if apdu is not None:
                    return apdu

    def _send_link(self, ctrl: int) -> None:
        self._sock.sendall(c.link_frame(ctrl, self._outstation, self._master))

    def _recv(self, n: int, deadline: float) -> bytes:
        buf = b""
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no response from RTU {self._addr[0]}:{self._addr[1]} within {TIMEOUT_S:g} s")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("RTU closed the connection")
            buf += chunk
        return buf
