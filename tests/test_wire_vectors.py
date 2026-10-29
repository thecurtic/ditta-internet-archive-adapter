"""Conformance against the spec's test vectors, in both directions."""

import json
from pathlib import Path

import pytest

from ditta_ia_adapter.wire import NodeKey, Rejected, accept, canonical_bytes, message_hash

VECTORS = json.loads((Path(__file__).parent / "vectors" / "vectors.json").read_text())
SECRETS = {key["pubkey"]: bytes.fromhex(key["seckey"]) for key in VECTORS["keys"].values()}


def _raw(vector: dict) -> bytes:
    if vector.get("encoding") == "hex":
        return bytes.fromhex(vector["input"])
    return vector["input"].encode("utf-8")


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda v: v["name"])
def test_valid_vector_is_accepted_and_rederived(vector: dict) -> None:
    raw = json.dumps(vector["message"], ensure_ascii=False).encode("utf-8")
    message = accept(raw, vector["message_type"])

    unsigned = {key: value for key, value in message.items() if key != "sig"}
    assert canonical_bytes(unsigned).decode("utf-8") == vector["canonical"]
    assert message_hash(message, vector["message_type"]).hex() == vector["sha256"]

    if vector["signed"]:
        # The vectors sign with zero aux randomness, so signing reproduces them.
        key = NodeKey(SECRETS[message["pubkey"]])
        resigned = key.sign(message, vector["message_type"], aux=bytes(32))
        assert resigned["sig"] == message["sig"]


@pytest.mark.parametrize("vector", VECTORS["invalid"], ids=lambda v: v["name"])
def test_invalid_vector_is_rejected_for_the_stated_reason(vector: dict) -> None:
    expected = vector["expect"] if isinstance(vector["expect"], list) else [vector["expect"]]
    with pytest.raises(Rejected) as caught:
        accept(_raw(vector), vector["message_type"])
    assert caught.value.code in expected, caught.value.detail
