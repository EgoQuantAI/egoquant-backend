"""Canonical bytes, commitment and transaction data (no network)."""

import json
import math
from pathlib import Path

import pytest

from egoquant.anchoring.canonical import CanonicalError, canonical_bytes, canonical_text, fixed6
from egoquant.anchoring.commit import commitment, intrinsic_gas, parse_tx_data, tx_data

VECTORS = json.loads((Path(__file__).parent / "vectors.json").read_text(encoding="utf-8"))


def test_plan_worked_example_is_golden():
    v = VECTORS["records"][0]
    assert v["bytes"] == 1089
    assert v["sha256"] == "cf782b6920b49cbf862bf49ee0e04240d9c342594ab5a1b7a943f683402bf008"
    assert v["salt"] == bytes(range(32)).hex()
    assert v["commitment"] == "fae178cabdd0e04269b665c8e57bb7ea3d2f906e2e8c195071ab377969b8c2e9"
    assert v["tx_data"] == "0x45474f5131fae178cabdd0e04269b665c8e57bb7ea3d2f906e2e8c195071ab377969b8c2e9"


@pytest.mark.parametrize("v", VECTORS["records"], ids=lambda v: v["name"])
def test_record_vectors(v):
    raw = canonical_bytes(v["value"])
    assert raw.decode() == v["canonical"]
    c = commitment(bytes.fromhex(v["salt"]), raw)
    assert c.hex() == v["commitment"]
    assert "0x" + tx_data(c).hex() == v["tx_data"]


@pytest.mark.parametrize("v", VECTORS["reports"], ids=lambda v: v["name"])
def test_report_vectors(v):
    assert canonical_text(v["value"], "report") == v["canonical"]


def test_key_order_and_whitespace_do_not_matter():
    a = json.loads('{"b": 1, "a": {"y": [1, 2], "x": "s"}}')
    b = json.loads('{"a":{"x":"s","y":[1,2]},"b":1}')
    assert canonical_bytes(a) == canonical_bytes(b) == b'{"a":{"x":"s","y":[1,2]},"b":1}'


@pytest.mark.parametrize("bad", [{"w": 0.5}, {"w": 1.0}, {"w": math.nan}, {"w": math.inf}, {"é": 1},
                                 {"n": 2**53}, {1: "x"}, {"s": {1, 2}}])
def test_record_mode_refuses_floats_nan_non_ascii_keys(bad):
    with pytest.raises(CanonicalError):
        canonical_bytes(bad)


def test_report_mode_numbers():
    assert canonical_text([1.0, 3, 0.22000000000000006, -0.0, True], "report") == '[1,3,"0.220000",0,true]'
    assert fixed6(0.0078125) == "0.007812"          # an exact binary tie rounds to even
    assert fixed6(0.0078375) != ""                  # never raises on any finite double
    with pytest.raises(CanonicalError):
        canonical_text({"x": math.nan}, "report")


def test_no_trailing_newline_and_utf8():
    raw = canonical_bytes({"t": "✓"})
    assert raw == '{"t":"✓"}'.encode("utf-8") and not raw.endswith(b"\n")


def test_tx_data_is_prefix_plus_32_bytes_and_gas():
    v = VECTORS["records"][0]
    data = tx_data(bytes.fromhex(v["commitment"]))
    assert len(data) == 37 and data[:5] == b"EGOQ1" and data.hex().startswith("45474f5131")
    assert intrinsic_gas(data) == 21_592
    assert parse_tx_data(data) == bytes.fromhex(v["commitment"])
    assert parse_tx_data(b"EGOQ2" + data[5:]) is None and parse_tx_data(data + b"\0") is None
    with pytest.raises(ValueError):
        tx_data(b"\0" * 31)
    with pytest.raises(ValueError):
        commitment(b"\0" * 16, b"x")


def test_salt_changes_the_commitment():
    from egoquant.anchoring.commit import new_salt
    s1, s2 = new_salt(), new_salt()
    assert len(s1) == 32 and s1 != s2
    assert commitment(s1, b"r") != commitment(s2, b"r")


@pytest.mark.parametrize("v", VECTORS["fixed6"], ids=lambda v: repr(v["x"]))
def test_fixed6_vectors(v):
    assert fixed6(v["x"]) == v["s"]
