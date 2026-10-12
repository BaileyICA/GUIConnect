from types import SimpleNamespace

import pytest

from kepclone.gui import App, parse_value


@pytest.mark.parametrize(
    "text, vtype, count, expected",
    [
        ("on", "Boolean", 0, True),
        (" 0 ", "Boolean", 0, False),
        ("3.25", "Float", 0, 3.25),
        ("-32768", "Int16", 0, -32768),
        ("255", "Byte", 0, 255),
        ("1, 0, true, off", "Boolean", 4, [True, False, True, False]),
        ("[1, 2]", "Int32", 2, [1, 2]),
        (" a b ", "String", 0, " a b "),
    ],
)
def test_parse_value(text, vtype, count, expected):
    assert parse_value(text, vtype, count) == expected


@pytest.mark.parametrize(
    "text, vtype, count",
    [("2", "Boolean", 0), ("1.5", "Int32", 0), ("32768", "Int16", 0), ("-1", "UInt16", 0), ("x", "Float", 0), ("1, 2", "Int32", 3)],
)
def test_parse_value_rejects(text, vtype, count):
    with pytest.raises(ValueError):
        parse_value(text, vtype, count)


def test_children_drill_down():
    from kepclone.gui import _children, _plc_status

    assert _children("A", [5, -1], "Int16") == [("A[0]", 5, "Int16"), ("A[1]", -1, "Int16")]
    bits = _children("A[1]", -1, "Int16")
    assert len(bits) == 16 and all(v for _, v, _ in bits) and bits[15][0] == "A[1].15"
    assert [v for _, v, _ in _children("D", 5, "Int32")][:4] == [True, False, True, False]
    assert _children("F", 1.5, "Float") == [] and _children("B", True, "Boolean") == []
    assert _plc_status({"mode": "REMOTE RUN", "faults": []})[0] == "REMOTE RUN, no faults"
    assert _plc_status({"mode": "RUN", "faults": ["major recoverable"]})[1] == "#c42b1c"


def test_toggle_bit_value():
    ok = ((), "good")
    app = SimpleNamespace(
        info={"Run": ("Boolean", 0, True), "Word": ("Int16", 0, True), "Words": ("Int32", 2, True), "RO": ("Int16", 0, False),
              "Bits": ("Boolean", 4, True), "Stale": ("Boolean", 0, True), "Speed": ("Float", 0, True)},
        rows={"Run": ok, "Word": ok, "Words": ok, "RO": ok, "Bits": ok, "Stale": ((), "bad"), "Speed": ok},
        values={"Run": True, "Word": -32768, "Words": [0, 1 << 31], "RO": 1, "Bits": [True] * 4, "Stale": False, "Speed": 1.0},
    )
    app._target = lambda path: App._target(app, path)
    assert App._bit_value(app, "Run") is True
    assert App._bit_value(app, "Word.15") is True
    assert App._bit_value(app, "Word.0") is False
    assert App._bit_value(app, "Words[1].31") is True
    assert App._bit_value(app, "Words[0].31") is False
    assert App._bit_value(app, "Bits[3]") is True
    for path in ["RO.0", "Bits", "Stale", "Speed", "Speed.0", "Word", "Missing.1", "Words[1]", "Words[2].0",
                 "Words.0", "Bits[4]", "Bits[0].0", "Word[0].1"]:
        assert App._bit_value(app, path) is None, path


def test_write_value_target():
    app = SimpleNamespace(
        info={"Words": ("Int32", 2, True), "Temps": ("Float", 3, True), "RO": ("Float", 3, False), "Run": ("Boolean", 0, True)},
        values={"Words": [0, 5], "Temps": [1.5, 2.5, None], "RO": [1.0] * 3, "Run": None},
    )
    assert App._target(app, "Words") == ("Words", "Int32", 2, [0, 5])
    assert App._target(app, "Words[1]") == ("Words", "Int32", 0, 5)
    assert App._target(app, "Words[1].2") == ("Words", "Boolean", 0, True)
    assert App._target(app, "Temps[2]") == ("Temps", "Float", 0, None)
    assert App._target(app, "Run") == ("Run", "Boolean", 0, None)
    for path in ["RO[0]", "Temps[3]", "Temps[0].1", "Words.1", "Run[0]", "Missing[0]"]:
        assert App._target(app, path) is None, path
