"""Load and validate the YAML project file."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml
from asyncua import ua

from .drivers import DRIVERS


class ConfigError(ValueError):
    pass


@dataclass
class DeviceConfig:
    name: str
    path: str  # pycomm3 CIP path: "192.168.1.10", "192.168.1.10/2" (slot), or a full route
    scan_ms: int = 1000
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(default_factory=list)
    program_tags: bool = False
    writable: list[str] = field(default_factory=list)  # fnmatch patterns on tag paths
    # dnp3 only
    outstation: int | None = None  # RTU's DNP3 address
    master: int = 1  # our DNP3 address
    names: dict[str, str] = field(default_factory=dict)  # friendly name -> "AI.5"


@dataclass
class ChannelConfig:
    name: str
    driver: str
    devices: list[DeviceConfig]


@dataclass
class ServerConfig:
    endpoint: str = "opc.tcp://0.0.0.0:4840/kepclone"
    security: list[str] = field(default_factory=lambda: ["NoSecurity", "Basic256Sha256_SignAndEncrypt"])
    pki_dir: str = "pki"
    anonymous: bool = True
    anonymous_write: bool = False
    users: dict[str, str] = field(default_factory=dict)  # username -> hash_password() output


@dataclass
class Config:
    server: ServerConfig
    channels: list[ChannelConfig]
    base_dir: Path  # folder of the config file; relative paths resolve here


def _build(cls, data, where):
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    try:
        obj = cls(**data)
    except TypeError as e:  # missing required key
        raise ConfigError(f"{where}: {e}") from None
    for f in fields(cls):
        value = getattr(obj, f.name)
        if str(f.type).startswith("list[str]") and not (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            raise ConfigError(f"{where}.{f.name}: expected a list of strings")
        if f.type == "bool" and not isinstance(value, bool):  # "false" as a string would be truthy
            raise ConfigError(f"{where}.{f.name}: expected true or false")
    return obj


def _check_name(name, where):
    # '.' separates Channel.Device.Tag in NodeIds
    if not isinstance(name, str) or not name or "." in name:
        raise ConfigError(f"{where}: name must be a non-empty string without '.'")


def _check_dnp3(dev: DeviceConfig, where: str) -> None:
    from .drivers.dnp3 import TYPES, parse_point_ref

    if dev.names is None:  # "names:" with every entry commented out
        dev.names = {}
    _, sep, port = dev.path.rpartition(":")
    if sep and not (port.isdigit() and 0 < int(port) < 65536):
        raise ConfigError(f"{where}.path: expected host or host:port, got {dev.path!r}")
    for key, value in (("outstation", dev.outstation), ("master", dev.master)):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 65519:
            raise ConfigError(f"{where}.{key}: expected a DNP3 address 0..65519")
    if dev.outstation == dev.master:
        raise ConfigError(f"{where}: outstation and master addresses must differ")
    if not isinstance(dev.names, dict):
        raise ConfigError(f"{where}.names: expected a mapping of name -> point, e.g. TankLevel: AI.5")
    seen = {}
    for name, ref in dev.names.items():
        if not isinstance(name, str) or not name or "." in name or name in TYPES:
            raise ConfigError(f"{where}.names: {name!r} must be non-empty, without '.', and not one of {list(TYPES)}")
        point = parse_point_ref(ref) if isinstance(ref, str) else None
        if point is None:
            raise ConfigError(f"{where}.names.{name}: expected <TYPE>.<index> with TYPE in {list(TYPES)}, got {ref!r}")
        if point in seen:
            raise ConfigError(f"{where}.names: {name!r} and {seen[point]!r} both map to {ref}")
        seen[point] = name


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    unknown = set(raw) - {"server", "channels"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s) {sorted(unknown)}")

    server = _build(ServerConfig, raw.get("server") or {}, "server")
    for p in server.security:
        if p not in ua.SecurityPolicyType.__members__:
            raise ConfigError(
                f"server.security: unknown policy {p!r}; use one of {list(ua.SecurityPolicyType.__members__)}"
            )
    if server.users is None:  # "users:" with every entry commented out
        server.users = {}
    if not isinstance(server.users, dict):
        raise ConfigError("server.users: expected a mapping of username -> password hash")
    for user, stored in server.users.items():
        if not _parse_hash(stored):
            raise ConfigError(f"server.users.{user}: not a hash; generate with: python -m kepclone hash-password")

    channels, seen = [], set()
    for i, ch_raw in enumerate(raw.get("channels") or []):
        where = f"channels[{i}]"
        if not isinstance(ch_raw, dict):
            raise ConfigError(f"{where}: expected a mapping")
        devices_raw = ch_raw.get("devices") or []
        ch = _build(ChannelConfig, {**ch_raw, "devices": []}, where)
        _check_name(ch.name, where)
        if ch.driver not in DRIVERS:
            raise ConfigError(f"{where}.driver: unknown driver {ch.driver!r}; available: {sorted(DRIVERS)}")
        for j, dev_raw in enumerate(devices_raw):
            dwhere = f"{where}.devices[{j}]"
            dev = _build(DeviceConfig, dev_raw, dwhere)
            _check_name(dev.name, dwhere)
            if not isinstance(dev.scan_ms, int) or dev.scan_ms < 50:
                raise ConfigError(f"{dwhere}.scan_ms: must be an integer >= 50")
            if ch.driver == "dnp3":
                _check_dnp3(dev, dwhere)
            key = f"{ch.name}.{dev.name}".lower()
            if key in seen:
                raise ConfigError(f"{dwhere}: duplicate device {ch.name}.{dev.name}")
            seen.add(key)
            ch.devices.append(dev)
        channels.append(ch)
    if not channels:
        raise ConfigError("no channels configured")
    return Config(server=server, channels=channels, base_dir=path.parent)


def set_device_path(path: str | Path, channel: str, device: str, value: str) -> None:
    """Change one device's address in the project file, keeping its comments and layout."""
    path = Path(path)
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()

    def pick(mapping, key):
        return next((v for k, v in getattr(mapping, "value", []) if getattr(k, "value", None) == key), None)

    def named(seq, name):
        return next((n for n in getattr(seq, "value", []) if getattr(pick(n, "name"), "value", None) == name), None)

    node = pick(named(pick(named(pick(yaml.compose(text), "channels"), channel), "devices"), device), "path")
    if node is None:
        raise ConfigError(f"{channel}.{device}: no path in {path.name}; was the file edited? Reload it.")
    # A JSON string is a valid YAML double-quoted scalar, whatever the value contains.
    text = text[: node.start_mark.index] + json.dumps(value) + text[node.end_mark.index :]
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        load(tmp)  # never save a project that would not load
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _parse_hash(stored):
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo == "pbkdf2_sha256":
            return int(iterations), bytes.fromhex(salt), bytes.fromhex(digest)
    except (AttributeError, ValueError):
        pass
    return None


def verify_password(password: str, stored: str) -> bool:
    parsed = _parse_hash(stored)
    if not parsed:
        return False
    iterations, salt, digest = parsed
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations), digest)
