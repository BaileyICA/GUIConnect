"""Bench-PLC checks. Skipped unless KEPCLONE_PLC_PATH is set, e.g.

    $env:KEPCLONE_PLC_PATH = "192.168.1.10/0"
    $env:KEPCLONE_PLC_WRITE_TAG = "HMI_TestDINT"   # optional: a DINT that is safe to change
    .venv\\Scripts\\python -m pytest tests\\test_live_plc.py -s
"""
import os
import time

import pytest

from kepclone.drivers.logix import LogixDriver

PATH = os.environ.get("KEPCLONE_PLC_PATH")
WRITE_TAG = os.environ.get("KEPCLONE_PLC_WRITE_TAG")
pytestmark = pytest.mark.skipif(not PATH, reason="set KEPCLONE_PLC_PATH to test against a real PLC")


@pytest.fixture
def plc():
    d = LogixDriver(PATH)
    d.connect()
    yield d
    d.close()


def test_browse_and_read_all(plc):
    tags = plc.browse()
    assert tags, "no tags imported"
    start = time.monotonic()
    results = plc.read(tags)
    elapsed = (time.monotonic() - start) * 1000
    errors = {name: r.error for name, r in results.items() if r.error}
    print(f"\n{len(tags)} tags read in {elapsed:.0f} ms, {len(errors)} errors")
    for name, err in list(errors.items())[:20]:
        print(f"  {name}: {err}")
    for t in tags:  # BOOL arrays: confirm the element count assumption in drivers/logix.py
        if t.type == "Boolean" and t.count and not results[t.name].error:
            assert len(results[t.name].value) == t.count, t.name
    assert len(errors) < len(results)


@pytest.mark.skipif(not WRITE_TAG, reason="set KEPCLONE_PLC_WRITE_TAG to a DINT that is safe to change")
def test_write_roundtrip(plc):
    t = next(t for t in plc.browse() if t.name == WRITE_TAG)
    original = plc.read([t])[t.name].value
    try:
        plc.write(WRITE_TAG, original + 1)
        assert plc.read([t])[t.name].value == original + 1
    finally:
        plc.write(WRITE_TAG, original)
