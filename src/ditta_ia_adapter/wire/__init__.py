"""The DITTA wire format: strict parsing, validation, canonical bytes, signatures.

Implemented here from `protocol.md` in ditta-spec; the spec's test vectors
(tests/vectors/vectors.json) are the conformance suite for this package.
Nothing here knows about the Internet Archive.
"""

from .canonical import canonical_bytes
from .errors import Rejected
from .parse import strict_loads
from .signing import NodeKey, message_hash, verify_message
from .validate import MESSAGE_TYPES, accept, validate, validate_peer_entries

__all__ = [
    "MESSAGE_TYPES",
    "NodeKey",
    "Rejected",
    "accept",
    "canonical_bytes",
    "message_hash",
    "strict_loads",
    "validate",
    "validate_peer_entries",
    "verify_message",
]
