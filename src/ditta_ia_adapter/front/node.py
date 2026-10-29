"""Request handling for the four DITTA paths, independent of HTTP.

`Node.handle(path, raw)` takes a request body and returns an HTTP status,
response bytes, and any extra headers. Every message it sends is signed.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from ..adapter import Source, SourceBusy, SourceError, UnusableQuery
from ..terms.store import TermStore
from ..terms.worker import CORPUS_UPDATED, DOCUMENT_COUNT
from ..wire import NodeKey, Rejected, accept, strict_loads
from .discovery import AnnouncementStore

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 0
STATISTICS_VERSIONS = [0]
# A receiver SHOULD ignore announcements this far ahead of its clock.
FUTURE_TOLERANCE_SECONDS = 3600


@dataclass
class NodeSettings:
    endpoint: str
    languages: list[str] = field(default_factory=lambda: ["en"])
    max_results: int = 50
    max_corpus_terms: int = 5000
    max_peers: int = 500
    reannounce_seconds: float = 6 * 3600
    log_queries: bool = False


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class Node:
    def __init__(
        self,
        key: NodeKey,
        settings: NodeSettings,
        source: Source,
        terms: TermStore,
        peers: AnnouncementStore,
        clock=time.time,
    ) -> None:
        self.key = key
        self._settings = settings
        self._source = source
        self._terms = terms
        self._peers = peers
        self._clock = clock
        self._gossip = None
        self._announcement: dict | None = None
        self._announcement_lock = threading.Lock()

    def attach_gossip(self, gossip) -> None:
        self._gossip = gossip

    def handle(self, path: str, raw: bytes) -> Reply:
        handlers = {
            "/query": self._query,
            "/corpus": self._corpus,
            "/announce": self._announce,
            "/peers": self._peers_request,
        }
        handler = handlers.get(path)
        if handler is None:
            return self.error(404, "rejected", None, "unknown path")
        try:
            return handler(raw)
        except Rejected as rejection:
            code = "invalid_signature" if rejection.code == "bad_signature" else "malformed"
            return self.error(400, code, _readable_request_id(raw), rejection.code)

    # --- paths -----------------------------------------------------------

    def _query(self, raw: bytes) -> Reply:
        request = accept(raw, "query_request")
        request_id = request["request_id"]
        if self._settings.log_queries:
            log.debug("query %r", request["query"])

        # The only option on offer is per_query at price 0, so max_price
        # (never negative) always covers it.
        if request["pricing_type"] != "per_query":
            return self.error(400, "unsupported_pricing_type", request_id, "only per_query is offered")

        rows = min(request["max_results"], self._settings.max_results)
        try:
            results = self._source.search(request["query"], rows)
        except UnusableQuery as reason:
            return self.error(400, "rejected", request_id, str(reason))
        except SourceBusy as busy:
            # Never an empty result set: a broker would read it as no matches.
            reply = self.error(503, "rate_limited", request_id, "the archive asked us to slow down")
            reply.headers["Retry-After"] = str(busy.retry_after)
            return reply
        except SourceError:
            log.warning("archive search failed")
            return self.error(502, "rejected", request_id, "the archive search failed")

        self._terms.note_terms(request["query"].split())
        result_set = {
            "request_id": request_id,
            "results": [
                {"id": r.id, "url": r.url, "title": r.title, "snippet": r.snippet}
                for r in results[: request["max_results"]]
            ],
        }
        return self._signed(200, result_set, "result_set")

    def _corpus(self, raw: bytes) -> Reply:
        request = accept(raw, "corpus_statistics_request")
        if request["statistics_version"] not in STATISTICS_VERSIONS:
            return self.error(400, "unsupported_version", request["request_id"], "only format 0 is served")
        limit = min(request.get("max_terms", self._settings.max_corpus_terms), self._settings.max_corpus_terms)
        response = {
            "request_id": request["request_id"],
            "statistics_version": 0,
            "updated": self._terms.get_meta(CORPUS_UPDATED),
            "terms": [
                {"term": t.term, "document_count": t.document_count, "newest": t.newest}
                for t in self._terms.top_terms(limit)
            ],
        }
        return self._signed(200, response, "corpus_statistics_response")

    def _announce(self, raw: bytes) -> Reply:
        announcement = accept(raw, "announcement")
        too_far_ahead = announcement["timestamp"] > self._clock() + FUTURE_TOLERANCE_SECONDS
        if announcement["pubkey"] == self.key.pubkey or too_far_ahead:
            return Reply(200)
        if self._peers.offer(announcement) and self._gossip:
            self._gossip.heard(announcement)
        return Reply(200)

    def _peers_request(self, raw: bytes) -> Reply:
        request = accept(raw, "peer_list_request")
        limit = min(request.get("max_peers", self._settings.max_peers), self._settings.max_peers)
        peers = [self.announcement()] + self._peers.announcements(limit)
        response = {"request_id": request["request_id"], "peers": peers[:limit]}
        return self._signed(200, response, "peer_list_response")

    # --- our own announcement ---------------------------------------------

    def announcement(self) -> dict:
        """Our signed announcement, re-signed when the summary changes or ages."""
        corpus = {
            "document_count": self._terms.get_meta(DOCUMENT_COUNT),
            "term_count": self._terms.counted_term_count(),
            "updated": self._terms.get_meta(CORPUS_UPDATED),
            "languages": list(self._settings.languages),
            "statistics_versions": STATISTICS_VERSIONS,
        }
        with self._announcement_lock:
            current = self._announcement
            now = int(self._clock())
            fresh = current and now - current["timestamp"] < self._settings.reannounce_seconds
            if fresh and current["corpus"] == corpus:
                return current
            # Each announcement's timestamp is strictly greater than the last.
            timestamp = max(now, current["timestamp"] + 1) if current else now
            self._announcement = self.key.sign(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "endpoint": self._settings.endpoint,
                    "timestamp": timestamp,
                    "roles": ["indexer"],
                    "pricing": [{"type": "per_query", "price": 0}],
                    "corpus": corpus,
                },
                "announcement",
            )
            return self._announcement

    # --- responses --------------------------------------------------------

    def _signed(self, status: int, message: dict, message_type: str) -> Reply:
        body = json.dumps(self.key.sign(message, message_type), ensure_ascii=False, separators=(",", ":"))
        return Reply(status, body.encode("utf-8"), {"Content-Type": "application/json"})

    def error(self, status: int, code: str, request_id: str | None, message: str) -> Reply:
        """A signed Error reply. `message` is for people; `code` drives behaviour."""
        error = {"code": code, "message": message}
        if request_id is not None:
            error["request_id"] = request_id
        return self._signed(status, error, "error")


def _readable_request_id(raw: bytes) -> str | None:
    """The failed request's identifier if it can be read, else None.

    The spec says an Error carries the identifier when the responder could
    read one, and omits it otherwise.
    """
    try:
        request_id = strict_loads(raw).get("request_id")
    except Rejected:
        return None
    if isinstance(request_id, str) and 1 <= len(request_id.encode("utf-8")) <= 64:
        return request_id
    return None
