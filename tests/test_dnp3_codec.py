import struct

import pytest

from kepclone.drivers import dnp3_codec as c
from kepclone.drivers.dnp3_codec import Point


def test_crc16_dnp_check_value():
    assert c.crc16(b"123456789") == 0xEA82  # CRC-16/DNP catalogue check value


def test_link_frame_known_vector():
    # RESET_LINK_STATES from master 1024 to outstation 1, a widely published example frame
    assert c.link_frame(0xC0, 1, 1024) == bytes.fromhex("056405c001000004e921")


def test_link_frame_roundtrip_multiblock():
    data = bytes(range(40))  # 3 blocks: 16 + 16 + 8
    frame = c.link_frame(c.LINK_UNCONFIRMED_DATA, 10, 1, data)
    h = c.parse_link_header(frame[:10])
    assert (h.ctrl, h.dest, h.src, h.data_len, h.body_len) == (0xC4, 10, 1, 40, 46)
    assert c.parse_link_body(frame[10:], h.data_len) == data
    corrupted = frame[:20] + bytes([frame[20] ^ 1]) + frame[21:]
    with pytest.raises(c.ProtocolError):
        c.parse_link_body(corrupted[10:], h.data_len)
    with pytest.raises(c.ProtocolError):
        c.parse_link_header(b"\x05\x65" + frame[2:10])


def test_transport_segment_and_reassemble():
    apdu = bytes(range(256)) * 2  # 512 bytes -> 3 segments
    segs, nxt = c.segment(apdu, 62)
    assert [s[0] for s in segs] == [c.T_FIR | 62, 63, c.T_FIN | 0] and nxt == 1
    r = c.Reassembler()
    assert r.add(segs[0]) is None and r.add(segs[1]) is None
    assert r.add(segs[2]) == apdu
    # out-of-sequence segment drops the partial APDU
    r.add(segs[0])
    assert r.add(segs[2]) is None and r.add(segs[1]) is None


def test_application_headers():
    assert c.request(17, c.FC_READ, c.CLASS_1230) == bytes.fromhex("c101") + bytes.fromhex("3c02063c03063c04063c0106")
    assert c.confirm(0xE3, unsolicited=False) == bytes([0xC3, 0x00])
    assert c.confirm(0xF5, unsolicited=True) == bytes([0xD5, 0x00])


ONLINE = 0x01


@pytest.mark.parametrize(
    "objects, expected",
    [
        # g1v2 start-stop 8-bit, flags: online+state, online, offline+state
        (bytes([1, 2, 0x00, 3, 5, 0x81, 0x01, 0x80]),
         [Point("BI", 3, True, True), Point("BI", 4, False, True), Point("BI", 5, True, False)]),
        # g1v1 packed, 16-bit start-stop, indexes 0..9
        (bytes([1, 1, 0x01, 0, 0, 9, 0, 0b0000_0101, 0b10]),
         [Point("BI", i, v, True) for i, v in enumerate([1, 0, 1, 0, 0, 0, 0, 0, 0, 1])]),
        # g3v1 packed double-bit: 0b10 (on), 0b01 (off)
        (bytes([3, 1, 0x00, 0, 1, 0b0110]), [Point("DBI", 0, 2, True), Point("DBI", 1, 1, True)]),
        # g10v2 binary output status
        (bytes([10, 2, 0x00, 0, 0, 0x81]), [Point("BO", 0, True, True)]),
        # g20v1 counter, g20v6 16-bit counter without flags
        (bytes([20, 1, 0x00, 0, 0, ONLINE]) + struct.pack("<I", 70000) + bytes([20, 6, 0x00, 1, 1]) + struct.pack("<H", 9),
         [Point("CTR", 0, 70000, True), Point("CTR", 1, 9, True)]),
        # g21v5 frozen counter with time
        (bytes([21, 5, 0x00, 2, 2, ONLINE]) + struct.pack("<I", 5) + bytes(6), [Point("FCTR", 2, 5, True)]),
        # g30v1 int32, g30v4 int16 no flags, g30v5 float, g30v6 double
        (bytes([30, 1, 0x00, 0, 0, ONLINE]) + struct.pack("<i", -5)
         + bytes([30, 4, 0x00, 1, 1]) + struct.pack("<h", 300)
         + bytes([30, 5, 0x00, 2, 2, ONLINE]) + struct.pack("<f", 1.5)
         + bytes([30, 6, 0x00, 3, 3, 0x00]) + struct.pack("<d", 2.25),
         [Point("AI", 0, -5.0, True), Point("AI", 1, 300.0, True), Point("AI", 2, 1.5, True), Point("AI", 3, 2.25, False)]),
        # g40v3 analog output status float
        (bytes([40, 3, 0x00, 0, 0, ONLINE]) + struct.pack("<f", 4.0), [Point("AO", 0, 4.0, True)]),
        # events: g2v2 with time (8-bit count + 8-bit index prefix), g32v7 float with time (16-bit prefix)
        (bytes([2, 2, 0x17, 1, 7, 0x81]) + bytes(6)
         + bytes([32, 7, 0x28, 1, 0, 0x2C, 0x01, ONLINE]) + struct.pack("<f", 9.5) + bytes(6),
         [Point("BI", 7, True, True), Point("AI", 300, 9.5, True)]),
        # g2v3 relative-time event after a g51v1 CTO, g22v1 counter event, g42v6 double event
        (bytes([51, 1, 0x07, 1]) + bytes(6) + bytes([2, 3, 0x17, 1, 4, 0x01, 0, 0])
         + bytes([22, 1, 0x17, 1, 3, ONLINE]) + struct.pack("<I", 12)
         + bytes([42, 6, 0x17, 1, 1, ONLINE]) + struct.pack("<d", -1.0),
         [Point("BI", 4, False, True), Point("CTR", 3, 12, True), Point("AO", 1, -1.0, True)]),
        # g80v1 IIN bits and g110v4 octet string are skipped; g4v1 double-bit event flags 0x41 -> 1
        (bytes([80, 1, 0x00, 7, 7, 0x01, 110, 4, 0x00, 0, 0]) + b"abcd" + bytes([4, 1, 0x17, 1, 2, 0x41]),
         [Point("DBI", 2, 1, True)]),
    ],
)
def test_parse_objects(objects, expected):
    assert c.parse_objects(objects) == (expected, None)


def test_parse_objects_errors_keep_earlier_points():
    good = bytes([30, 3, 0x00, 0, 0]) + struct.pack("<i", 1)
    points, err = c.parse_objects(good + bytes([99, 1, 0x00, 0, 0, 0]))
    assert points == [Point("AI", 0, 1.0, True)] and err == "unsupported object g99v1"
    points, err = c.parse_objects(bytes([30, 1, 0x00, 0, 1, ONLINE, 0, 0]))  # truncated
    assert points == [] and err == "truncated object data"
    points, err = c.parse_objects(bytes([30, 1, 0x5B, 0]))
    assert err == "g30v1: unsupported qualifier 0x5b"
