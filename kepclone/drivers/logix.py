"""Allen-Bradley ControlLogix/CompactLogix over EtherNet/IP (CIP symbolic tags) via pycomm3."""
from __future__ import annotations

import logging
from math import prod
from typing import Any

from pycomm3 import LogixDriver as _Plc

from ..driver import ReadResult, TagDef

log = logging.getLogger(__name__)

# Logix atomic type -> asyncua VariantType name. DWORD is how Logix stores BOOL arrays.
TYPES = {
    "BOOL": "Boolean",
    "SINT": "SByte",
    "INT": "Int16",
    "DINT": "Int32",
    "LINT": "Int64",
    "USINT": "Byte",
    "UINT": "UInt16",
    "UDINT": "UInt32",
    "ULINT": "UInt64",
    "REAL": "Float",
    "LREAL": "Double",
    "DWORD": "Boolean",
}

# CIP identity status bits 8..11
FAULTS = ("minor recoverable", "minor unrecoverable", "major recoverable", "major unrecoverable")


def _struct_def(name: str, data_type: dict) -> TagDef | None:
    if "string" in data_type:  # pycomm3 marks STRING and string-shaped UDTs
        return TagDef(name, "String")
    members = [
        m for m in (_member_def(a, data_type["internal_tags"][a]) for a in data_type["attributes"]) if m
    ]
    return TagDef(name, None, members=members) if members else None


def _member_def(name: str, info: dict) -> TagDef | None:
    if info["tag_type"] == "struct":
        node = _struct_def(name, info["data_type"])
    elif info["data_type"] in TYPES:
        node = TagDef(name, TYPES[info["data_type"]])
    else:
        log.info("skipping member %s: unsupported type %s", name, info["data_type"])
        return None
    if node is None:
        return None
    node.count = info.get("array", 0)  # BOOL members carry "bit" instead of "array"
    if info["data_type"] == "DWORD":  # BOOL[n] member: n/32 DWORDs, 32 bools each
        node.count = (node.count or 1) * 32
    return node


def tag_def(name: str, tag: dict) -> TagDef | None:
    """Convert one entry of pycomm3's `LogixDriver.tags` to a TagDef (None = not served)."""
    access = tag.get("external_access", "Unknown")
    if access == "None":
        return None
    if tag["tag_type"] == "struct":
        node = _struct_def(name, tag["data_type"])
    elif tag["data_type"] in TYPES:
        node = TagDef(name, TYPES[tag["data_type"]])
    else:
        log.info("skipping tag %s: unsupported type %s", name, tag["data_type"])
        return None
    if node is None:
        return None
    node.count = prod(tag["dimensions"][: tag["dim"]]) if tag["dim"] else 0
    if tag["data_type"] == "DWORD":
        # shortcut: assumes the controller reports BOOL array dimensions in bits (pycomm3's
        # element semantics). If a bench read shows only part of a BOOL array, multiply by 32 here.
        node.count = node.count or 32
    node.writable = access in ("Read/Write", "Unknown")  # Unknown = firmware < v18, no attribute
    return node


def _flatten(values):
    out = []
    for v in values:
        out.extend(_flatten(v) if isinstance(v, list) else [v])
    return out


def normalize(t: TagDef, value: Any) -> Any:
    """Shape a pycomm3 value like its TagDef (pycomm3 nests DWORD bools, unwraps 1-element arrays)."""
    if value is None:
        return None
    if t.count:
        if not isinstance(value, list):
            value = [value]
        if t.type == "Boolean":
            return [bool(v) for v in _flatten(value)][: t.count]
        if t.type is None:
            return [_normalize_struct(t, v) for v in value]
        return value
    if t.type is None:
        return _normalize_struct(t, value)
    return value


def _normalize_struct(t: TagDef, value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {m.name: normalize(m, value.get(m.name)) for m in t.members}


class LogixDriver:
    def __init__(self, path: str, program_tags: bool = False):
        self._path = path
        self._program_tags = program_tags
        self._plc: _Plc | None = None

    def connect(self) -> None:
        self._plc = _Plc(self._path, init_program_tags=self._program_tags)
        self._plc.open()  # raises CommError; also uploads the tag list and UDT definitions

    def close(self) -> None:
        plc, self._plc = self._plc, None
        if plc is not None:
            plc.close()

    def browse(self) -> list[TagDef]:
        return [t for name, raw in self._plc.tags.items() if (t := tag_def(name, raw))]

    def status(self) -> dict:
        """Controller identity, mode and fault flags from the CIP identity object."""
        info = self._plc.get_plc_info()
        lo, hi = info["status"][0], info["status"][1]
        mode = info["keyswitch"]
        if mode == "UNKNOWN":  # e.g. faulted: pycomm3 only maps run/program
            mode = f"unknown (status 0x{hi << 8 | lo:04x})"
        rev = info["revision"]
        return {
            "controller": f'{info["product_name"]}, rev {rev["major"]}.{rev["minor"]}, s/n {info["serial"]}, '
            f'program "{self._plc.info.get("name") or "?"}"',
            "mode": mode,
            "faults": [f for i, f in enumerate(FAULTS) if hi >> i & 1],
        }

    def read(self, tags: list[TagDef]) -> dict[str, ReadResult]:
        names = [f"{t.name}{{{t.count}}}" if t.count else t.name for t in tags]
        results = self._plc.read(*names)
        if not isinstance(results, list):
            results = [results]
        return {
            t.name: ReadResult(None, r.error) if r.error else ReadResult(normalize(t, r.value))
            for t, r in zip(tags, results)
        }

    def write(self, path: str, value: Any) -> None:
        if isinstance(value, list):
            path = f"{path}{{{len(value)}}}"
        result = self._plc.write((path, value))
        if result.error:
            raise IOError(f"{path}: {result.error}")
