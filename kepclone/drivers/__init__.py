"""Protocol drivers, selected by the channel's `driver` key."""
from __future__ import annotations

from ..driver import Driver

DRIVERS = {"logix", "dnp3"}


def make_driver(kind: str, device) -> Driver:
    if kind == "logix":
        from .logix import LogixDriver

        return LogixDriver(device.path, program_tags=device.program_tags)
    if kind == "dnp3":
        from .dnp3 import DEFAULT_PORT, Dnp3Driver

        host, _, port = device.path.rpartition(":") if ":" in device.path else (device.path, "", "")
        return Dnp3Driver(host, int(port or DEFAULT_PORT), device.outstation, device.master, device.names)
    raise ValueError(f"unknown driver {kind!r}")
