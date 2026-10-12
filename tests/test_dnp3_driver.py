"""Dnp3Driver against a scripted fake outstation on a local TCP socket."""
import asyncio
import socket
import struct
import threading

import pytest
from asyncua import Client, ua

from kepclone.config import ChannelConfig, Config, DeviceConfig, ServerConfig, hash_password
from kepclone.device import Device
from kepclone.driver import TagDef
from kepclone.drivers import dnp3, dnp3_codec as c
from kepclone.drivers import make_driver
from kepclone.opcua_server import OpcUaServer
from tests.test_gateway import free_port, until, write_status

OUTSTATION, MASTER = 10, 1


class FakeOutstation(threading.Thread):
    """Answers DISABLE_UNSOLICITED and class reads like an RTU, with protocol detours:
    a link-status keep-alive, an unsolicited report, and a two-fragment response."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.ai = [i * 1.5 for i in range(100)]
        self.counter = 7
        self.silent = False
        self.log = []
        self._tseq = 0
        self._useq = 0
        self._reasm = c.Reassembler()

    def run(self):
        self.conn, _ = self.srv.accept()
        try:
            while True:
                apdu = self.recv_apdu()
                seq, fc = apdu[0] & 0x0F, apdu[1]
                self.log.append(("request", fc, apdu[2:]))
                if self.silent:
                    continue
                if fc == c.FC_DISABLE_UNSOLICITED:
                    self.send_apdu(bytes([0xC0 | seq, 0x81, 0x80, 0x01]))  # IIN: restart, func not supported
                elif fc == c.FC_READ:
                    self.answer_read(seq)
        except (ConnectionError, OSError):
            pass

    def answer_read(self, seq):
        self.send_link(0x49)  # REQUEST_LINK_STATUS: master must answer LINK_STATUS
        useq = self._useq
        self._useq = (useq + 1) % 16
        self.send_apdu(bytes([0xF0 | useq, 0x82, 0, 0]) + bytes([30, 5, 0x00, 0, 0, 1]) + struct.pack("<f", -99))
        self.expect_confirm(useq, unsolicited=True)

        event = bytes([32, 5, 0x17, 1, 0, 1]) + struct.pack("<f", 555.0)  # stale event for AI.0
        statics = bytes([30, 5, 0x01, 0, 0, 99, 0]) + b"".join(struct.pack("<Bf", 1, v) for v in self.ai)
        self.send_apdu(bytes([c.A_FIR | c.A_CON | seq, 0x81, 0, 0]) + event + statics)  # > 249 bytes: 3 segments
        self.expect_confirm(seq, unsolicited=False)

        seq2 = (seq + 1) % 16
        rest = (
            bytes([1, 2, 0x00, 0, 1, 0x81, 0x80])  # BI.0 on, BI.1 offline
            + bytes([20, 1, 0x00, 0, 0, 1]) + struct.pack("<I", self.counter)
        )
        self.send_apdu(bytes([c.A_FIN | c.A_CON | seq2, 0x81, 0, 0]) + rest)
        self.expect_confirm(seq2, unsolicited=False)

    def expect_confirm(self, seq, unsolicited):
        apdu = self.recv_apdu()
        expected = c.confirm(seq, unsolicited)
        self.log.append(("confirm", apdu == expected))

    def send_apdu(self, apdu):
        segs, self._tseq = c.segment(apdu, self._tseq)
        for s in segs:
            self.conn.sendall(c.link_frame(0x44, MASTER, OUTSTATION, s))

    def send_link(self, ctrl):
        self.conn.sendall(c.link_frame(ctrl, MASTER, OUTSTATION))

    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def recv_apdu(self):
        while True:
            h = c.parse_link_header(self.recv_exact(10))
            data = c.parse_link_body(self.recv_exact(h.body_len), h.data_len)
            assert (h.dest, h.src) == (OUTSTATION, MASTER)
            if h.ctrl == c.LINK_STATUS:
                self.log.append(("link_status",))
                continue
            assert h.ctrl == c.LINK_UNCONFIRMED_DATA
            apdu = self._reasm.add(data)
            if apdu is not None:
                return apdu


@pytest.fixture
def rtu(monkeypatch):
    monkeypatch.setattr(dnp3, "TIMEOUT_S", 1.0)
    fake = FakeOutstation()
    fake.start()
    yield fake
    fake.srv.close()


def test_connect_browse_read(rtu):
    d = dnp3.Dnp3Driver("127.0.0.1", rtu.port, OUTSTATION, MASTER, {"TankLevel": "AI.5", "Missing": "AI.999"})
    d.connect()
    try:
        tags = d.browse()
        by_name = {t.name: t for t in tags}
        assert [t.name for t in tags] == ["TankLevel", "Missing", "BI", "CTR", "AI"]
        assert by_name["TankLevel"] == TagDef("TankLevel", "Double", writable=False)
        assert [m.name for m in by_name["BI"].members] == ["0", "1"]
        assert len(by_name["AI"].members) == 99 and "5" not in {m.name for m in by_name["AI"].members}
        assert all(not t.writable for t in tags)

        res = d.read(tags)
        assert res["TankLevel"].value == 7.5
        assert res["Missing"].value is None  # not reported by the RTU -> Bad
        assert res["BI"].value == {"0": True, "1": None}  # BI.1 offline -> Bad
        assert res["AI"].value["0"] == 0.0  # static after the stale event wins
        assert res["CTR"].value == {"0": 7}

        rtu.ai[5], rtu.counter = 42.0, 8
        res = d.read(tags)
        assert res["TankLevel"].value == 42.0 and res["CTR"].value == {"0": 8}

        with pytest.raises(IOError, match="read-only"):
            d.write("TankLevel", 1.0)
    finally:
        d.close()

    # The driver returns right after sending the final confirm; wait for the fake to log it (it exits on EOF).
    rtu.join(timeout=2)
    assert not rtu.is_alive()
    requests =[e for e in rtu.log if e[0] == "request"]
    assert requests[0] == ("request", c.FC_DISABLE_UNSOLICITED, c.CLASS_123)
    assert requests[1:] == [("request", c.FC_READ, c.CLASS_1230)] * 3
    assert rtu.log.count(("link_status",)) == 3
    assert [e for e in rtu.log if e[0] == "confirm"] == [("confirm", True)] * 9


def test_timeout(rtu):
    d = dnp3.Dnp3Driver("127.0.0.1", rtu.port, OUTSTATION, MASTER)
    d.connect()
    rtu.silent = True
    with pytest.raises(TimeoutError):
        d.read(d.browse())
    d.close()


def test_through_opcua(rtu, tmp_path):
    asyncio.run(_opcua_scenario(rtu, tmp_path))


async def _opcua_scenario(rtu, tmp_path):
    url = f"opc.tcp://127.0.0.1:{free_port()}/kepclone"
    dev_cfg = DeviceConfig(
        "Pump3", f"127.0.0.1:{rtu.port}", scan_ms=100, writable=["*"],
        outstation=OUTSTATION, master=MASTER, names={"TankLevel": "AI.5"},
    )
    cfg = Config(
        ServerConfig(endpoint=url, security=["NoSecurity"], users={"op": hash_password("pw", 1000)}),
        [ChannelConfig("RTUs", "dnp3", [dev_cfg])],
        tmp_path,
    )
    server = OpcUaServer(cfg)
    await server.start()
    device = Device("RTUs", dev_cfg, make_driver("dnp3", dev_cfg))
    await server.add_device(device)
    task = asyncio.create_task(device.run(server))
    try:
        client = Client(url)
        client.set_user("op")
        client.set_password("pw")
        async with client:
            ns = await client.get_namespace_index("urn:kepclone")

            def n(path):
                return client.get_node(ua.NodeId(f"RTUs.Pump3.{path}", ns))

            await until(lambda: _eq(n("TankLevel"), 7.5))
            assert await n("AI.7").read_value() == 10.5
            assert await n("BI.0").read_value() is True
            assert await n("CTR.0").read_value() == 7
            dv = await n("BI.1").read_data_value(raise_on_bad_status=False)
            assert dv.StatusCode.value == ua.StatusCodes.BadDeviceFailure
            # read-only even when the allow-list matches
            assert await write_status(n("TankLevel"), 1.0, ua.VariantType.Double) == ua.StatusCodes.BadNotWritable
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await device.close()
        await server.stop()


async def _eq(node, expected):
    return await node.read_value() == expected
