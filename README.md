# GUIConnect

Kepware-style gateway: Allen-Bradley ControlLogix/CompactLogix (EtherNet/IP, direct, no
RSLinx) -> OPC UA server for SCADA/HMI. Design: `docs/superpowers/specs/2026-10-12-kepclone-logix-opcua-design.md`.

## Setup (Windows, Python 3.14)

```powershell
cd D:\Projects\KepClone
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy config.example.yaml config.yaml
.venv\Scripts\python -m kepclone hash-password      # paste output under server.users
.venv\Scripts\python -m kepclone run config.yaml    # -v for debug logging
.venv\Scripts\python -m kepclone gui config.yaml    # same gateway, with the desktop window
```

## Desktop window

`kepclone gui` runs the gateway and shows: the project tree (channels, devices, connection
state), the selected device's address, driver, scan time and last error, for Logix the
controller's catalog/revision/serial/program and its mode and fault flags (refreshed every 5 s),
its tags with live value, quality and timestamp (filter box; double-click a tag for its full
value and NodeId; expand an array to see its elements and an integer to see its bits),
the event log, and the connected OPC UA clients. Menus: File (open/reload/edit project,
log folder), View > Settings (server, devices, project as loaded), Tools > Hash Password.
Change Address… (or double-click a device) saves a new device address to the project file,
keeping its comments, and restarts the gateway. Other settings are read-only in the window:
edit the project file, then File > Reload Project (F5).
Closing the window stops the gateway; use `run` under NSSM for an always-on service.

Log: `kepclone.log` next to the config file (5 x 5 MB rotation). Every client write is
logged with user, tag and value (`WRITE ...`, `WRITE REJECTED ...`, `WRITE FAILED ...`).

## Standalone exe

`dist\GUIConnect.exe` needs no Python install. Put `config.yaml` next to it and double-click
to open the desktop window, or run `GUIConnect.exe run C:\path\config.yaml` for console mode. `GUIConnect.exe hash-password` works too.
Rebuild after code changes:

```powershell
.venv\Scripts\python -m pip install pyinstaller==6.22.3
.venv\Scripts\pyinstaller --noconfirm --onefile --hide-console hide-early --icon $PWD\kepclone\kepclone.ico --add-data "$PWD\kepclone\kepclone.ico;kepclone" --name GUIConnect --collect-all asyncua --workpath $env:TEMP\kepclone-build --specpath $env:TEMP\kepclone-build kepclone_exe.py
```

## Installer

`dist\GUIConnect-Setup.exe` is a one-file installer: no admin rights, no Python, nothing else to
install (the exe bundles Python and all libraries). The wizard offers a desktop shortcut, a Start
menu shortcut, and an optional Windows Firewall rule for TCP 4840 (asks for admin only if ticked).
It installs to `%LOCALAPPDATA%\Programs\GUIConnect` with a starter `config.yaml` that upgrades and
uninstall never overwrite or delete. Build it (installs Inno Setup via winget if missing):

```powershell
.\build-installer.ps1
```

## Connecting a client

- Endpoint: `opc.tcp://<host>:4840/kepclone`. Open TCP 4840 in Windows Firewall.
- NodeIds: `ns=<index of urn:kepclone>;s=<Channel>.<Device>.<Tag>`, e.g.
  `Line1.Press1.Recipe[2].Speed`, same scheme as Kepware.
- Per device: `_Connected`, `_Error`, `_ScanTimeMs`.
- Quality: `BadWaitingForInitialData` until first scan, `BadNotConnected` while the PLC
  is unreachable, `BadDeviceFailure` for a tag the PLC refuses to read.
- Secure endpoints use a self-signed cert in `pki/`. Clients (Ignition, UaExpert) must
  trust it on first connect. Passwords are encrypted whenever a `..._SignAndEncrypt`
  policy is enabled, also on the `NoSecurity` endpoint.

## DNP3 RTUs (Brodersen etc.)

Channel `driver: dnp3`, one device per RTU (see `config.example.yaml`). The gateway is a
DNP3 master over TCP: on connect it disables unsolicited reporting for its association,
then every scan reads class 1, 2, 3 and 0 data. Every point in the RTU's class 0 data
appears as `<Channel>.<Device>.<TYPE>.<index>` (`AI.7`, `BI.3`, ...), or under its
friendly name from `names`. Types: BI, DBI, BO, CTR, FCTR, AI, AO. A point with its
ONLINE flag cleared shows `BadDeviceFailure`. DNP3 points are read-only: no controls,
no time sync. Points added on the RTU appear after the gateway reconnects.

## Writes

A client write reaches the PLC only if all hold:
1. the tag path matches the device `writable` patterns,
2. the tag's External Access in the PLC is Read/Write,
3. the client logged in as a configured user (or `anonymous_write: true`),
4. the OPC UA type matches exactly (DINT = Int32, REAL = Float, ...).

The value in OPC UA changes only after the PLC accepts the write.

The desktop window can write too: select a writable tag and press Enter, click
**Write Value…**, or right-click it. Rules 1, 2 and 4 apply (no OPC UA login); the log
names the Windows user (`WRITE ... by window (<user>)`). Arrays take comma-separated values.

## Run as a Windows service (NSSM)

Get `nssm.exe` from https://nssm.cc, then in an admin PowerShell:

```powershell
nssm install GUIConnect D:\Projects\KepClone\.venv\Scripts\python.exe "-m kepclone run D:\Projects\KepClone\config.yaml"
nssm set GUIConnect AppDirectory D:\Projects\KepClone
nssm start GUIConnect
```

With the exe: `nssm install GUIConnect C:\KepClone\GUIConnect.exe "run C:\KepClone\config.yaml"`.

NSSM restarts the gateway if it crashes and stops it with Ctrl+C, which shuts down cleanly.

## Tests

```powershell
.venv\Scripts\python -m pytest
$env:KEPCLONE_PLC_PATH = "192.168.1.10/0"         # bench PLC
$env:KEPCLONE_PLC_WRITE_TAG = "HMI_TestDINT"      # optional, a DINT safe to change
.venv\Scripts\python -m pytest tests\test_live_plc.py -s
$env:KEPCLONE_DNP3_PATH = "10.0.5.20:20000"       # RTU
$env:KEPCLONE_DNP3_OUTSTATION = "10"
.venv\Scripts\python -m pytest tests\test_live_dnp3.py -s
```

## Adding a protocol

Implement `kepclone.driver.Driver` (connect / close / browse / read / write) with
VariantType names as tag types, register it in `kepclone/drivers/__init__.py`. Nothing
in the OPC UA layer changes.

## Known limits (v1)

- All imported tags are polled every scan, subscribed or not.
- Multi-dim arrays are served flattened (1-D).
- BOOL array length assumes the PLC reports dimensions in bits; `test_live_plc.py` checks it.
- AOI/UDT tags the PLC won't read whole show `BadDeviceFailure`; exclude them.
- LWORD/timestamp/other non-basic atomic types are skipped.
- Client certificates are not validated (internal network only).
- DNP3: TCP only, read-only, no event history (latest value only), no Secure Authentication.
