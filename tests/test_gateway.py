"""End to end: real asyncua server and client, fake PLC driver."""
import asyncio
import copy
import socket
import time

import pytest
from asyncua import Client, ua

from kepclone.config import ChannelConfig, Config, DeviceConfig, ServerConfig, hash_password
from kepclone.device import Device
from kepclone.driver import ReadResult, TagDef
from kepclone.opcua_server import OpcUaServer

S = ua.StatusCodes


class FakeDriver:
    def __init__(self):
        self.tags = [
            TagDef("Speed", "Float"),
            TagDef("HMI_Count", "Int32"),
            TagDef("HMI_RO", "Int32", writable=False),
            TagDef("HMI_Bits", "Boolean", 4),
            TagDef("HMI_Rec", None, members=[TagDef("Name", "String"), TagDef("Temp", "Float")]),
            TagDef("Recs", None, 2, [TagDef("Temp", "Float")]),
            TagDef("_Sys", "Int32"),
            TagDef("Bad", "Int32"),
            TagDef("Local:1:I", None, members=[TagDef("Data", "Int32")]),
        ]
        self.values = {
            "Speed": 1.5,
            "HMI_Count": 1,
            "HMI_RO": 2,
            "HMI_Bits": [True, False, True, False],
            "HMI_Rec": {"Name": "abc", "Temp": 20.0},
            "Recs": [{"Temp": 1.0}, {"Temp": 2.0}],
            "_Sys": 0,
            "Local:1:I": {"Data": 9},
        }
        self.down = False
        self.fail_write = False
        self.written = []

    def connect(self):
        if self.down:
            raise ConnectionError("unreachable")

    def close(self):
        pass

    def browse(self):
        return copy.deepcopy(self.tags)

    def read(self, tags):
        if self.down:
            raise ConnectionError("connection lost")
        return {
            t.name: ReadResult(error="tag error") if t.name == "Bad" else ReadResult(copy.deepcopy(self.values[t.name]))
            for t in tags
        }

    def write(self, path, value):
        if self.fail_write:
            raise IOError("plc says no")
        self.written.append((path, value))
        if path.endswith("]"):  # one element of a BOOL array
            top, idx = path[:-1].split("[")
            self.values[top][int(idx)] = value
        elif "." in path:
            top, member = path.split(".")
            if member.isdigit():  # bit of an integer, as the PLC's Read-Modify-Write does it
                self.values[top] = self.values[top] & ~(1 << int(member)) | int(value) << int(member)
            else:
                self.values[top][member] = value
        else:
            self.values[path] = value


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(check, timeout=8.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if await check():
                return
        except Exception:
            pass
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def write_status(node, value, vtype):
    try:
        await node.write_value(ua.DataValue(ua.Variant(value, vtype)))
    except ua.UaStatusCodeError as e:
        return e.code
    return S.Good


def test_gateway_end_to_end(tmp_path):
    asyncio.run(_scenario(tmp_path))


async def _scenario(tmp_path):
    url = f"opc.tcp://127.0.0.1:{free_port()}/kepclone"
    dev_cfg = DeviceConfig("Press1", "fake", scan_ms=50, exclude=["_*"], writable=["HMI_*"])
    cfg = Config(
        ServerConfig(endpoint=url, security=["NoSecurity"], users={"op": hash_password("pw", 1000)}),
        [ChannelConfig("Line1", "logix", [dev_cfg])],
        tmp_path,
    )
    fake = FakeDriver()
    server = OpcUaServer(cfg)
    await server.start()
    device = Device("Line1", dev_cfg, fake)
    await server.add_device(device)
    task = asyncio.create_task(device.run(server))
    try:
        async with Client(url) as anon:
            ns = await anon.get_namespace_index("urn:kepclone")

            def node(c, path):
                return c.get_node(ua.NodeId(f"Line1.Press1.{path}", ns))

            await until(lambda: _eq(node(anon, "Speed"), 1.5))
            assert await node(anon, "HMI_Bits").read_value() == [True, False, True, False]
            assert await node(anon, "HMI_Rec.Name").read_value() == "abc"
            assert await node(anon, "Recs[1].Temp").read_value() == 2.0
            assert await node(anon, "Local:1:I.Data").read_value() == 9
            assert await node(anon, "_Connected").read_value() is True
            bad = await node(anon, "Bad").read_data_value(raise_on_bad_status=False)
            assert bad.StatusCode.value == S.BadDeviceFailure
            excluded = await node(anon, "_Sys").read_data_value(raise_on_bad_status=False)
            assert excluded.StatusCode.value == S.BadNodeIdUnknown

            # poll picks up PLC-side changes
            fake.values["Speed"] = 2.5
            await until(lambda: _eq(node(anon, "Speed"), 2.5))

            # anonymous may read but not write
            assert await write_status(node(anon, "HMI_Count"), 5, ua.VariantType.Int32) == S.BadUserAccessDenied
            assert fake.written == []

        op = Client(url)
        op.set_user("op")
        op.set_password("pw")
        async with op:
            def n(path):
                return op.get_node(ua.NodeId(f"Line1.Press1.{path}", ns))

            assert await write_status(n("HMI_Count"), 5, ua.VariantType.Int32) == S.Good
            assert await n("HMI_Count").read_value() == 5
            assert await write_status(n("HMI_Rec.Temp"), 3.5, ua.VariantType.Float) == S.Good
            assert await write_status(n("HMI_Bits"), [False] * 4, ua.VariantType.Boolean) == S.Good
            assert fake.written == [("HMI_Count", 5), ("HMI_Rec.Temp", 3.5), ("HMI_Bits", [False] * 4)]

            assert await write_status(n("Speed"), 1.0, ua.VariantType.Float) == S.BadNotWritable
            assert await write_status(n("HMI_RO"), 1, ua.VariantType.Int32) == S.BadNotWritable
            assert await write_status(n("HMI_Count"), 1.0, ua.VariantType.Double) == S.BadTypeMismatch
            assert await write_status(n("HMI_Bits"), [True], ua.VariantType.Boolean) == S.BadTypeMismatch
            assert await write_status(n("_Connected"), False, ua.VariantType.Boolean) == S.BadUserAccessDenied
            fake.fail_write = True
            assert await write_status(n("HMI_Count"), 7, ua.VariantType.Int32) == S.BadCommunicationError
            fake.fail_write = False
            assert len(fake.written) == 3

            # desktop window writes: same writable rules, no OPC UA login
            await server.local_write("Line1.Press1", "HMI_Count", 9, "gui")
            assert await n("HMI_Count").read_value() == 9
            with pytest.raises(PermissionError):
                await server.local_write("Line1.Press1", "Speed", 1.0, "gui")
            fake.fail_write = True
            with pytest.raises(IOError):
                await server.local_write("Line1.Press1", "HMI_Count", 10, "gui")
            fake.fail_write = False
            assert fake.written[-1] == ("HMI_Count", 9)

            # desktop bit toggle: writes just the bit; bad bit, non-bool or read-only tag refused
            await server.local_write("Line1.Press1", "HMI_Count.1", True, "gui")
            assert fake.written[-1] == ("HMI_Count.1", True)
            await until(lambda: _eq(n("HMI_Count"), 11))
            await server.local_write("Line1.Press1", "HMI_Bits[1]", True, "gui")
            await until(lambda: _eq(n("HMI_Bits"), [False, True, False, False]))
            bad = [("HMI_Count.32", True), ("HMI_Count.1", 1), ("HMI_RO.0", True), ("Speed.0", True),
                   ("HMI_Bits[4]", True), ("HMI_Bits[1].0", True), ("HMI_Count[0]", True), ("HMI_Bits[1]", 1),
                   ("HMI_Bits[1]", [True]), ("HMI_RO[0]", 1)]
            for path, value in bad:
                with pytest.raises(PermissionError):
                    await server.local_write("Line1.Press1", path, value, "gui")

            # PLC goes away: tags go bad, status nodes say so, writes fail cleanly
            fake.down = True
            await until(lambda: _status(n("Speed"), S.BadNotConnected))
            assert await n("_Connected").read_value() is False
            assert await n("_Error").read_value() == "connection lost"
            assert await write_status(n("HMI_Count"), 8, ua.VariantType.Int32) == S.BadCommunicationError

            # PLC comes back with a changed program: new tag appears, old values resume
            fake.tags.append(TagDef("NewTag", "Int16"))
            fake.values["NewTag"] = 42
            fake.down = False
            await until(lambda: _eq(n("NewTag"), 42))
            assert await n("Speed").read_value() == 2.5
            assert await n("_Connected").read_value() is True

        bad_login = Client(url)
        bad_login.set_user("op")
        bad_login.set_password("wrong")
        with pytest.raises(Exception):
            async with bad_login:
                pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await device.close()
        await server.stop()


async def _eq(node, expected):
    return await node.read_value() == expected


async def _status(node, code):
    dv = await node.read_data_value(raise_on_bad_status=False)
    return dv.StatusCode.value == code


def test_gui_gateway_snapshot(tmp_path, monkeypatch):
    """The desktop window's background gateway: snapshots of devices and tags, clean stop."""
    import kepclone.__main__ as main_mod
    from kepclone.gui import Gateway

    monkeypatch.setattr(main_mod, "make_driver", lambda kind, d: FakeDriver())
    dev_cfg = DeviceConfig("Press1", "fake", scan_ms=50, exclude=["_*"])
    url = f"opc.tcp://127.0.0.1:{free_port()}/kepclone"
    gw = Gateway(Config(ServerConfig(endpoint=url, security=["NoSecurity"]), [ChannelConfig("Line1", "logix", [dev_cfg])], tmp_path))
    try:
        deadline = time.monotonic() + 8
        while True:
            snap = gw.call(gw.overview)
            tags = {t[0]: t for t in gw.call(gw.tags, "Line1.Press1") or []}
            if snap and snap[0]["Line1.Press1"][0] and tags.get("Speed", (0,) * 4)[3] == (1.5, S.Good):
                break
            assert time.monotonic() < deadline, (snap, tags)
            time.sleep(0.05)
        assert snap[1] == []  # no OPC UA clients

        async def clients_while_connected():
            async with Client(url):
                for _ in range(100):
                    if gw.call(gw.overview)[1]:
                        return gw.call(gw.overview)[1]
                    await asyncio.sleep(0.05)

        [(peer, user)] = asyncio.run(clients_while_connected())
        assert peer.startswith("127.0.0.1:") and user == "anonymous"
        assert tags["Recs[1].Temp"][1:3] == ("Float", 0)
        assert tags["Bad"][3] == (None, S.BadDeviceFailure)
        assert tags["Speed"][4] is not None
    finally:
        gw.stop()
    assert not gw.running and gw.error is None
    assert gw.call(gw.overview) is None
