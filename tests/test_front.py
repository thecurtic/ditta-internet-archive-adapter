"""The DITTA front: every path, every reply signed and conformant."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import FakeSource
from ditta_ia_adapter.front import AnnouncementStore, Gossip, Node, make_server
from ditta_ia_adapter.front.netguard import endpoint_allowed
from ditta_ia_adapter.front.node import NodeSettings
from ditta_ia_adapter.terms import TermStore
from ditta_ia_adapter.wire import NodeKey, accept

NOW = 1790000000
BROKER = NodeKey(bytes.fromhex("371ceb18f5e01877e33489d2c48398114339ed7f77e882acddaed063b313a995"))
PEER = NodeKey(bytes(31) + b"\x07")


class RecordingGossip:
    def __init__(self) -> None:
        self.heard_announcements: list[dict] = []

    def heard(self, announcement: dict) -> None:
        self.heard_announcements.append(announcement)


@pytest.fixture
def node(tmp_path, sample_results):
    source = FakeSource(sample_results)
    built = Node(
        NodeKey.generate(),
        NodeSettings(endpoint="https://example.com/ditta", max_results=5),
        source,
        TermStore(str(tmp_path / "terms.sqlite")),
        AnnouncementStore(str(tmp_path / "peers.sqlite")),
        clock=lambda: NOW,
    )
    built.source = source
    built.gossip = RecordingGossip()
    built.attach_gossip(built.gossip)
    return built


def body(message: dict) -> bytes:
    return json.dumps(message).encode()


def query(**overrides) -> dict:
    message = {"query": "lighthouse keeper", "max_results": 3, "request_id": "q1",
               "pricing_type": "per_query", "max_price": 0}
    message.update(overrides)
    return message


def reply_as(reply, message_type: str) -> dict:
    """Every reply must be a conformant, correctly signed message."""
    message = accept(reply.body, message_type)
    assert "sig" in message
    return message


# --- /query -------------------------------------------------------------------


def test_query_returns_signed_result_set_within_max_results(node) -> None:
    reply = node.handle("/query", body(query()))
    result_set = reply_as(reply, "result_set")
    assert reply.status == 200
    assert result_set["pubkey"] == node.key.pubkey
    assert result_set["request_id"] == "q1"
    assert [r["id"] for r in result_set["results"]] == ["item-0", "item-1", "item-2"]


def test_query_rows_are_capped_by_local_limit(node) -> None:
    node.handle("/query", body(query(max_results=500)))
    assert node.source.searches == [("lighthouse keeper", 5)]


def test_signed_queries_are_accepted(node) -> None:
    signed = BROKER.sign(query(), "query_request")
    assert node.handle("/query", body(signed)).status == 200


def test_bad_signature_is_invalid_signature_with_request_id(node) -> None:
    signed = BROKER.sign(query(), "query_request")
    signed["query"] = "tampered"
    error = reply_as(node.handle("/query", body(signed)), "error")
    assert (error["code"], error["request_id"]) == ("invalid_signature", "q1")


@pytest.mark.parametrize("pricing", [{"pricing_type": "per_result"}])
def test_other_pricing_types_are_unsupported(node, pricing) -> None:
    error = reply_as(node.handle("/query", body(query(**pricing))), "error")
    assert error["code"] == "unsupported_pricing_type"


def test_per_batch_is_unsupported(node) -> None:
    signed = BROKER.sign(query(pricing_type="per_batch", query_count=10), "query_request")
    assert reply_as(node.handle("/query", body(signed)), "error")["code"] == "unsupported_pricing_type"


def test_empty_query_is_rejected(node) -> None:
    error = reply_as(node.handle("/query", body(query(query="  "))), "error")
    assert error["code"] == "rejected"


def test_malformed_body_gets_malformed_without_request_id(node) -> None:
    error = reply_as(node.handle("/query", b"{not json"), "error")
    assert error["code"] == "malformed"
    assert "request_id" not in error


def test_throttled_source_answers_rate_limited_never_empty_results(node) -> None:
    node.source.busy = True
    reply = node.handle("/query", body(query()))
    assert reply.status == 503
    assert reply.headers["Retry-After"] == "30"
    assert reply_as(reply, "error")["code"] == "rate_limited"


def test_served_query_terms_reach_the_store_but_not_the_query(node, tmp_path) -> None:
    node.handle("/query", body(query(query="the Lighthouse keeper")))
    import sqlite3

    rows = sqlite3.connect(tmp_path / "terms.sqlite").execute("SELECT term FROM terms ORDER BY term").fetchall()
    assert rows == [("keeper",), ("lighthouse",)]


# --- /corpus ------------------------------------------------------------------


def test_corpus_statistics_are_signed_format_0(node) -> None:
    node.handle("/query", body(query()))
    store = node._terms
    from ditta_ia_adapter.adapter import TermStats

    store.record("lighthouse", TermStats(100, 1789000000))
    request = BROKER.sign({"request_id": "c1", "statistics_version": 0, "max_terms": 10}, "corpus_statistics_request")
    response = reply_as(node.handle("/corpus", body(request)), "corpus_statistics_response")
    assert response["terms"] == [{"term": "lighthouse", "document_count": 100, "newest": 1789000000}]


def test_corpus_requires_a_signature(node) -> None:
    unsigned = {"request_id": "c1", "statistics_version": 0}
    assert reply_as(node.handle("/corpus", body(unsigned)), "error")["code"] == "malformed"


def test_unknown_statistics_version_is_unsupported_version(node) -> None:
    request = BROKER.sign({"request_id": "c1", "statistics_version": 7}, "corpus_statistics_request")
    assert reply_as(node.handle("/corpus", body(request)), "error")["code"] == "unsupported_version"


# --- announcements and discovery --------------------------------------------------


def peer_announcement(timestamp: int = NOW, key: NodeKey = PEER) -> dict:
    return key.sign(
        {"protocol_version": 0, "endpoint": "https://peer.example.net", "timestamp": timestamp,
         "roles": ["broker"], "pricing": []},
        "announcement",
    )


def test_own_announcement_is_a_free_indexer(node) -> None:
    announcement = accept(body(node.announcement()), "announcement")
    assert announcement["roles"] == ["indexer"]
    assert announcement["pricing"] == [{"type": "per_query", "price": 0}]
    assert announcement["corpus"]["statistics_versions"] == [0]
    assert announcement["endpoint"] == "https://example.com/ditta"


def test_own_announcement_timestamp_strictly_increases(node) -> None:
    first = node.announcement()
    node._terms.set_meta("document_count", 42)
    second = node.announcement()
    assert second["timestamp"] > first["timestamp"]


def test_new_announcement_is_stored_and_relayed_once(node) -> None:
    announcement = peer_announcement()
    assert node.handle("/announce", body(announcement)).body == b""
    assert node.handle("/announce", body(announcement)).status == 200
    assert node.gossip.heard_announcements == [announcement]

    newer = peer_announcement(NOW + 10)
    node.handle("/announce", body(newer))
    assert node.gossip.heard_announcements[-1] == newer


def test_far_future_announcement_is_ignored(node) -> None:
    node.handle("/announce", body(peer_announcement(NOW + 7200)))
    assert node.gossip.heard_announcements == []


def test_bad_announcement_gets_an_error(node) -> None:
    announcement = peer_announcement()
    announcement["endpoint"] = "https://evil.example.net"
    assert reply_as(node.handle("/announce", body(announcement)), "error")["code"] == "invalid_signature"


def test_peers_returns_ourselves_and_held_announcements(node) -> None:
    node.handle("/announce", body(peer_announcement()))
    request = BROKER.sign({"request_id": "p1", "max_peers": 5}, "peer_list_request")
    response = reply_as(node.handle("/peers", body(request)), "peer_list_response")
    assert [p["pubkey"] for p in response["peers"]] == [node.key.pubkey, PEER.pubkey]
    for entry in response["peers"]:
        accept(body(entry), "announcement")  # content unchanged, still verifies


def test_unknown_path_is_rejected(node) -> None:
    assert reply_as(node.handle("/search", b"{}"), "error")["code"] == "rejected"


def test_gossip_relays_to_others_and_introduces_us(tmp_path) -> None:
    store = AnnouncementStore(str(tmp_path / "peers.sqlite"))
    other = peer_announcement(key=NodeKey(bytes(31) + b"\x09"))
    other["endpoint"] = "https://other.example.net"
    store.offer(other)
    gossip = Gossip(store, lambda: {"me": 1}, "me", "ua", ["https://seed.example.net"])

    newcomer = peer_announcement()
    gossip.heard(newcomer)
    queued = []
    while not gossip._queue.empty():
        endpoint, data = gossip._queue.get()
        queued.append((endpoint, json.loads(data)))
    assert queued == [
        ("https://other.example.net", newcomer),
        ("https://seed.example.net", newcomer),
        ("https://peer.example.net", {"me": 1}),
    ]


@pytest.mark.parametrize(
    "endpoint",
    ["http://example.com", "https://127.0.0.1", "https://10.0.0.5:8443", "https://169.254.169.254",
     "https://[::1]", "https://abc.onion", "ftp://example.com", "https://user@example.com", "not a url"],
)
def test_unsafe_endpoints_are_refused(endpoint: str) -> None:
    assert not endpoint_allowed(endpoint)


def test_public_ip_endpoint_is_allowed() -> None:
    assert endpoint_allowed("https://203.0.113.9:8443") is False  # documentation range is not global
    assert endpoint_allowed("https://8.8.8.8")


# --- over real HTTP -----------------------------------------------------------------


def test_http_server_routes_under_the_endpoint_prefix(node) -> None:
    server = make_server(node, "127.0.0.1", 0, "/ditta")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/ditta/query", data=body(query()), method="POST")
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            accept(response.read(), "result_set")

        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/ditta/query")
        assert caught.value.code == 405
        accept(caught.value.read(), "error")
    finally:
        server.shutdown()
        server.server_close()
