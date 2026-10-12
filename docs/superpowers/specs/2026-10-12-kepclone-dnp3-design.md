# KepClone — DNP3 master driver (Brodersen RTUs)

## Goal
Poll Brodersen RTUs (DNP3 outstations) over TCP/IP and serve their points on the existing
OPC UA server, next to the Logix tags. Read-only.

Success: points from a live Brodersen RTU appear under `<Channel>.<Device>` with correct
values and quality; RTU unplugged -> `BadNotConnected`, plugged back -> recovers.

## Decisions
- Pure-Python master, no library: no maintained Python DNP3 package exists for Windows /
  Python 3.14 (`pydnp3` 2018/py3.6, `dnp3-python` Linux-only py3.8-3.10, opendnp3 archived).
  Step Function `dnp3` needs a commercial license and .NET/Rust interop.
- TCP only (default port 20000). Read-only: no CROB/analog output, no time sync,
  no restart-IIN clear, no Secure Authentication, no serial, no octet strings.

## Files
- `kepclone/drivers/dnp3_codec.py`: pure functions. CRC-16/DNP, link frame encode/decode,
  transport segmentation/reassembly, application request build, response object parser.
- `kepclone/drivers/dnp3.py`: `Dnp3Driver` implementing `kepclone.driver.Driver`.
- OPC UA layer, Device, write path: unchanged.

## Config
```yaml
- name: RTUs
  driver: dnp3
  devices:
    - name: Pump3
      path: 10.0.5.20:20000   # host[:port], port defaults to 20000
      outstation: 10          # required, 0..65519
      master: 1               # default 1
      scan_ms: 2000
      names: {TankLevel: AI.5, PumpRun: BI.0}
```
`names` values must be `<TYPE>.<index>` with TYPE in BI, DBI, BO, CTR, FCTR, AI, AO;
keys must not contain `.` or equal a TYPE.

## Protocol behaviour
- Link: master sends UNCONFIRMED_USER_DATA (ctrl 0xC4). Outstation REQUEST_LINK_STATUS ->
  LINK_STATUS; RESET_LINK / TEST_LINK / CONFIRMED_USER_DATA -> ACK. Frames for other
  addresses ignored. Bad start bytes or CRC -> error -> reconnect.
- Transport: FIR/FIN/6-bit seq; reassembly restarts on FIR, drops out-of-sequence segments.
- Application:
  - connect: DISABLE_UNSOLICITED (FC 21) for classes 1-3, then READ class 1,2,3,0
    (g60v2, v3, v4, v1, qualifier 06). The class 0 result defines the browse list.
  - each scan: the same READ. Events first, statics last, so the last value is current.
  - CON bit -> CONFIRM (UNS bit mirrored for unsolicited). Multi-fragment responses
    collected until FIN. Unsolicited responses are confirmed and their data ignored.
  - Response timeout 5 s per fragment -> error -> reconnect with Device backoff.
- Objects parsed (static and event): g1, g2, g3, g4, g10, g11, g20, g21, g22, g23, g30,
  g32, g40, g42 (all standard variations, incl. packed and with-time), g50/g51 time/CTO,
  g80 IIN, g110/g111 octet strings (skipped). Qualifiers 00/01/02 start-stop,
  07/08/09 count, 17/28/39 index-prefixed count. Unsupported object -> stop parsing that
  fragment, keep points parsed so far, log a warning.
- Point quality: ONLINE flag (bit 0) clear -> value None -> OPC UA `BadDeviceFailure`.
  Packed variations carry no flags -> online.

## OPC UA view
| Type folder | DNP3 groups | OPC UA type |
|---|---|---|
| BI | 1, 2 | Boolean |
| DBI | 3, 4 | Byte (0 intermediate, 1 off, 2 on, 3 indeterminate) |
| BO | 10, 11 | Boolean |
| CTR | 20, 22 | UInt32 |
| FCTR | 21, 23 | UInt32 |
| AI | 30, 32 | Double |
| AO | 40, 42 | Double |

Unnamed points: `<Channel>.<Device>.<TYPE>.<index>`. Named points: `<Channel>.<Device>.<Name>`
(not repeated in the type folder). A named point the RTU does not report shows
`BadDeviceFailure`. All DNP3 nodes are read-only. Points added on the RTU appear after the
next reconnect.

## Testing
- CRC-16/DNP check value (`123456789` -> 0xEA82) and a known link-reset frame.
- Parser: every supported group/variation and qualifier from hand-built bytes.
- Fake outstation (TCP thread) exercising link-status keepalive, unsolicited confirm,
  two-fragment response with confirms, offline flag, named and unnamed points.
- Live: `tests/test_live_dnp3.py`, skipped unless `KEPCLONE_DNP3_PATH` and
  `KEPCLONE_DNP3_OUTSTATION` are set.
