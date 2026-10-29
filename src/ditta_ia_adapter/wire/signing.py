"""Tagged message hashes and BIP-340 signatures, via coincurve."""

import hashlib
import os
from pathlib import Path

from coincurve import PrivateKey, PublicKeyXOnly

from .canonical import canonical_bytes


def message_hash(message: dict, message_type: str) -> bytes:
    """The tagged hash a signature covers: every field except `sig`."""
    unsigned = {key: value for key, value in message.items() if key != "sig"}
    tag = hashlib.sha256(f"ditta/v0/{message_type}".encode("ascii")).digest()
    return hashlib.sha256(tag + tag + canonical_bytes(unsigned)).digest()


def verify_message(message: dict, message_type: str) -> bool:
    """True when `sig` verifies against the message's own `pubkey`."""
    try:
        pubkey = PublicKeyXOnly(bytes.fromhex(message["pubkey"]))
        signature = bytes.fromhex(message["sig"])
    except ValueError:
        # Hex form is checked by validation; this is a pubkey off the curve.
        return False
    return pubkey.verify(signature, message_hash(message, message_type))


class NodeKey:
    """The node's protocol identity: a secp256k1 key used for DITTA alone."""

    def __init__(self, secret: bytes) -> None:
        self._private = PrivateKey(secret)
        self.pubkey = PublicKeyXOnly.from_secret(secret).format().hex()

    @classmethod
    def generate(cls) -> "NodeKey":
        return cls(os.urandom(32))

    @classmethod
    def load(cls, path: Path) -> "NodeKey":
        """Read a key file holding 64 hex characters."""
        return cls(bytes.fromhex(path.read_text().strip()))

    def save(self, path: Path) -> None:
        """Write the secret as hex to a new file only the owner can read."""
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(self._private.secret.hex() + "\n")

    def sign(self, message: dict, message_type: str, aux: bytes | None = None) -> dict:
        """Return a copy of `message` carrying this node's pubkey and sig.

        `aux` is BIP-340 auxiliary randomness. It defaults to fresh random
        bytes, as BIP-340 recommends; tests pass zeros to reproduce vectors.
        """
        signed = {key: value for key, value in message.items() if key != "sig"}
        signed["pubkey"] = self.pubkey
        digest = message_hash(signed, message_type)
        signature = self._private.sign_schnorr(digest, aux if aux is not None else os.urandom(32))
        signed["sig"] = signature.hex()
        return signed
