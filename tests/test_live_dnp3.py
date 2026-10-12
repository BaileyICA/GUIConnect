"""Live RTU check. Skipped unless KEPCLONE_DNP3_PATH and KEPCLONE_DNP3_OUTSTATION are set:

    $env:KEPCLONE_DNP3_PATH = "10.0.5.20:20000"
    $env:KEPCLONE_DNP3_OUTSTATION = "10"
    $env:KEPCLONE_DNP3_MASTER = "1"      # optional, default 1
    .venv\\Scripts\\python -m pytest tests\\test_live_dnp3.py -s
"""
import os
import time

import pytest

from kepclone.drivers.dnp3 import DEFAULT_PORT, Dnp3Driver

PATH = os.environ.get("KEPCLONE_DNP3_PATH")
OUTSTATION = os.environ.get("KEPCLONE_DNP3_OUTSTATION")
pytestmark = pytest.mark.skipif(not (PATH and OUTSTATION), reason="set KEPCLONE_DNP3_PATH and KEPCLONE_DNP3_OUTSTATION")


def test_integrity_poll():
    host, _, port = PATH.partition(":")
    d = Dnp3Driver(host, int(port or DEFAULT_PORT), int(OUTSTATION), int(os.environ.get("KEPCLONE_DNP3_MASTER", "1")))
    d.connect()
    try:
        tags = d.browse()
        start = time.monotonic()
        results = d.read(tags)
        print(f"\nscan {(time.monotonic() - start) * 1000:.0f} ms")
        for t in tags:
            values = results[t.name].value
            shown = list(values.items())[:10] if isinstance(values, dict) else values
            print(f"  {t.name}: {len(t.members) or 1} points, first: {shown}")
        assert tags, "RTU reported no points in its class 0 data"
    finally:
        d.close()
