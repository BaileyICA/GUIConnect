"""OPC UA side: address space per device, value updates, write forwarding, users, certificates."""
from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from asyncua import Node, Server, ua
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from asyncua.crypto.permission_rules import User, UserRole
from cryptography.x509.oid import ExtendedKeyUsageOID

from .config import Config, ServerConfig, verify_password
from .device import Device, matches
from .driver import ReadResult, TagDef

log = logging.getLogger(__name__)
NAMESPACE = "urn:kepclone"
APP_URI = "urn:kepclone:server"
S = ua.StatusCodes
INT_BITS = {"SByte": 8, "Byte": 8, "Int16": 16, "UInt16": 16, "Int32": 32, "UInt32": 32, "Int64": 64, "UInt64": 64}


def _default(vtype: str) -> Any:
    return {"Boolean": False, "String": "", "Float": 0.0, "Double": 0.0}.get(vtype, 0)


@dataclass(eq=False)
class Leaf:
    device: Device
    path: str  # PLC tag path, e.g. "Recipe[2].Speed"
    nodeid: ua.NodeId
    vtype: ua.VariantType
    count: int
    writable: bool
    last: tuple | None = None  # (value, status) last published, to skip unchanged updates
    ts: datetime | None = None  # source timestamp of `last`


class Users:
    """asyncua user manager. Never hands out UserRole.Admin: asyncua lets Admin bypass AccessLevel."""

    def __init__(self, cfg: ServerConfig):
        self._cfg = cfg

    def get_user(self, iserver, username=None, password=None, certificate=None):
        if username is None:
            return User(role=UserRole.User) if self._cfg.anonymous else None
        stored = self._cfg.users.get(username)
        if stored and verify_password(password or "", stored):
            return User(role=UserRole.User, name=username)
        log.warning("OPC UA login failed for user %r", username)
        return None


class OpcUaServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.server = Server(user_manager=Users(cfg.server))
        self.ns = 0
        self._channels: dict[str, Node] = {}
        self._folders: dict[str, Node] = {}  # device key -> device folder
        self._status: dict[str, dict[str, Node]] = {}
        self._tag_nodes: dict[str, list[Node]] = {}  # device key -> top-level tag nodes
        self._tags: dict[str, list[TagDef]] = {}  # device key -> tag defs currently served
        self._by_device: dict[str, dict[str, Leaf]] = {}  # device key -> path -> leaf
        self._leaves: dict[ua.NodeId, Leaf] = {}

    # ---- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        s, c = self.server, self.cfg.server
        await s.init()
        s.set_endpoint(c.endpoint)
        s.set_server_name("GUIConnect")
        await s.set_application_uri(APP_URI)
        policies = [ua.SecurityPolicyType[p] for p in c.security]
        s.set_security_policy(policies)
        s.set_identity_tokens([ua.UserNameIdentityToken] + ([ua.AnonymousIdentityToken] if c.anonymous else []))
        if any(p != ua.SecurityPolicyType.NoSecurity for p in policies):
            await self._load_certificate()
        self.ns = await s.register_namespace(NAMESPACE)
        self._intercept_writes()
        await s.start()
        log.info("OPC UA server listening on %s (%s)", c.endpoint, ", ".join(c.security))

    async def stop(self) -> None:
        await self.server.stop()

    async def _load_certificate(self) -> None:
        pki = self.cfg.base_dir / self.cfg.server.pki_dir
        pki.mkdir(parents=True, exist_ok=True)
        key, cert = pki / "server_key.pem", pki / "server_cert.der"
        # Creates the key/cert on first run and renews an expired or mismatched cert.
        await setup_self_signed_certificate(
            key, cert, APP_URI, socket.gethostname(),
            [ExtendedKeyUsageOID.SERVER_AUTH], {"commonName": "KepClone"},
        )
        await self.server.load_certificate(cert)
        await self.server.load_private_key(key)

    def _nid(self, s: str) -> ua.NodeId:
        return ua.NodeId(s, self.ns)

    def _qn(self, name: str) -> ua.QualifiedName:
        # Explicit: a str browse name is parsed as "ns:Name", which breaks on "Local:1:I"
        return ua.QualifiedName(Name=name, NamespaceIndex=self.ns)

    # ---- address space ---------------------------------------------------------------

    async def add_device(self, dev: Device) -> None:
        ch = self._channels.get(dev.channel)
        if ch is None:
            ch = await self.server.nodes.objects.add_folder(self._nid(dev.channel), self._qn(dev.channel))
            self._channels[dev.channel] = ch
        folder = await ch.add_folder(self._nid(dev.key), self._qn(dev.cfg.name))
        self._folders[dev.key] = folder
        self._status[dev.key] = {
            name: await folder.add_variable(self._nid(f"{dev.key}.{name}"), self._qn(name), ua.Variant(v, t))
            for name, v, t in (
                ("_Connected", False, ua.VariantType.Boolean),
                ("_Error", dev.last_error, ua.VariantType.String),
                ("_ScanTimeMs", 0.0, ua.VariantType.Double),
            )
        }
        self._tag_nodes[dev.key] = []
        self._by_device[dev.key] = {}

    async def _rebuild(self, dev: Device, tags: list[TagDef]) -> None:
        if self._tag_nodes[dev.key]:
            await self.server.delete_nodes(self._tag_nodes[dev.key], recursive=True)
        for leaf in self._by_device[dev.key].values():
            self._leaves.pop(leaf.nodeid, None)
        self._by_device[dev.key] = {}
        folder = self._folders[dev.key]
        self._tag_nodes[dev.key] = [await self._add(dev, folder, t, t.name, t.name, t.writable) for t in tags]
        self._tags[dev.key] = tags
        log.info("%s: %d OPC UA variables", dev.key, len(self._by_device[dev.key]))

    async def _add(self, dev: Device, parent: Node, t: TagDef, path: str, name: str, plc_writable: bool) -> Node:
        nid = self._nid(f"{dev.key}.{path}")
        if t.type is None:  # structure, or array of structures
            node = await parent.add_folder(nid, self._qn(name))
            if t.count:
                element = TagDef("", None, members=t.members)
                for i in range(t.count):
                    await self._add(dev, node, element, f"{path}[{i}]", f"[{i}]", plc_writable)
            else:
                for m in t.members:
                    await self._add(dev, node, m, f"{path}.{m.name}", m.name, plc_writable)
            return node

        vtype = ua.VariantType[t.type]
        default = [_default(t.type)] * t.count if t.count else _default(t.type)
        var = await parent.add_variable(nid, self._qn(name), ua.Variant(default, vtype))
        if t.count:
            await var.write_value_rank(1)
            await var.write_array_dimensions([t.count])
        writable = plc_writable and matches(path, dev.cfg.writable)
        if writable:
            await var.set_writable()
        leaf = Leaf(dev, path, nid, vtype, t.count, writable)
        self._by_device[dev.key][path] = leaf
        self._leaves[nid] = leaf
        await self._publish(leaf, None, S.BadWaitingForInitialData)
        return var

    # ---- values (Sink interface used by Device) --------------------------------------

    async def device_browsed(self, dev: Device, tags: list[TagDef]) -> None:
        if self._tags.get(dev.key) != tags:  # first connect, or PLC program changed
            await self._rebuild(dev, tags)
        await self._publish_status(dev)

    async def device_values(self, dev: Device, results: dict[str, ReadResult], ts: datetime) -> None:
        for t in self._tags.get(dev.key, []):
            r = results.get(t.name)
            if r is None:
                continue
            if r.error:
                await self._mark(dev, t.name, S.BadDeviceFailure)
            else:
                await self._apply(dev, t, t.name, r.value, ts)
        await self._publish_status(dev)

    async def device_down(self, dev: Device) -> None:
        for leaf in self._by_device.get(dev.key, {}).values():
            await self._publish(leaf, None, S.BadNotConnected)
        await self._publish_status(dev)

    async def _apply(self, dev: Device, t: TagDef, path: str, value: Any, ts: datetime) -> None:
        if value is None:
            await self._mark(dev, path, S.BadDeviceFailure)
        elif t.type is not None:
            leaf = self._by_device[dev.key].get(path)
            if leaf:
                await self._publish(leaf, value, S.Good, ts)
        elif t.count:
            for i in range(t.count):
                element = value[i] if i < len(value) else {}
                for m in t.members:
                    await self._apply(dev, m, f"{path}[{i}].{m.name}", element.get(m.name), ts)
        else:
            for m in t.members:
                await self._apply(dev, m, f"{path}.{m.name}", value.get(m.name), ts)

    async def _mark(self, dev: Device, prefix: str, code: int) -> None:
        for path, leaf in self._by_device[dev.key].items():
            if path == prefix or path.startswith((prefix + ".", prefix + "[")):
                await self._publish(leaf, None, code)

    async def _publish(self, leaf: Leaf, value: Any, code: int, ts: datetime | None = None) -> None:
        if leaf.last == (value, code):
            return
        leaf.last = (value, code)
        now = datetime.now(timezone.utc)
        leaf.ts = ts or now
        dv = ua.DataValue(
            Value=ua.Variant(value, leaf.vtype) if code == S.Good else ua.Variant(),
            StatusCode=ua.StatusCode(code),
            SourceTimestamp=ts or now,
            ServerTimestamp=now,
        )
        await self.server.write_attribute_value(leaf.nodeid, dv)

    async def _publish_status(self, dev: Device) -> None:
        nodes = self._status[dev.key]
        await nodes["_Connected"].write_value(ua.Variant(dev.connected, ua.VariantType.Boolean))
        await nodes["_Error"].write_value(ua.Variant(dev.last_error, ua.VariantType.String))
        await nodes["_ScanTimeMs"].write_value(ua.Variant(round(dev.scan_ms, 1), ua.VariantType.Double))

    # ---- client writes ---------------------------------------------------------------

    def _intercept_writes(self) -> None:
        # Wraps asyncua's internal AttributeService.write (asyncua pinned in requirements.txt).
        # asyncua's own write hooks are sync or cannot reject per item; this one awaits the
        # PLC and returns a real per-item status to the client.
        service = self.server.iserver.attribute_service
        original = service.write

        async def write(params: ua.WriteParameters, user: User = User(role=UserRole.Admin)):
            if user.role == UserRole.Admin:  # the server's own internal session
                return await original(params, user)
            results = []
            for wv in params.NodesToWrite:
                leaf = self._leaves.get(wv.NodeId)
                if leaf is None or wv.AttributeId != ua.AttributeIds.Value:
                    results += await original(ua.WriteParameters(NodesToWrite=[wv]), user)
                else:
                    results.append(ua.StatusCode(await self._client_write(leaf, wv.Value.Value, user)))
            return results

        service.write = write

    async def _client_write(self, leaf: Leaf, variant: ua.Variant | None, user: User) -> int:
        who = user.name or "anonymous"
        where = f"{leaf.device.key}.{leaf.path}"
        if not leaf.writable:
            code = S.BadNotWritable
        elif user.name is None and not self.cfg.server.anonymous_write:
            code = S.BadUserAccessDenied
        elif variant is None or variant.VariantType != leaf.vtype or not _shape_ok(variant.Value, leaf.count):
            code = S.BadTypeMismatch
        else:
            try:
                await self._write(leaf, variant.Value, who)
            except Exception:
                return S.BadCommunicationError
            return S.Good
        log.warning("WRITE REJECTED %s by %s: %s", where, who, ua.StatusCode(code).name)
        return code

    async def local_write(self, key: str, path: str, value: Any, who: str) -> None:
        """Write from the desktop window: same tag rules as OPC UA clients, no login. Raises on failure."""
        leaves = self._by_device.get(key, {})
        leaf = leaves.get(path)
        if leaf is None:  # one array element or bit: "Words[3]", "Words[3].5", "Word.5"
            base, idx, bit = split_bit(path)
            leaf = leaves.get(base)
            vtype = sub_type(leaf.vtype.name, leaf.count, idx, bit) if leaf else None
            if not vtype or isinstance(value, list) or isinstance(value, bool) != (vtype == "Boolean"):
                leaf = None
        if leaf is None or not leaf.writable:
            log.warning("WRITE REJECTED %s.%s by %s: not writable", key, path, who)
            raise PermissionError(f"{key}.{path} is not writable")
        await self._write(leaf, value, who, path)

    async def _write(self, leaf: Leaf, value: Any, who: str, path: str | None = None) -> None:
        # element and bit paths go to the PLC as is; for a bit pycomm3 sends Read-Modify-Write, so other bits are untouched
        path = path or leaf.path
        where = f"{leaf.device.key}.{path}"
        try:
            await leaf.device.write(path, value)
        except Exception as e:
            log.warning("WRITE FAILED %s = %r by %s: %s", where, value, who, e)
            raise
        log.info("WRITE %s = %r by %s", where, value, who)
        if path == leaf.path:  # after an element or bit write the whole value is unknown here; the next poll shows it
            await self._publish(leaf, value, S.Good)


def split_bit(path: str) -> tuple[str, int | None, int | None]:
    """'Words[3].5' -> ('Words', 3, 5); 'Bools[3]' -> ('Bools', 3, None); 'Word.5' -> ('Word', None, 5)."""
    base, idx, bit = re.fullmatch(r"(.+?)(?:\[(\d+)\])?(?:\.(\d+))?", path).groups()
    return base, idx and int(idx), bit and int(bit)


def sub_type(vtype: str, count: int, idx: int | None, bit: int | None) -> str | None:
    """Type of tag[idx].bit for a tag of this type and array size; None if the path does not fit the tag."""
    if idx is not None and not (count and idx < count):
        return None
    if bit is None:
        return vtype
    if count and idx is None:
        return None
    return "Boolean" if bit < INT_BITS.get(vtype, 0) else None


def _shape_ok(value: Any, count: int) -> bool:
    if count:
        return isinstance(value, list) and len(value) == count
    return not isinstance(value, list)
