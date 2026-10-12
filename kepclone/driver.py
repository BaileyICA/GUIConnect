"""Driver-agnostic tag model and the interface every protocol driver implements."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TagDef:
    """A PLC tag or structure member.

    `type` is an asyncua VariantType name ("Int32", "Float", ...) so the OPC UA layer
    never sees protocol-specific types. `type is None` means a structure (see `members`).
    `count > 0` means an array of that many elements.
    """

    name: str
    type: str | None = None
    count: int = 0
    members: list[TagDef] = field(default_factory=list)
    writable: bool = True  # the PLC itself allows external writes


@dataclass
class ReadResult:
    value: Any = None  # scalar, list, or dict (structure) shaped like the TagDef
    error: str | None = None


class Driver(Protocol):
    """Blocking calls; the Device runs them on its own I/O thread."""

    def connect(self) -> None: ...

    def close(self) -> None: ...

    def browse(self) -> list[TagDef]: ...

    def read(self, tags: list[TagDef]) -> dict[str, ReadResult]: ...

    def write(self, path: str, value: Any) -> None:
        """Write one tag or member path; raise on failure."""
        ...

    # Optional: def status(self) -> dict with "controller", "mode", "faults" (list of str),
    # polled every few seconds while connected.
