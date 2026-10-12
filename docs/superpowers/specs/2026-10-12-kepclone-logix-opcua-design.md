# KepClone v1 â€” Allen-Bradley Logix â†’ OPC UA gateway

## Goal
Internal, Kepware-style gateway in Python. Connects directly to ControlLogix/CompactLogix
PLCs over EtherNet/IP (CIP), auto-imports their tags, and serves them over OPC UA to
SCADA/HMI clients (Ignition, AVEVA, FactoryTalk, historians). Runs as a Windows service via NSSM.

Success: an OPC UA client (e.g. UaExpert) browses, reads, subscribes to, and writes
(allow-listed) live PLC tags through the gateway on the bench PLC.

## Scale / constraints
- 1-5 PLCs, < ~2,000 tags each, ~1 s scan.
- Python 3.14 on Windows. Internal use only.
- Pinned deps: `pycomm3==1.2.16` (MIT), `asyncua==2.0.1` (LGPL-3.0+), `PyYAML`.
  asyncua 2.1.0 skipped: released 2026-10-06, under two weeks old at design time.

## Architecture
```
kepclone/
  config.py          YAML -> dataclasses, validation, password hashing
  driver.py          TagDef / ReadResult / Driver protocol (driver-agnostic)
  drivers/logix.py   pycomm3 LogixDriver wrapper
  device.py          per-PLC connect/browse/poll loop, reconnect, serialized I/O
  opcua_server.py    asyncua server, address space, write intercept, users, certs
  __main__.py        CLI: run | hash-password
```
- Kepware model: channel (protocol) -> device (one PLC) -> tags.
- NodeIds match Kepware: `ns=2;s=<Channel>.<Device>.<TagPath>`; namespace URI `urn:kepclone`.
- Driver value types are asyncua `VariantType` names, so the OPC UA layer never sees
  Logix types and future drivers (Modbus, S7) plug in unchanged.
- Each device owns a single-thread executor; every PLC call (connect, browse, read,
  write) runs there, so client writes never interleave with polls on one CIP connection.

## Data flow
1. Device task: `connect()` -> `browse()` -> filter (`include`/`exclude` fnmatch on
   top-level names, case-insensitive) -> sink builds/rebuilds the device subtree.
2. Poll every `scan_ms`: one `driver.read(tags)`; pycomm3 packs multi-service requests.
   Results are split into leaves; only changed value/status is written to OPC UA, with
   the read time as SourceTimestamp.
3. Tag read error -> that tag's leaves `BadDeviceFailure`. All tags failing, or a
   `CommError`, -> connection treated as lost.
4. Disconnect -> all device leaves `BadNotConnected`, `_Connected=false`, `_Error` set;
   reconnect with backoff 1 s doubling to 30 s. Tag list re-browsed on every reconnect;
   subtree rebuilt only if the tag definitions changed.

### Type mapping
| Logix | OPC UA |
|---|---|
| BOOL | Boolean |
| SINT/INT/DINT/LINT | SByte/Int16/Int32/Int64 |
| USINT/UINT/UDINT/ULINT | Byte/UInt16/UInt32/UInt64 |
| REAL/LREAL | Float/Double |
| STRING and string-shaped UDTs | String |
| BOOL array (DWORD storage) | Boolean array |
| atomic array | 1-D array variable (multi-dim arrays flattened) |
| UDT / AOI | object with member children (recursive) |
| UDT array | object with `[i]` children |
Unsupported atomic types (e.g. LWORD, timestamps) are skipped with a log line.

### Status nodes per device
`<Channel>.<Device>._Connected` (Boolean), `._Error` (String), `._ScanTimeMs` (Double).

## Writes
- asyncua `AttributeService.write` is wrapped (internal API; version pinned; covered by
  tests). Internal (Admin) writes pass straight through. For client writes to a PLC leaf:
  1. Leaf not writable -> `BadNotWritable`.
  2. Anonymous user and `anonymous_write: false` -> `BadUserAccessDenied`.
  3. Variant type / array length mismatch -> `BadTypeMismatch`.
  4. `device.write(path, value)` through the device executor; failure ->
     `BadCommunicationError`, nothing updated.
  5. Success -> leaf updated, `Good`; audit log line with user, tag, value.
- Writable = device `writable` fnmatch allow-list matches the leaf path AND the PLC
  ExternalAccess is Read/Write. ExternalAccess `None` tags are not imported.

## Security
- Endpoint policies from config (`NoSecurity`, `Basic256Sha256_SignAndEncrypt`, ...).
- Self-signed server cert auto-created (and renewed when invalid) in `pki/`.
- Users: `server.users` map of username -> PBKDF2-SHA256 hash
  (`python -m kepclone hash-password`). Network clients never get the asyncua Admin role.
- `anonymous: true` (default) allows anonymous read; `anonymous_write: false` (default).
- Client certificates are not validated (internal network). Upgrade path: asyncua
  `CertificateValidator` + trust list.

## Config example
See `config.example.yaml`.

## Testing
- Unit: config validation, password hash, filters, Logix tag-list -> TagDef conversion
  (fixtures shaped like pycomm3 output), value normalization.
- Integration: real asyncua server + asyncua client on localhost with a fake driver:
  browse, poll updates, disconnect -> Bad status, writes (allowed, denied, type mismatch,
  PLC failure, anonymous denied).
- Live: `tests/test_live_plc.py`, skipped unless `KEPCLONE_PLC_PATH` is set; reads tag
  list and one scan from the bench PLC. Optional write test on `KEPCLONE_PLC_WRITE_TAG`.

## Out of scope (v1)
Config UI / REST API, OPC DA, MQTT, other protocols, redundancy, per-tag scan rates,
subscription-driven polling (all imported tags are polled), historian, client cert trust list.

## Windows service
NSSM wraps `python -m kepclone run <config>` (restart on crash, Ctrl+C stop -> clean
shutdown). Chosen over a pywin32 service: pythonservice.exe needs DLL fix-ups inside a venv.
Commands in README.

