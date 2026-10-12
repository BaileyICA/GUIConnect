"""Logix driver against fixtures shaped like pycomm3 1.2.16 `LogixDriver.tags` output."""
import pytest
from pycomm3 import Tag

from kepclone.driver import TagDef
from kepclone.drivers.logix import LogixDriver, normalize, tag_def


def atomic(dt, **kw):
    return {"tag_type": "atomic", "data_type": dt, "data_type_name": dt, "offset": 0, **kw}


STRING_DT = {
    "name": "STRING",
    "attributes": ["LEN", "DATA"],
    "internal_tags": {"LEN": atomic("DINT", array=0), "DATA": atomic("SINT", array=82)},
    "string": 82,
}
RECIPE_DT = {
    "name": "Recipe",
    "attributes": ["Name", "Temp", "Flags", "Run", "Steps", "Pos"],
    "internal_tags": {
        "Name": {"tag_type": "struct", "data_type": STRING_DT, "data_type_name": "STRING", "array": 0, "offset": 0},
        "Temp": atomic("REAL", array=0),
        "Flags": atomic("DWORD", array=1),  # BOOL[32] member
        "Run": atomic("BOOL", bit=0),
        "Steps": atomic("DINT", array=3),
        "Pos": atomic("LWORD", array=0),  # unsupported -> skipped
        "ZZZZZZZZZZRecipe0": atomic("SINT", array=0),  # hidden host member, not in attributes
    },
}


def tag(dt, dims=(), access="Read/Write", struct=None):
    t = {
        "tag_type": "struct" if struct else "atomic",
        "data_type": struct or dt,
        "data_type_name": struct["name"] if struct else dt,
        "dim": len(dims),
        "dimensions": list(dims) + [0] * (3 - len(dims)),
        "external_access": access,
    }
    return t


TAGS = {
    "Speed": tag("REAL"),
    "Counts": tag("DINT", (10,)),
    "Grid": tag("INT", (2, 3)),
    "Bits": tag("DWORD", (64,)),
    "RO": tag("DINT", access="Read Only"),
    "Old": tag("DINT", access="Unknown"),
    "Hidden": tag("DINT", access="None"),
    "Rec": tag(None, struct=RECIPE_DT),
    "Recs": tag(None, (2,), struct=RECIPE_DT),
    "Msg": tag(None, struct=STRING_DT),
    "Big": tag("LWORD"),
}

RECIPE_MEMBERS = [
    TagDef("Name", "String"),
    TagDef("Temp", "Float"),
    TagDef("Flags", "Boolean", 32),
    TagDef("Run", "Boolean"),
    TagDef("Steps", "Int32", 3),
]


def test_tag_defs():
    defs = {name: tag_def(name, raw) for name, raw in TAGS.items()}
    assert defs["Speed"] == TagDef("Speed", "Float")
    assert defs["Counts"] == TagDef("Counts", "Int32", 10)
    assert defs["Grid"] == TagDef("Grid", "Int16", 6)  # multi-dim flattened
    assert defs["Bits"] == TagDef("Bits", "Boolean", 64)
    assert defs["RO"].writable is False
    assert defs["Old"].writable is True
    assert defs["Hidden"] is None
    assert defs["Big"] is None
    assert defs["Rec"] == TagDef("Rec", None, 0, RECIPE_MEMBERS)
    assert defs["Recs"] == TagDef("Recs", None, 2, RECIPE_MEMBERS)
    assert defs["Msg"] == TagDef("Msg", "String")


def test_normalize_shapes_pycomm3_values():
    rec = tag_def("Rec", TAGS["Rec"])
    raw = {"Name": "abc", "Temp": 1.5, "Flags": [[True] + [False] * 31], "Run": True, "Steps": [1, 2, 3]}
    assert normalize(rec, raw) == {**raw, "Flags": [True] + [False] * 31}
    assert normalize(TagDef("A", "Int32", 1), 7) == [7]  # pycomm3 unwraps 1-element arrays
    assert normalize(TagDef("B", "Boolean", 4), [1, 0, 1, 0, 1, 1]) == [True, False, True, False]


class FakePlc:
    def __init__(self):
        self.tags = TAGS
        self.reads, self.writes = [], []

    def read(self, *names):
        self.reads.append(names)
        out = [Tag(n, 1.5, "REAL") if n == "Speed" else Tag(n, None, None, "boom") for n in names]
        return out if len(out) > 1 else out[0]

    def write(self, tag_value):
        self.writes.append(tag_value)
        return Tag(tag_value[0], tag_value[1], "X", "denied" if tag_value[0] == "RO" else None)


@pytest.fixture
def driver():
    d = LogixDriver("1.2.3.4")
    d._plc = FakePlc()
    return d


def test_read_requests_full_arrays(driver):
    tags = driver.browse()
    by_name = {t.name: t for t in tags}
    res = driver.read([by_name["Speed"], by_name["Counts"], by_name["Recs"]])
    assert driver._plc.reads == [("Speed", "Counts{10}", "Recs{2}")]
    assert res["Speed"].value == 1.5 and res["Speed"].error is None
    assert res["Counts"].error == "boom"
    assert driver.read([by_name["Speed"]])["Speed"].value == 1.5  # single read returns a bare Tag


def test_write(driver):
    driver.write("Rec.Temp", 2.0)
    driver.write("Counts", [1] * 10)
    assert driver._plc.writes == [("Rec.Temp", 2.0), ("Counts{10}", [1] * 10)]
    with pytest.raises(IOError, match="denied"):
        driver.write("RO", 1)


def test_status(driver):
    info = {"status": bytes([0x60, 0x35]), "keyswitch": "REMOTE RUN", "product_name": "1756-L83E/B",
            "revision": {"major": 33, "minor": 11}, "serial": "00c0ffee"}
    driver._plc.get_plc_info = lambda: info
    driver._plc.info = {"name": "PLCA"}
    st = driver.status()
    assert st["controller"] == '1756-L83E/B, rev 33.11, s/n 00c0ffee, program "PLCA"'
    assert st["mode"] == "REMOTE RUN"
    assert st["faults"] == ["minor recoverable", "major recoverable"]
    info.update(status=bytes([0x00, 0x34]), keyswitch="UNKNOWN")
    assert driver.status()["mode"] == "unknown (status 0x3400)"
