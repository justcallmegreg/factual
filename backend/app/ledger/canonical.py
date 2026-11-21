"""Canonical JSON: the JSON Canonicalization Scheme, RFC 8785 (JCS).

Only the standard library is used, so the same code can ship inside an evidence
pack's verifier. Rules: no whitespace; object keys sorted by their UTF-16 code
units; strings escaped the JSON way with the short escapes and lower-case
`\\u00xx` for other control characters, every other character literal; numbers
printed as ECMAScript's Number.prototype.toString prints them. The same value
therefore always yields the same bytes, whatever produced it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

MAX_SAFE_INTEGER = 2**53 - 1  # ECMAScript Number.MAX_SAFE_INTEGER

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def canonicalize(value: object) -> bytes:
    """Return the canonical UTF-8 bytes of a JSON value."""
    parts: list[str] = []
    _write(value, parts)
    return "".join(parts).encode("utf-8")


def _write(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(f"integer {value} is outside the IEEE 754 safe range")
        out.append(str(value))
    elif isinstance(value, float):
        out.append(format_number(value))
    elif isinstance(value, str):
        out.append(quote(value))
    elif isinstance(value, Mapping):
        out.append("{")
        first = True
        for key in sorted(value, key=_utf16_key):
            if not isinstance(key, str):
                raise TypeError(f"object keys must be strings, not {type(key).__name__}")
            if not first:
                out.append(",")
            first = False
            out.append(quote(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise TypeError(f"{type(value).__name__} is not a JSON type")


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def quote(text: str) -> str:
    """A JSON string literal with JCS escaping."""
    out = ['"']
    for char in text:
        escaped = _SHORT_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def format_number(number: float) -> str:
    """ECMAScript Number.prototype.toString for a finite double."""
    if math.isnan(number) or math.isinf(number):
        raise ValueError("NaN and Infinity cannot be represented in JSON")
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    text = repr(abs(number))  # shortest digits that round-trip, like ECMAScript
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    all_digits = int_part + frac_part
    stripped = all_digits.lstrip("0")
    leading_zeros = len(all_digits) - len(stripped)
    digits = stripped.rstrip("0")
    n = len(int_part) + exponent - leading_zeros  # value = 0.digits × 10^n
    k = len(digits)
    if k == 0:
        return "0"
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = ("+" if e >= 0 else "-") + str(abs(e))
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + exp
    return sign + body
