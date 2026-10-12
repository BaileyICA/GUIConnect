"""One PLC: connect, browse, poll, reconnect. All driver calls for a device run on one thread."""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Protocol

from .config import DeviceConfig
from .driver import Driver, ReadResult, TagDef

log = logging.getLogger(__name__)
MAX_BACKOFF_S = 30
STATUS_S = 5  # how often to refresh the PLC's mode/faults


def matches(name: str, patterns: list[str]) -> bool:
    # Logix tag names are case-insensitive
    name = name.lower()
    return any(fnmatchcase(name, p.lower()) for p in patterns)


class Sink(Protocol):
    async def device_browsed(self, device: Device, tags: list[TagDef]) -> None: ...

    async def device_values(self, device: Device, results: dict[str, ReadResult], ts: datetime) -> None: ...

    async def device_down(self, device: Device) -> None: ...


class Device:
    def __init__(self, channel: str, cfg: DeviceConfig, driver: Driver):
        self.channel = channel
        self.cfg = cfg
        self.driver = driver
        self.key = f"{channel}.{cfg.name}"
        self.connected = False
        self.last_error = "not connected yet"
        self.scan_ms = 0.0
        self.info: dict = {}  # driver.status(), if the driver has one
        # One worker: polls and client writes never interleave on the same PLC connection.
        self._io = ThreadPoolExecutor(1, thread_name_prefix=self.key)

    async def _call(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._io, fn, *args)

    async def run(self, sink: Sink) -> None:
        backoff = 1
        while True:
            try:
                await self._call(self.driver.connect)
                tags = [
                    t
                    for t in await self._call(self.driver.browse)
                    if matches(t.name, self.cfg.include) and not matches(t.name, self.cfg.exclude)
                ]
                log.info("%s connected (%s), serving %d tags", self.key, self.cfg.path, len(tags))
                self.connected, self.last_error, backoff = True, "", 1
                await sink.device_browsed(self, tags)
                await self._poll(sink, tags)
            except Exception as e:
                self.connected, self.info = False, {}
                self.last_error = str(e) or type(e).__name__
                log.warning(
                    "%s down: %s (retry in %ds)", self.key, self.last_error, backoff,
                    exc_info=log.isEnabledFor(logging.DEBUG),
                )
                await sink.device_down(self)
                await self._call(self._close_quietly)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _poll(self, sink: Sink, tags: list[TagDef]) -> None:
        period = self.cfg.scan_ms / 1000
        next_status = 0.0
        while True:
            if time.monotonic() >= next_status and hasattr(self.driver, "status"):
                self.info = await self._call(self._status)
                next_status = time.monotonic() + STATUS_S
            start = time.monotonic()  # after the status request: keep it out of scan time
            results = await self._call(self.driver.read, tags) if tags else {}
            # shortcut: every tag failing is treated as a lost connection; a PLC whose only
            # served tag always errors will reconnect in a loop. Exclude that tag.
            if results and all(r.error for r in results.values()):
                raise ConnectionError(next(iter(results.values())).error)
            self.scan_ms = (time.monotonic() - start) * 1000
            await sink.device_values(self, results, datetime.now(timezone.utc))
            await asyncio.sleep(max(0.0, period - (time.monotonic() - start)))

    async def write(self, path: str, value: Any) -> None:
        if not self.connected:
            raise ConnectionError(f"{self.key} not connected")
        await self._call(self.driver.write, path, value)

    def _status(self) -> dict:
        try:
            return self.driver.status()
        except Exception as e:  # a lost connection still shows up through the next read
            return {"mode": f"status unavailable: {e}", "faults": []}

    def _close_quietly(self) -> None:
        try:
            self.driver.close()
        except Exception as e:
            log.debug("%s close: %s", self.key, e)

    async def close(self) -> None:
        await self._call(self._close_quietly)
        self._io.shutdown(wait=False)
