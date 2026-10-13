from pathlib import Path

import pytest

from kepclone import config
from kepclone.drivers import make_driver

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_example_config_loads():
    cfg = config.load(ROOT / "config.example.yaml")
    dev = cfg.channels[0].devices[0]
    assert (cfg.channels[0].name, dev.name, dev.path) == ("Line1", "Press1", "192.168.1.10/0")
    assert cfg.server.users == {}
    assert cfg.server.anonymous_write is False
    rtu = cfg.channels[1].devices[0]
    assert (cfg.channels[1].driver, rtu.outstation, rtu.master, rtu.names["TankLevel"]) == ("dnp3", 10, 1, "AI.5")
    driver = make_driver("dnp3", rtu)
    assert driver._addr == ("10.0.5.20", 20000)


@pytest.mark.parametrize(
    "body, msg",
    [
        ("channels: [{name: L, driver: logix, devices: [{name: P, path: x, writeable: ['*']}]}]", "unknown key"),
        ("channels: [{name: L, driver: modbus, devices: []}]", "unknown driver"),
        ("channels: [{name: L.1, driver: logix, devices: []}]", "without '.'"),
        ("channels: [{name: L, driver: logix, devices: [{name: P}]}]", "path"),
        ("channels: [{name: L, driver: logix, devices: [{name: P, path: x, scan_ms: 10}]}]", "scan_ms"),
        ("channels: [{name: L, driver: logix, devices: [{name: P, path: x, writable: 'HMI_*'}]}]", "list of strings"),
        ("server: {anonymous_write: 'false'}\nchannels: [{name: L, driver: logix, devices: []}]", "true or false"),
        ("server: {security: [Basic128]}\nchannels: [{name: L, driver: logix, devices: []}]", "unknown policy"),
        ("server: {users: {op: hunter2}}\nchannels: [{name: L, driver: logix, devices: []}]", "not a hash"),
        (
            "channels: [{name: L, driver: logix, devices: [{name: P, path: x}, {name: p, path: y}]}]",
            "duplicate",
        ),
        ("server: {}", "no channels"),
        ("channels: [{name: R, driver: dnp3, devices: [{name: P, path: 'h:20000'}]}]", "outstation"),
        ("channels: [{name: R, driver: dnp3, devices: [{name: P, path: 'h:x', outstation: 2}]}]", "host:port"),
        ("channels: [{name: R, driver: dnp3, devices: [{name: P, path: h, outstation: 1}]}]", "must differ"),
        ("channels: [{name: R, driver: dnp3, devices: [{name: P, path: h, outstation: 2, names: {T: XX.1}}]}]", "TYPE"),
        ("channels: [{name: R, driver: dnp3, devices: [{name: P, path: h, outstation: 2, names: {AI: AI.1}}]}]", "not one of"),
        (
            "channels: [{name: R, driver: dnp3, devices: [{name: P, path: h, outstation: 2, names: {A: AI.1, B: AI.1}}]}]",
            "both map",
        ),
    ],
)
def test_invalid_config(tmp_path, body, msg):
    with pytest.raises(config.ConfigError, match=msg):
        config.load(write(tmp_path, body))


def test_password_hash_roundtrip():
    h = config.hash_password("s3cret", iterations=1000)
    assert config.verify_password("s3cret", h)
    assert not config.verify_password("wrong", h)
    assert not config.verify_password("s3cret", "garbage")


def test_set_device_path_keeps_comments(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_bytes(
        b"# project\r\nchannels:\r\n  - name: L\r\n    driver: logix\r\n    devices:\r\n"
        b"      - {name: A, path: 1.1.1.1}\r\n      - name: B\r\n        path: 10.0.0.1/0   # slot 0\r\n"
    )
    config.set_device_path(p, "L", "B", "10.0.0.2/1")
    assert p.read_bytes().endswith(b'path: "10.0.0.2/1"   # slot 0\r\n')
    assert b"# project\r\n" in p.read_bytes()
    assert [d.path for d in config.load(p).channels[0].devices] == ["1.1.1.1", "10.0.0.2/1"]
    with pytest.raises(config.ConfigError, match="no path"):
        config.set_device_path(p, "L", "Nope", "x")


def test_set_device_path_refuses_invalid(tmp_path):
    p = write(tmp_path, "channels: [{name: R, driver: dnp3, devices: [{name: P, path: h, outstation: 2}]}]")
    before = p.read_text()
    with pytest.raises(config.ConfigError, match="host:port"):
        config.set_device_path(p, "R", "P", "h:notaport")
    assert p.read_text() == before and not list(tmp_path.glob("*.tmp"))


def test_update_device_edits_adds_removes_and_renames(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_bytes(
        b"channels:\r\n  - name: L\r\n    driver: logix\r\n    devices:\r\n"
        b"      - {name: A, path: 1.1.1.1}\r\n      - name: B\r\n        path: 10.0.0.1/0   # slot 0\r\n"
        b"        scan_ms: 500\r\n        writable: [\"X*\"]\r\n"
    )
    config.update_device(p, "L", "A", {"scan_ms": 200, "program_tags": True})
    config.update_device(p, "L", "B", {"name": "B2", "scan_ms": None, "writable": ["Y*", "Z"], "include": ["T*"]}, "M")
    ch = config.load(p).channels[0]
    a, b = ch.devices
    assert ch.name == "M" and (a.scan_ms, a.program_tags) == (200, True)
    assert (b.name, b.scan_ms, b.writable, b.include) == ("B2", 1000, ["Y*", "Z"], ["T*"])
    assert b"# slot 0" in p.read_bytes()
    with pytest.raises(config.ConfigError, match="scan_ms"):
        config.update_device(p, "M", "A", {"scan_ms": 1})
