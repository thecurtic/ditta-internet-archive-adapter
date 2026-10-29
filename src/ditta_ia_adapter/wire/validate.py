"""Protocol validation: is this a conformant message of the stated type?

The message type is always given by the caller, never inferred: on the wire
it is known from the endpoint and the direction of travel.

Checks run in a fixed order so that a message breaking several rules is
reported the way the spec's vectors expect (Conformance section):

1. missing required fields      -> missing_field
2. unknown fields               -> unknown_field (announcement top level exempt)
3. every field but pubkey/sig   -> wrong_type, out_of_range, bad_length
4. pubkey/sig pairing           -> bad_combination
5. pubkey/sig form              -> bad_hex
6. rules across fields          -> bad_enum, duplicate_value, bad_combination
7. signature                    -> bad_signature
"""

from .errors import Rejected
from .parse import strict_loads
from .signing import verify_message

ROLES = ("broker", "indexer")
PRICING_TYPES = ("per_query", "per_result", "per_batch")
_HEX = frozenset("0123456789abcdef")

# Field kinds: "pubkey", "sig", "request_id", "string", "int", "positive"
# (int > 0), "batch_count" (int > 1), "strings", "ints", "objects", "object".
# Each entry is (kind, required).
_SHAPES: dict[str, dict[str, tuple[str, bool]]] = {
    "announcement": {
        "pubkey": ("pubkey", True),
        "protocol_version": ("int", True),
        "endpoint": ("string", True),
        "timestamp": ("int", True),
        "roles": ("strings", True),
        "pricing": ("objects", True),
        "corpus": ("object", False),
        "sig": ("sig", True),
    },
    "peer_list_request": {
        "pubkey": ("pubkey", True),
        "request_id": ("request_id", True),
        "max_peers": ("positive", False),
        "sig": ("sig", True),
    },
    "peer_list_response": {
        "pubkey": ("pubkey", True),
        "request_id": ("request_id", True),
        "peers": ("objects", True),
        "sig": ("sig", False),
    },
    "corpus_statistics_request": {
        "pubkey": ("pubkey", True),
        "request_id": ("request_id", True),
        "statistics_version": ("int", True),
        "max_terms": ("positive", False),
        "sig": ("sig", True),
    },
    "corpus_statistics_response": {
        "pubkey": ("pubkey", True),
        "request_id": ("request_id", True),
        "statistics_version": ("int", True),
        "updated": ("int", True),
        "terms": ("objects", True),
        "sig": ("sig", True),
    },
    "query_request": {
        "pubkey": ("pubkey", False),
        "query": ("string", True),
        "max_results": ("positive", True),
        "request_id": ("request_id", True),
        "pricing_type": ("string", True),
        "query_count": ("batch_count", False),
        "max_price": ("int", True),
        "sig": ("sig", False),
    },
    "payment_challenge": {
        "pubkey": ("pubkey", True),
        "request_id": ("request_id", True),
        "invoice": ("string", True),
        "macaroon": ("string", True),
        "expiry": ("int", True),
        "sig": ("sig", True),
    },
    "result_set": {
        "pubkey": ("pubkey", True),
        "request_id": ("request_id", True),
        "results": ("objects", True),
        "sig": ("sig", False),
    },
    "error": {
        "pubkey": ("pubkey", True),
        "request_id": ("request_id", False),
        "code": ("string", True),
        "message": ("string", False),
        "sig": ("sig", False),
    },
}

# Closed shapes nested inside messages.
_PRICING_ENTRY = {
    "type": ("string", True),
    "price": ("int", True),
    "query_count": ("batch_count", False),
}
_CORPUS = {
    "document_count": ("int", True),
    "term_count": ("int", True),
    "updated": ("int", True),
    "languages": ("strings", True),
    "statistics_versions": ("ints", True),
}
_TERM_ENTRY_V0 = {
    "term": ("string", True),
    "document_count": ("int", True),
    "newest": ("int", True),
}
_RESULT_ENTRY = {
    "id": ("string", True),
    "url": ("string", True),
    "title": ("string", True),
    "snippet": ("string", True),
}

MESSAGE_TYPES = tuple(_SHAPES)


def accept(raw: bytes, message_type: str) -> dict:
    """Parse and validate raw bytes; return the message or raise Rejected."""
    message = strict_loads(raw)
    validate(message, message_type)
    return message


def validate(message: dict, message_type: str) -> list[str]:
    """Raise Rejected unless `message` is conformant for `message_type`.

    Returns the unknown top-level field names of an announcement, which the
    spec says to tolerate and log; the list is empty for every other type.
    """
    shape = _SHAPES[message_type]
    tolerate_unknown = message_type == "announcement"
    unknown = _check_shape(message, shape, tolerate_unknown)

    _check_nested(message, message_type)

    if message_type == "query_request" and ("pubkey" in message) != ("sig" in message):
        raise Rejected("bad_combination", "pubkey and sig are all-or-nothing")
    _check_hex(message, "pubkey", 64)
    _check_hex(message, "sig", 128)

    if message_type == "announcement":
        _check_announcement_rules(message)
    elif message_type == "query_request":
        _check_query_rules(message)

    if "sig" in message and not verify_message(message, message_type):
        raise Rejected("bad_signature", "signature does not verify")
    return unknown


def validate_peer_entries(response: dict) -> list[dict]:
    """The announcements in a validated peer list response that pass.

    A failing entry is discarded without invalidating the rest (spec, Peer
    list response).
    """
    survivors = []
    for entry in response["peers"]:
        try:
            validate(entry, "announcement")
        except Rejected:
            continue
        survivors.append(entry)
    return survivors


def _check_shape(obj: dict, shape: dict, tolerate_unknown: bool = False) -> list[str]:
    for name, (_, required) in shape.items():
        if required and name not in obj:
            raise Rejected("missing_field", f"missing {name}")
    unknown = [name for name in obj if name not in shape]
    if unknown and not tolerate_unknown:
        raise Rejected("unknown_field", f"unknown field {unknown[0]}")
    for name, (kind, _) in shape.items():
        if name in obj and kind not in ("pubkey", "sig"):
            _check_kind(name, obj[name], kind)
    return unknown


def _check_kind(name: str, value: object, kind: str) -> None:
    if kind in ("int", "positive", "batch_count"):
        if not isinstance(value, int):
            raise Rejected("wrong_type", f"{name} must be an integer")
        if kind == "positive" and value < 1:
            raise Rejected("out_of_range", f"{name} must be greater than zero")
        if kind == "batch_count" and value < 2:
            raise Rejected("out_of_range", f"{name} must be greater than one")
    elif kind in ("string", "request_id"):
        if not isinstance(value, str):
            raise Rejected("wrong_type", f"{name} must be a string")
        if kind == "request_id" and not 1 <= len(value.encode("utf-8")) <= 64:
            raise Rejected("bad_length", "request_id must be 1-64 bytes")
    elif kind == "object":
        if not isinstance(value, dict):
            raise Rejected("wrong_type", f"{name} must be an object")
    else:
        element = {"strings": str, "ints": int, "objects": dict}[kind]
        if not isinstance(value, list) or not all(isinstance(v, element) for v in value):
            raise Rejected("wrong_type", f"{name} must be an array of {element.__name__}")


def _check_nested(message: dict, message_type: str) -> None:
    if message_type == "announcement":
        for entry in message["pricing"]:
            _check_shape(entry, _PRICING_ENTRY)
        if "corpus" in message:
            _check_shape(message["corpus"], _CORPUS)
    elif message_type == "corpus_statistics_response" and message["statistics_version"] == 0:
        for entry in message["terms"]:
            _check_shape(entry, _TERM_ENTRY_V0)
    elif message_type == "result_set":
        for entry in message["results"]:
            _check_shape(entry, _RESULT_ENTRY)


def _check_hex(message: dict, name: str, length: int) -> None:
    if name not in message:
        return
    value = message[name]
    if not isinstance(value, str) or len(value) != length or not _HEX.issuperset(value):
        raise Rejected("bad_hex", f"{name} must be {length} lowercase hex characters")


def _check_announcement_rules(message: dict) -> None:
    roles = message["roles"]
    if not roles:
        raise Rejected("out_of_range", "roles must not be empty")
    for role in roles:
        if role not in ROLES:
            raise Rejected("bad_enum", f"unknown role {role!r}")
    if len(set(roles)) != len(roles):
        raise Rejected("duplicate_value", "a role is declared twice")

    seen = set()
    for entry in message["pricing"]:
        pricing_type = entry["type"]
        if pricing_type not in PRICING_TYPES:
            raise Rejected("bad_enum", f"unknown pricing type {pricing_type!r}")
        if (pricing_type == "per_batch") != ("query_count" in entry):
            raise Rejected("bad_combination", "query_count belongs to per_batch only")
        option = (pricing_type, entry.get("query_count"))
        if option in seen:
            raise Rejected("duplicate_value", f"pricing option {option} repeats")
        seen.add(option)

    if "indexer" in roles:
        if "corpus" not in message:
            raise Rejected("bad_combination", "an indexer must carry a corpus summary")
        if not message["pricing"]:
            raise Rejected("bad_combination", "an indexer must advertise pricing")
    else:
        if "corpus" in message:
            raise Rejected("bad_combination", "only an indexer carries a corpus summary")
        if message["pricing"]:
            raise Rejected("bad_combination", "only an indexer advertises pricing")


def _check_query_rules(message: dict) -> None:
    if message["pricing_type"] not in PRICING_TYPES:
        raise Rejected("bad_enum", f"unknown pricing type {message['pricing_type']!r}")
    per_batch = message["pricing_type"] == "per_batch"
    if per_batch != ("query_count" in message):
        raise Rejected("bad_combination", "query_count belongs to per_batch only")
    if per_batch and "sig" not in message:
        raise Rejected("bad_combination", "a per_batch query must be signed")
