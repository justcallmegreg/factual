"""FAC-08: canonical JSON (RFC 8785, JCS) with only the standard library."""

from __future__ import annotations

import json
import math

import pytest
import rfc8785
from hypothesis import given, settings
from hypothesis import strategies as st

from app.ledger.canonical import canonicalize


def test_rfc_8785_appendix_example():
    # the RFC's input string, decoded: euro sign, $, U+000F, newline, A, ', B, ", \\, \\, ", /
    text = chr(0x20AC) + "$" + chr(0x0F) + "\n" + "A'B" + '"' + "\\\\" + '"' + "/"
    data = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
        "string": text,
        "literals": [None, True, False],
    }
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"' + chr(0x20AC) + '$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    ).encode("utf-8")
    assert canonicalize(data) == expected


def test_keys_are_sorted_by_utf_16_code_units():
    data = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "\u000a": "Newline",
        "1": "One",
        "\u0080": "Control\u007f",
        "\U0001f602": "Smiley",
        "ö": "Latin Small Letter O With Diaeresis",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "</script>": "Browser Challenge",
    }
    keys = list(json.loads(canonicalize(data)))
    # the smiley (surrogate pair D83D DE02) sorts before U+FB33: UTF-16 order, not code points
    assert keys == ["\n", "\r", "1", "</script>", "\u0080", "ö", "€", "\U0001f602", "דּ"]


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (9007199254740991, "9007199254740991"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (4.5, "4.5"),
        (-2.5e-8, "-2.5e-8"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (0.1 + 0.2, "0.30000000000000004"),
    ],
)
def test_numbers_follow_es6_number_to_string(number, text):
    assert canonicalize(number) == text.encode()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_numbers_are_refused(bad):
    with pytest.raises(ValueError):
        canonicalize(bad)


@pytest.mark.parametrize("big", [2**53, -(2**53), 123456789012345678])
def test_integers_outside_the_safe_range_are_refused(big):
    """Beyond 2^53 a JSON number is not exact as an IEEE double; refuse, like the reference."""
    with pytest.raises(ValueError):
        canonicalize(big)


def test_only_json_types_are_accepted():
    with pytest.raises(TypeError):
        canonicalize({"when": object()})


def test_output_has_no_whitespace_and_is_utf_8():
    out = canonicalize({"b": [1, 2, {"z": "ü", "a": None}], "a": "x"})
    assert out == b'{"a":"x","b":[1,2,{"a":null,"z":"\xc3\xbc"}]}'


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=8), children, max_size=5)
    ),
    max_leaves=25,
)


@settings(max_examples=400, deadline=None)
@given(json_values)
def test_matches_the_reference_implementation(value):
    assert canonicalize(value) == rfc8785.dumps(value)


@settings(max_examples=200, deadline=None)
@given(json_values)
def test_round_trips_through_json(value):
    decoded = json.loads(canonicalize(value))
    assert same_value(decoded, value)


def same_value(left, right) -> bool:
    """Equality where numbers compare as doubles: an integral double such as
    2.519512978150591e+16 prints as digits and reads back as an int."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return float(left) == float(right) and math.isfinite(float(left))
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_value(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_value(left[k], right[k]) for k in left)
    return left == right
