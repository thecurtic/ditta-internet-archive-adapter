"""Strict JSON parsing under the spec's Encoding rules.

A stock parser silently keeps one of two duplicate keys, turns 2.0 into a
float and huge integers into lossy values, and accepts true/false/null. By
the time the result is a dict that evidence is gone, so these rules are
enforced during parsing, through the json module's hooks.
"""

import json

from .errors import Rejected

MAX_INTEGER = 2**53 - 1
# The reference implementation and the vectors use 64 open containers.
MAX_DEPTH = 64


def strict_loads(raw: bytes) -> dict:
    """Parse raw bytes into a message dict, or raise Rejected."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Rejected("not_utf8", str(exc)) from None

    # Checked before parsing so deep input can never reach json's recursion.
    if _max_depth(text) > MAX_DEPTH:
        raise Rejected("too_deep", f"more than {MAX_DEPTH} nested containers")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_int=_checked_int,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except Rejected:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise Rejected("not_json", str(exc)) from None

    if not isinstance(value, dict):
        raise Rejected("not_object", "top-level value is not an object")
    _check_values(value)
    return value


def _max_depth(text: str) -> int:
    """Deepest container nesting, ignoring brackets inside strings."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _object_without_duplicates(pairs: list) -> dict:
    # Keys arrive after escape processing, so "a" and "a" collide here.
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("duplicate_key", f"duplicate key {key!r}")
        result[key] = value
    return result


def _checked_int(literal: str) -> int:
    # "-0" and every negative literal arrive here with their sign.
    if literal.startswith("-"):
        raise Rejected("integer_out_of_range", f"negative integer {literal}")
    # Length check first: Python refuses to convert very long digit strings.
    if len(literal) > 16 or int(literal) > MAX_INTEGER:
        raise Rejected("integer_out_of_range", "integer exceeds 2^53 - 1")
    return int(literal)


def _reject_float(literal: str) -> None:
    raise Rejected("non_integer_number", f"number {literal} is not an integer")


def _reject_constant(literal: str) -> None:
    raise Rejected("wrong_type", f"{literal} is not a protocol value")


def _check_values(value: object) -> None:
    """Reject null, booleans, and strings holding unpaired surrogates."""
    if value is None:
        raise Rejected("null_value", "null appears in the message")
    if isinstance(value, bool):
        raise Rejected("wrong_type", "booleans appear in no protocol field")
    if isinstance(value, str):
        _check_string(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_string(key)
            _check_values(item)
    elif isinstance(value, list):
        for item in value:
            _check_values(item)


def _check_string(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise Rejected("bad_string", "string holds an unpaired surrogate") from None
