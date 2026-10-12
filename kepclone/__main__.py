"""CLI: python -m kepclone run <config.yaml> [-v] | gui [config.yaml] | hash-password"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import logging.handlers
import sys

from . import config as config_mod
from .device import Device
from .drivers import make_driver
from .opcua_server import OpcUaServer

log = logging.getLogger("kepclone")


async def serve(cfg: config_mod.Config, ready=None) -> None:
    """`ready(server, devices)` is called once everything is set up (used by the GUI)."""
    server = OpcUaServer(cfg)
    await server.start()
    devices = [Device(ch.name, d, make_driver(ch.driver, d)) for ch in cfg.channels for d in ch.devices]
    for d in devices:
        await server.add_device(d)
    if ready:
        ready(server, devices)
    tasks = [asyncio.create_task(d.run(server), name=d.key) for d in devices]
    try:
        await asyncio.Event().wait()  # run until Ctrl+C / service stop cancels us
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for d in devices:
            await d.close()
        await server.stop()
        log.info("stopped")


def _setup_logging(cfg: config_mod.Config, verbose: bool, console: bool = True) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [
        logging.handlers.RotatingFileHandler(
            cfg.base_dir / "kepclone.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
    ]
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        logging.root.addHandler(h)
    logging.root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not verbose:
        # Our own warnings carry the error; these libraries would repeat full tracebacks
        # on every reconnect attempt.
        logging.getLogger("asyncua").setLevel(logging.WARNING)
        logging.getLogger("pycomm3").setLevel(logging.CRITICAL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kepclone")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the gateway")
    run.add_argument("config")
    run.add_argument("-v", "--verbose", action="store_true")
    gui = sub.add_parser("gui", help="run the gateway with the desktop window")
    gui.add_argument("config", nargs="?", default="config.yaml")
    sub.add_parser("hash-password", help="hash a password for server.users")
    args = parser.parse_args(argv)

    if args.cmd == "gui":
        from .gui import run_gui

        return run_gui(args.config)

    if args.cmd == "hash-password":
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat: "):
            print("passwords differ", file=sys.stderr)
            return 1
        print(config_mod.hash_password(pw))
        return 0

    try:
        cfg = config_mod.load(args.config)
    except (OSError, config_mod.ConfigError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    _setup_logging(cfg, args.verbose)
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:  # Ctrl+C, or NSSM stopping the service
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
