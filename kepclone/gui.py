"""Desktop window: the gateway runs on a background thread; Tk shows a live snapshot of it."""
from __future__ import annotations

import asyncio
import concurrent.futures
import getpass
import logging
import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font, messagebox, simpledialog, ttk

import yaml
from asyncua import ua

from . import config as config_mod
from .__main__ import _setup_logging, serve
from .opcua_server import INT_BITS, NAMESPACE, split_bit, sub_type

log = logging.getLogger(__name__)
REFRESH_MS = 500
EVENT_ROWS = 2000
DRIVER_NAMES = {"logix": "Allen-Bradley Logix (EtherNet/IP)", "dnp3": "DNP3 master (TCP)"}
RED, GREEN, GREY, AMBER = "#c42b1c", "#107c10", "#767676", "#9d5d00"
WRITE_TIMEOUT_S = 15  # the write queues behind the current poll on the device's I/O thread


class Gateway:
    """serve() on its own thread and event loop. Its state is only read on that loop, via call()."""

    def __init__(self, cfg: config_mod.Config):
        self.cfg = cfg
        self.server = None
        self.devices = []
        self.error: str | None = None
        self._loop = asyncio.new_event_loop()
        self._task = self._loop.create_task(serve(cfg, self._ready))
        self._thread = threading.Thread(target=self._run, name="gateway", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # e.g. OPC UA port already in use
            self.error = str(e) or type(e).__name__
            log.error("gateway stopped: %s", self.error)
        finally:
            self._loop.close()

    def _ready(self, server, devices) -> None:
        self.server, self.devices = server, devices

    def call(self, fn, *args):
        """fn(*args) on the gateway loop; None when the gateway is not running."""
        if not self.running:
            return None

        async def run():
            return fn(*args)

        try:
            return asyncio.run_coroutine_threadsafe(run(), self._loop).result(2)
        except (RuntimeError, concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            return None

    def write(self, key: str, path: str, value) -> None:
        """Write a tag from the window; raises if the gateway, the tag rules or the PLC refuse."""
        if not self.running or self.server is None:
            raise ConnectionError("gateway is not running")
        who = f"window ({getpass.getuser()})"
        fut = asyncio.run_coroutine_threadsafe(self.server.local_write(key, path, value, who), self._loop)
        try:
            fut.result(WRITE_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"no answer within {WRITE_TIMEOUT_S} s; the PLC may still apply the write") from None

    def stop(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._task.cancel)
        except RuntimeError:  # loop already closed
            pass
        self._thread.join(15)

    # ---- snapshots, run on the gateway loop ------------------------------------------

    def overview(self):
        if self.server is None:
            return None
        devices = {
            d.key: (d.connected, d.last_error, d.scan_ms, len(self.server._by_device.get(d.key, {})), d.info)
            for d in self.devices
        }
        clients = []
        bserver = self.server.server.bserver
        for c in bserver.clients if bserver else []:
            session = c.processor.session if c.processor else None
            user = session.user if session else None
            peer = "%s:%s" % tuple(c.peer_name[:2]) if c.peer_name else "?"
            clients.append((peer, "(logging in)" if user is None else user.name or "anonymous"))
        return devices, clients

    def tags(self, key: str):
        if self.server is None:
            return []
        leaves = self.server._by_device.get(key, {}).values()
        return [(lf.path, lf.vtype.name, lf.count, lf.last, lf.ts, lf.writable) for lf in leaves]


class _ToQueue(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(record)


def parse_value(text: str, vtype: str, count: int):
    """Operator text -> tag value; ValueError with a readable message if it does not fit."""
    if count:
        items = text.strip().strip("[]").split(",")
        if len(items) != count:
            raise ValueError(f"expected {count} comma-separated values, got {len(items)}")
        return [parse_value(item, vtype, 0) for item in items]
    if vtype == "String":
        return text
    text = text.strip()
    if vtype == "Boolean":
        bit = {"1": True, "true": True, "on": True, "0": False, "false": False, "off": False}.get(text.lower())
        if bit is None:
            raise ValueError(f"{text!r} is not a bit value; use 1/0, true/false or on/off")
        return bit
    if vtype in ("Float", "Double"):
        return float(text)
    bits = INT_BITS[vtype]
    value = int(text)
    lo, hi = (0, 2**bits - 1) if vtype.startswith("U") or vtype == "Byte" else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
    if not lo <= value <= hi:
        raise ValueError(f"{value} is outside the {vtype} range {lo}..{hi}")
    return value


def _fmt(value, vtype: str) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v, vtype) for v in value[:50]) + (", …]" if len(value) > 50 else "]")
    if vtype == "Float" and isinstance(value, float):
        return f"{value:.7g}"  # REAL is 32-bit; don't show float64 noise
    return str(value)


def _expandable(value, vtype: str) -> bool:
    return isinstance(value, list) or (vtype in INT_BITS and isinstance(value, int))


def _children(path: str, value, vtype: str) -> list[tuple[str, object, str]]:
    """(path, value, type) one level down: the elements of an array, or the bits of an integer."""
    if isinstance(value, list):
        return [(f"{path}[{i}]", v, vtype) for i, v in enumerate(value)]
    if vtype in INT_BITS and isinstance(value, int):  # >> on a negative int keeps two's complement bits
        return [(f"{path}.{b}", bool(value >> b & 1), "Boolean") for b in range(INT_BITS[vtype])]
    return []


def _plc_status(info: dict) -> tuple[str, str]:
    """(text, colour) for a driver.status() dict."""
    mode, faults = info.get("mode", ""), info.get("faults", [])
    if faults:
        return f"{mode}, FAULTED: {', '.join(faults)}", RED
    return (f"{mode}, no faults", GREEN if mode.endswith("RUN") else AMBER) if mode else ("", GREY)


def _address(driver: str, d: config_mod.DeviceConfig) -> str:
    if driver == "dnp3":
        return f"{d.path}  (outstation {d.outstation}, master {d.master})"
    return d.path


def _scrolled(parent, widget_cls, **kw):
    frame = ttk.Frame(parent)
    w = widget_cls(frame, **kw)
    ys = ttk.Scrollbar(frame, orient="vertical", command=w.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=w.xview)
    w.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    w.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, w


def _table(parent, columns: list[tuple[str, str, int]]):
    frame, tv = _scrolled(parent, ttk.Treeview, columns=[c[0] for c in columns], show="headings", selectmode="browse")
    for cid, title, width in columns:
        tv.heading(cid, text=title, anchor="w")
        tv.column(cid, width=width, stretch=cid == columns[-1][0] or cid == "value")
    return frame, tv


def _form(nb: ttk.Notebook, title: str, rows: list[tuple[str, str]]) -> None:
    f = ttk.Frame(nb, padding=12)
    nb.add(f, text=title)
    for i, (label, value) in enumerate(rows):
        ttk.Label(f, text=label + ":").grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
        e = ttk.Entry(f, width=80)
        e.insert(0, value)
        e.state(["readonly"])  # selectable/copyable, not editable
        e.grid(row=i, column=1, sticky="ew")
    f.columnconfigure(1, weight=1)


class App:
    def __init__(self, root: tk.Tk, config_path: str):
        self.root = root
        self.path = Path(config_path).resolve()
        self.cfg: config_mod.Config | None = None
        self.gw: Gateway | None = None
        self.dev_cfg: dict[str, tuple] = {}  # device key -> (ChannelConfig, DeviceConfig)
        self.rows: dict[str, tuple] = {}  # tag path -> (values, tag) shown in the table
        self.values: dict[str, object] = {}  # tag path -> full value, for the details dialog
        self.info: dict[str, tuple] = {}  # tag path -> (vtype, count, writable), for the write dialog
        self.shown_kids: dict[str, tuple] = {}  # element/bit row iid -> (values, tag) shown
        self.shown_key = None
        self.clients: list = []
        self.error_shown: Gateway | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self.qh = _ToQueue(self.log_queue)
        logging.root.addHandler(self.qh)
        root.report_callback_exception = lambda *exc: log.error("GUI error", exc_info=exc)
        self._build()
        self.open_project(self.path)
        self._tick()

    # ---- layout ----------------------------------------------------------------------

    def _build(self) -> None:
        r = self.root
        r.title("GUIConnect")
        r.geometry("1280x800")
        r.minsize(900, 560)
        ttk.Style().configure("Treeview", rowheight=font.nametofont("TkDefaultFont").metrics("linespace") + 6)

        menu = tk.Menu(r)
        m = tk.Menu(menu, tearoff=False)
        m.add_command(label="Open Project…", accelerator="Ctrl+O", command=self.ask_open)
        m.add_command(label="Reload Project", accelerator="F5", command=self.reload)
        m.add_command(label="Edit Project File", command=lambda: self._open_file(self.path))
        m.add_separator()
        m.add_command(label="Open Log Folder", command=lambda: self._open_file(self.path.parent))
        m.add_separator()
        m.add_command(label="Exit", command=self.quit)
        menu.add_cascade(label="File", menu=m)
        m = tk.Menu(menu, tearoff=False)
        m.add_command(label="Settings…", accelerator="Ctrl+,", command=self.show_settings)
        m.add_command(label="Clear Event Log", command=lambda: self.events.delete(*self.events.get_children()))
        menu.add_cascade(label="View", menu=m)
        m = tk.Menu(menu, tearoff=False)
        m.add_command(label="Hash Password…", command=self.hash_password)
        menu.add_cascade(label="Tools", menu=m)
        m = tk.Menu(menu, tearoff=False)
        m.add_command(label="About GUIConnect", command=self.about)
        menu.add_cascade(label="Help", menu=m)
        r.config(menu=menu)
        r.bind_all("<Control-o>", lambda e: self.ask_open())
        r.bind_all("<F5>", lambda e: self.reload())
        r.bind_all("<Control-comma>", lambda e: self.show_settings())
        r.protocol("WM_DELETE_WINDOW", self.quit)

        self.status = tk.StringVar(value="No project loaded")
        self.status_label = ttk.Label(r, textvariable=self.status, relief="sunken", anchor="w", padding=(6, 2))
        self.status_label.pack(side="bottom", fill="x")

        outer = ttk.PanedWindow(r, orient="vertical")
        outer.pack(fill="both", expand=True, padx=6, pady=6)
        top = ttk.PanedWindow(outer, orient="horizontal")
        outer.add(top, weight=4)

        # Project tree: channels -> devices
        left, self.tree = _scrolled(top, ttk.Treeview, columns=("state",), selectmode="browse")
        self.tree.heading("#0", text="Project", anchor="w")
        self.tree.heading("state", text="State", anchor="w")
        self.tree.column("#0", width=200)
        self.tree.column("state", width=100, stretch=False)
        for tag, color in (("up", GREEN), ("down", RED), ("idle", GREY)):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._refresh())
        self.tree.bind("<Double-1>", lambda e: self.change_address())
        top.add(left, weight=1)

        right = ttk.Frame(top)
        top.add(right, weight=4)
        info = ttk.LabelFrame(right, text="Connection", padding=8)
        info.pack(fill="x")
        self.fields = {}
        names = ("Device", "Driver", "Address", "Status", "Scan time", "Tags", "PLC status", "Controller", "Last error")
        for i, name in enumerate(names):
            row, col = divmod(i, 2)
            ttk.Label(info, text=name + ":", foreground=GREY).grid(row=row, column=col * 2, sticky="w", padx=(0, 8), pady=1)
            var = self.fields[name] = tk.StringVar()
            lbl = ttk.Label(info, textvariable=var)
            lbl.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 24), columnspan=3 if name == "Last error" else 1)
            if name == "Status":
                self.status_value = lbl
            if name == "PLC status":
                self.plc_value = lbl
        ttk.Button(info, text="Change Address…", command=self.change_address).grid(row=1, column=4, sticky="e")
        info.columnconfigure(1, weight=1)
        info.columnconfigure(3, weight=1)

        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(8, 4))
        ttk.Label(bar, text="Filter:").pack(side="left")
        self.filter = tk.StringVar()
        self.filter.trace_add("write", lambda *a: self._refresh(force=True))
        ttk.Entry(bar, textvariable=self.filter, width=32).pack(side="left", padx=6)
        self.count = tk.StringVar()
        ttk.Label(bar, textvariable=self.count, foreground=GREY).pack(side="right")
        self.write_btn = ttk.Button(bar, text="Write Value…", command=self.write_value)
        self.write_btn.pack(side="right", padx=12)
        self.toggle_btn = ttk.Button(bar, text="Toggle Bit", command=self.toggle_bit)
        self.toggle_btn.pack(side="right")

        frame, self.table = _table(right, [
            ("type", "Data Type", 100), ("value", "Value", 260),
            ("quality", "Quality", 150), ("time", "Timestamp", 170), ("access", "Access", 90),
        ])
        self.table.configure(show="tree headings")  # expand arrays to elements, integers to bits
        self.table.heading("#0", text="Tag", anchor="w")
        self.table.column("#0", width=260)
        self.table.tag_configure("bad", foreground=RED)
        self.table.tag_configure("wait", foreground=GREY)
        self.table.bind("<Double-1>", lambda e: "break" if self.tag_details() else None)
        # <<TreeviewOpen>> fires before the row opens; fill it once it has
        self.table.bind("<<TreeviewOpen>>", lambda e: self.root.after_idle(self._refresh_tags, False))
        self.table.bind("<Return>", lambda e: self.write_value())
        self.table.bind("<<TreeviewSelect>>", lambda e: self._update_write_btn())
        self.tag_menu = tk.Menu(self.table, tearoff=False)
        self.tag_menu.add_command(label="Write Value…", command=self.write_value)
        self.tag_menu.add_command(label="Toggle Bit", command=self.toggle_bit)
        self.tag_menu.add_command(label="Details…", command=self.tag_details)
        self.table.bind("<Button-3>", self._tag_menu)
        frame.pack(fill="both", expand=True)

        self.nb = ttk.Notebook(outer)
        outer.add(self.nb, weight=1)
        frame, self.events = _table(self.nb, [
            ("time", "Time", 170), ("level", "Level", 80), ("source", "Source", 200), ("message", "Event", 600),
        ])
        self.events.tag_configure("WARNING", foreground=AMBER)
        self.events.tag_configure("ERROR", foreground=RED)
        self.events.tag_configure("CRITICAL", foreground=RED)
        self.nb.add(frame, text="Event Log")
        self.client_tab, self.client_table = _table(self.nb, [("peer", "Client address", 220), ("user", "User", 300)])
        self.nb.add(self.client_tab, text="OPC UA Clients (0)")

    # ---- project ---------------------------------------------------------------------

    def ask_open(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Open Project", initialdir=self.path.parent,
            filetypes=[("GUIConnect project", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if path:
            self.open_project(path)

    def reload(self) -> None:
        self.open_project(self.path)

    def open_project(self, path) -> None:
        path = Path(path).resolve()
        try:
            cfg = config_mod.load(path)
        except (OSError, config_mod.ConfigError) as e:
            messagebox.showerror("GUIConnect", f"Cannot load project {path}:\n\n{e}", parent=self.root)
            return  # keep serving the project that is already running, if any
        self._stop_gateway()
        self.path, self.cfg = path, cfg
        for h in logging.root.handlers[:]:  # the log file lives next to the project file
            if h is not self.qh:
                logging.root.removeHandler(h)
                h.close()
        _setup_logging(cfg, verbose=False, console=False)
        self.root.title(f"GUIConnect - {path}")
        self.dev_cfg = {f"{ch.name}.{d.name}": (ch, d) for ch in cfg.channels for d in ch.devices}
        self.tree.delete(*self.tree.get_children())
        for ch in cfg.channels:
            self.tree.insert("", "end", "ch:" + ch.name, text=ch.name, values=(ch.driver,), open=True)
            for d in ch.devices:
                self.tree.insert("ch:" + ch.name, "end", f"{ch.name}.{d.name}", text=d.name, values=("Starting",), tags=("idle",))
        if self.dev_cfg:
            self.tree.selection_set(next(iter(self.dev_cfg)))
        log.info("project %s loaded", path)
        self.gw = Gateway(cfg)

    def _stop_gateway(self) -> None:
        if self.gw:
            self.status.set("Stopping gateway…")
            self.root.update_idletasks()
            self.gw.stop()
            self.gw = None

    def _open_file(self, path: Path) -> None:
        try:
            os.startfile(path)
        except OSError:  # no app associated with .yaml
            subprocess.Popen(["notepad.exe", str(path)])

    def quit(self) -> None:
        if self.gw and self.gw.running and not messagebox.askokcancel(
            "Exit GUIConnect", "Stop the gateway and exit?\n\nOPC UA clients will be disconnected.", parent=self.root
        ):
            return
        self._stop_gateway()
        self.root.destroy()

    # ---- refresh ---------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            self._drain_log()
            self._refresh()
        finally:
            self.root.after(REFRESH_MS, self._tick)

    def _drain_log(self) -> None:
        at_bottom = self.events.yview()[1] >= 0.999
        last = None
        while True:
            try:
                rec = self.log_queue.get_nowait()
            except queue.Empty:
                break
            when = datetime.fromtimestamp(rec.created).strftime("%Y-%m-%d %H:%M:%S")
            msg = rec.getMessage().splitlines()[0] if rec.getMessage() else ""
            last = self.events.insert("", "end", values=(when, rec.levelname, rec.name, msg), tags=(rec.levelname,))
        children = self.events.get_children()
        if len(children) > EVENT_ROWS:
            self.events.delete(*children[: len(children) - EVENT_ROWS])
        if last and at_bottom:
            self.events.see(last)

    def _refresh(self, force: bool = False) -> None:
        gw = self.gw
        snap = gw.call(gw.overview) if gw else None
        devices, clients = snap or ({}, [])

        for key in self.dev_cfg:
            s = devices.get(key)
            state, tag = ("Connected", "up") if s and s[0] else ("Down", "down") if s else ("Stopped", "idle")
            if gw and gw.running and not snap:
                state = "Starting"
            self.tree.item(key, values=(state,), tags=(tag,))

        if clients != self.clients:
            self.client_table.delete(*self.client_table.get_children())
            for c in clients:
                self.client_table.insert("", "end", values=c)
            self.clients = clients
            self.nb.tab(self.client_tab, text=f"OPC UA Clients ({len(clients)})")

        if gw is None:
            text, color = "No project loaded", GREY
        elif gw.error:
            text, color = f"Gateway stopped: {gw.error}", RED
            if self.error_shown is not gw:
                self.error_shown = gw
                messagebox.showerror("GUIConnect", f"The gateway stopped:\n\n{gw.error}", parent=self.root)
        elif not snap:
            text, color = "Starting…", GREY
        else:
            up = sum(1 for s in devices.values() if s[0])
            text = f"OPC UA server: {gw.cfg.server.endpoint}     Devices connected: {up}/{len(devices)}     Clients: {len(clients)}"
            color = GREEN if up == len(devices) else AMBER
        self.status.set(text)
        self.status_label.configure(foreground=color)

        self._refresh_device(devices)
        self._refresh_tags(force)

    def _selected_device(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] in self.dev_cfg else None

    def _refresh_device(self, devices: dict) -> None:
        sel = self.tree.selection()
        f = self.fields
        for var in f.values():
            var.set("")
        if not sel:
            return
        if sel[0].startswith("ch:"):
            ch = next(c for c in self.cfg.channels if "ch:" + c.name == sel[0])
            f["Device"].set(f"{ch.name}  (channel, {len(ch.devices)} devices)")
            f["Driver"].set(DRIVER_NAMES.get(ch.driver, ch.driver))
            return
        ch, d = self.dev_cfg[sel[0]]
        s = devices.get(sel[0])
        f["Device"].set(sel[0])
        f["Driver"].set(DRIVER_NAMES.get(ch.driver, ch.driver))
        f["Address"].set(_address(ch.driver, d))
        f["Status"].set("Connected" if s and s[0] else "Not connected" if s else "Stopped")
        self.status_value.configure(foreground=GREEN if s and s[0] else RED)
        f["Scan time"].set(f"{s[2]:.0f} ms (configured {d.scan_ms} ms)" if s else f"configured {d.scan_ms} ms")
        f["Tags"].set(str(s[3]) if s else "")
        f["Last error"].set((s[1] if s else "") or "-")
        info = s[4] if s else {}
        text, color = _plc_status(info)
        f["PLC status"].set(text)
        self.plc_value.configure(foreground=color)
        f["Controller"].set(info.get("controller", ""))

    def _refresh_tags(self, force: bool) -> None:
        key = self._selected_device()
        tags = (self.gw.call(self.gw.tags, key) if key and self.gw else None) or []
        needle = self.filter.get().strip().lower()
        rows, values, info = {}, {}, {}
        for path, vtype, count, last, ts, writable in tags:
            if needle and needle not in path.lower():
                continue
            value, code = last or (None, ua.StatusCodes.BadWaitingForInitialData)
            good = code == ua.StatusCodes.Good
            rows[path] = (
                (
                    path,
                    vtype + (f"[{count}]" if count else ""),
                    _fmt(value, vtype) if good else "",
                    ua.StatusCode(code).name,
                    ts.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if ts else "",
                    "Read/Write" if writable else "Read Only",
                ),
                "good" if good else "wait" if code == ua.StatusCodes.BadWaitingForInitialData else "bad",
            )
            values[path] = value
            info[path] = (vtype, count, writable)
        if force or key != self.shown_key or rows.keys() != self.rows.keys():
            self.table.delete(*self.table.get_children())
            self.shown_kids = {}
            for path, (vals, tag) in rows.items():
                self.table.insert("", "end", iid=path, text=path, values=vals[1:], tags=(tag,))
        else:
            for path, row in rows.items():
                if self.rows[path] != row:
                    self.table.item(path, values=row[0][1:], tags=(row[1],))
        for path, (vals, tag) in rows.items():
            self._sync_children(path, values[path] if tag == "good" else None, info[path][0], vals[3:], tag)
        self.rows, self.values, self.info, self.shown_key = rows, values, info, key
        self._update_write_btn()
        self.count.set(f"{len(rows)} of {len(tags)} tags" if needle else f"{len(tags)} tags")

    def _sync_children(self, iid: str, value, vtype: str, rest: tuple, tag: str) -> None:
        """Rows under an array or integer row; built only while it is open, so big arrays cost nothing."""
        tv = self.table
        if not _expandable(value, vtype):
            kids = []
        elif not tv.item(iid, "open"):
            if not tv.get_children(iid):
                tv.insert(iid, "end", iid + " ")  # placeholder, so the row shows an expand arrow
            return
        else:
            kids = _children(iid, value, vtype)
        if list(tv.get_children(iid)) != [k[0] for k in kids]:
            tv.delete(*tv.get_children(iid))
            for k, _, _ in kids:
                tv.insert(iid, "end", iid=k, text=k)
        for k, v, t in kids:
            row = ((t, _fmt(v, t), *rest), tag)
            if self.shown_kids.get(k) != row:
                tv.item(k, values=row[0], tags=(tag,))
                self.shown_kids[k] = row
            self._sync_children(k, v, t, rest, tag)

    # ---- dialogs ---------------------------------------------------------------------

    def change_address(self) -> None:
        key = self._selected_device()
        if not key:
            return
        ch, d = self.dev_cfg[key]
        hint = "host or host:port, e.g. 10.0.5.20:20000" if ch.driver == "dnp3" else "IP/slot, e.g. 192.168.1.10/0"
        new = simpledialog.askstring(
            "Change Address",
            f"Address of {key}\n({hint}).\n\nSaved to the project file; the gateway restarts to apply it.",
            initialvalue=d.path, parent=self.root,
        )
        new = (new or "").strip()
        if not new or new == d.path:
            return
        try:
            config_mod.set_device_path(self.path, ch.name, d.name, new)
        except (OSError, yaml.YAMLError, config_mod.ConfigError) as e:
            messagebox.showerror("Change Address", f"Address not changed:\n\n{e}", parent=self.root)
            return
        log.info("%s address changed from %s to %s", key, d.path, new)
        self.reload()
        if key in self.dev_cfg:
            self.tree.selection_set(key)

    def tag_details(self) -> bool:
        path = self.table.focus()
        if not path or path not in self.rows:
            return False
        _, vtype, _, quality, ts, access = self.rows[path][0]
        ns = self.gw.server.ns if self.gw and self.gw.server else "?"
        value = repr(self.values.get(path))
        if len(value) > 4000:
            value = value[:4000] + " …"
        messagebox.showinfo(
            path,
            f"NodeId: ns={ns};s={self.shown_key}.{path}\nData type: {vtype}\nAccess: {access}\n"
            f"Quality: {quality}\nTimestamp: {ts}\n\nValue:\n{value}",
            parent=self.root,
        )
        return True

    def _update_write_btn(self) -> None:
        path = self.table.focus()
        self.write_btn.state(["!disabled"] if self._target(path) else ["disabled"])
        self.toggle_btn.state(["!disabled"] if self._bit_value(path) is not None else ["disabled"])

    def _target(self, path: str) -> tuple[str, str, int, object] | None:
        """(tag, type, array size, current value) of a writable tag row, array element or integer bit; else None."""
        if not path:  # no row focused
            return None
        base, idx, bit = (path, None, None) if path in self.info else split_bit(path)
        info = self.info.get(base)
        vtype = sub_type(info[0], info[1], idx, bit) if info and info[2] else None
        if not vtype:
            return None
        value = self.values.get(base)
        if value is not None and idx is not None:
            value = value[idx]
        if value is not None and bit is not None:
            value = bool(value >> bit & 1)
        return base, vtype, info[1] if idx is None and bit is None else 0, value

    def _bit_value(self, path: str) -> bool | None:
        """Current value of a writable bit (Boolean tag, BOOL array element, integer bit); None if not toggleable."""
        t = self._target(path)
        if not t or t[1] != "Boolean" or t[2] or self.rows[t[0]][1] != "good":
            return None
        return bool(t[3])

    def _tag_menu(self, event) -> None:
        row = self.table.identify_row(event.y)
        if not row:
            return
        self.table.selection_set(row)
        self.table.focus(row)
        self.tag_menu.entryconfigure("Write Value…", state="normal" if self._target(row) else "disabled")
        self.tag_menu.entryconfigure("Toggle Bit", state="normal" if self._bit_value(row) is not None else "disabled")
        self.tag_menu.tk_popup(event.x_root, event.y_root)

    def toggle_bit(self) -> None:
        path = self.table.focus()
        current = self._bit_value(path)
        if current is not None and self.gw:
            self._write(self.shown_key, path, not current)

    def write_value(self) -> None:
        path, key = self.table.focus(), self.shown_key
        if not path or not self.gw:
            return
        target = self._target(path)
        if not target:
            messagebox.showinfo(
                "Write Value", f"{path} is read-only: it does not match the device's writable patterns, "
                "or its External Access in the PLC is not Read/Write.", parent=self.root,
            )
            return
        _, vtype, count, current = target
        if isinstance(current, list):
            initial = ", ".join(map(str, current))
        else:
            initial = "" if current is None else str(current)
        hint = "1/0, true/false or on/off" if vtype == "Boolean" else vtype
        if count:
            hint = f"{count} comma-separated values, each {hint}"
        text = simpledialog.askstring(
            "Write Value", f"{key}.{path}\n\nNew value ({hint}):", initialvalue=initial, parent=self.root
        )
        if text is None:
            return
        try:
            value = parse_value(text, vtype, count)
        except ValueError as e:
            messagebox.showerror("Write Value", f"Not written: {e}", parent=self.root)
            return
        self._write(key, path, value)

    def _write(self, key: str, path: str, value) -> None:
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            self.gw.write(key, path, value)
        except Exception as e:
            messagebox.showerror("Write Value", f"Write to {path} failed:\n\n{e}", parent=self.root)
        finally:
            self.root.config(cursor="")
        self._refresh()

    def show_settings(self) -> None:
        if not self.cfg:
            return
        cfg, s = self.cfg, self.cfg.server
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.geometry(f"1000x480+{self.root.winfo_rootx() + 60}+{self.root.winfo_rooty() + 40}")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        _form(nb, "OPC UA Server", [
            ("Endpoint", s.endpoint),
            ("Security policies", ", ".join(s.security)),
            ("Namespace URI", NAMESPACE),
            ("Certificate folder", str(cfg.base_dir / s.pki_dir)),
            ("Anonymous clients may connect", "Yes" if s.anonymous else "No"),
            ("Anonymous clients may write", "Yes" if s.anonymous_write else "No"),
            ("Users", ", ".join(s.users) or "(none)"),
        ])
        frame, tv = _table(nb, [
            ("channel", "Channel", 100), ("device", "Device", 110), ("driver", "Driver", 80),
            ("address", "Address", 150), ("scan", "Scan (ms)", 70), ("include", "Include", 110),
            ("exclude", "Exclude", 90), ("writable", "Writable", 110), ("options", "Options", 200),
        ])
        for ch in cfg.channels:
            for d in ch.devices:
                if ch.driver == "dnp3":
                    options = f"outstation {d.outstation}, master {d.master}, {len(d.names)} named points"
                else:
                    options = f"program tags: {'yes' if d.program_tags else 'no'}"
                tv.insert("", "end", values=(
                    ch.name, d.name, ch.driver, d.path, d.scan_ms, ", ".join(d.include),
                    ", ".join(d.exclude), ", ".join(d.writable), options,
                ))
        nb.add(frame, text="Devices")
        _form(nb, "Project", [
            ("Project file", str(self.path)),
            ("Log file", str(cfg.base_dir / "kepclone.log")),
            ("Refresh rate (window)", f"{REFRESH_MS} ms"),
        ])
        bar = ttk.Frame(win, padding=(10, 4, 10, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="Settings come from the project file. Edit it, then reload to apply.", foreground=GREY).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="Reload Project", command=lambda: (win.destroy(), self.reload())).pack(side="right", padx=6)
        ttk.Button(bar, text="Edit Project File", command=lambda: self._open_file(self.path)).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())

    def hash_password(self) -> None:
        pw = simpledialog.askstring("Hash Password", "Password:", show="*", parent=self.root)
        if not pw:
            return
        if pw != simpledialog.askstring("Hash Password", "Repeat password:", show="*", parent=self.root):
            messagebox.showerror("Hash Password", "Passwords differ.", parent=self.root)
            return
        h = config_mod.hash_password(pw)
        self.root.clipboard_clear()
        self.root.clipboard_append(h)
        messagebox.showinfo(
            "Hash Password",
            f'Copied to the clipboard. Add it under server.users in the project file:\n\n    username: "{h}"',
            parent=self.root,
        )

    def about(self) -> None:
        messagebox.showinfo(
            "About GUIConnect",
            "GUIConnect\n\nAllen-Bradley Logix and DNP3 to OPC UA gateway.\n\n"
            "Double-click a tag to see its full value and NodeId.\n"
            "Right-click a writable tag, or press Enter, to write a new value.\n"
            "Expand an array or integer tag to see its elements and bits.\n"
            "Double-click a device to change its address.",
            parent=self.root,
        )


def run_gui(config_path: str) -> int:
    try:  # crisp text on scaled Windows displays
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (ImportError, AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        root.iconbitmap(default=str(Path(__file__).with_name("kepclone.ico")))
    except tk.TclError:  # icon file missing; keep Tk's default
        pass
    App(root, config_path)
    root.mainloop()
    return 0
