"""Canonical serialization: one byte sequence per message.

Rules 2-10 of the spec's Canonical serialization section. Rule 1, leaving
out the signature, is the caller's job (see signing.py).
"""

from .parse import MAX_INTEGER

_TWO_CHARACTER_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def canonical_bytes(message: dict) -> bytes:
    """Serialize a message dict to its canonical UTF-8 bytes."""
    return _encode(message).encode("utf-8")


def _encode(value: object) -> str:
    # bool is a subclass of int, so it must be refused before the int case.
    if isinstance(value, bool) or value is None:
        raise ValueError("null and booleans cannot be serialized")
    if isinstance(value, int):
        if not 0 <= value <= MAX_INTEGER:
            raise ValueError(f"integer {value} is outside [0, 2^53 - 1]")
        return str(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        # Sort by UTF-8 bytes, not code points or UTF-16 units (rule 3).
        keys = sorted(value, key=lambda key: key.encode("utf-8"))
        members = (_encode_string(key) + ":" + _encode(value[key]) for key in keys)
        return "{" + ",".join(members) + "}"
    raise ValueError(f"{type(value).__name__} cannot be serialized")


def _encode_string(text: str) -> str:
    # Minimal escaping: quote, backslash and C0 controls only. Non-ASCII
    # and "/" are emitted literally (rules 8-10).
    out = []
    for char in text:
        if char in _TWO_CHARACTER_ESCAPES:
            out.append(_TWO_CHARACTER_ESCAPES[char])
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'
