"""Discovery: hold other nodes' announcements, relay them, announce ourselves.

The announcement store is a separate SQLite file from the term store and is
never joined to it. It holds only what nodes publish about themselves.
"""

import json
import logging
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from .netguard import endpoint_allowed

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    pubkey    TEXT PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    endpoint  TEXT NOT NULL,
    body      TEXT NOT NULL
);
"""


class AnnouncementStore:
    """The newest valid announcement per pubkey (spec: supersession)."""

    def __init__(self, path: str, max_entries: int = 10000) -> None:
        self._max = max_entries
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    def offer(self, announcement: dict) -> bool:
        """Keep a validated announcement if it is new; True if it was kept.

        An equal or older timestamp changes nothing: with two differing
        announcements at one timestamp the spec lets us keep either.
        """
        pubkey, timestamp = announcement["pubkey"], announcement["timestamp"]
        body = json.dumps(announcement, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT timestamp FROM announcements WHERE pubkey = ?", (pubkey,)
            ).fetchone()
            if row and row[0] >= timestamp:
                return False
            if not row and self._count() >= self._max:
                return False
            self._db.execute(
                "INSERT INTO announcements (pubkey, timestamp, endpoint, body) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(pubkey) DO UPDATE SET timestamp = excluded.timestamp, "
                "endpoint = excluded.endpoint, body = excluded.body",
                (pubkey, timestamp, announcement["endpoint"], body),
            )
        return True

    def announcements(self, limit: int | None = None) -> list[dict]:
        """Held announcements, newest first, content unchanged."""
        with self._lock:
            rows = self._db.execute(
                "SELECT body FROM announcements ORDER BY timestamp DESC LIMIT ?",
                (-1 if limit is None else limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def endpoints(self) -> dict[str, str]:
        """pubkey -> endpoint for every held announcement."""
        with self._lock:
            return dict(self._db.execute("SELECT pubkey, endpoint FROM announcements").fetchall())

    def _count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]

    def close(self) -> None:
        self._db.close()


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    # A redirect could point anywhere, including the addresses netguard refuses.
    def redirect_request(self, *args, **kwargs):
        return None


class Gossip:
    """Outbound announcement traffic: a bounded queue posted at a bounded rate."""

    def __init__(
        self,
        store: AnnouncementStore,
        own_announcement,  # callable returning our current signed announcement
        own_pubkey: str,
        user_agent: str,
        seed_endpoints: list[str],
        reannounce_seconds: float = 6 * 3600,
        min_post_interval: float = 1.0,
        allow_private: bool = False,
        timeout_seconds: float = 10,
        max_queued: int = 1000,
    ) -> None:
        self._store = store
        self._own = own_announcement
        self._own_pubkey = own_pubkey
        self._user_agent = user_agent
        self._seeds = list(seed_endpoints)
        self._reannounce = reannounce_seconds
        self._interval = min_post_interval
        self._allow_private = allow_private
        self._timeout = timeout_seconds
        self._queue: queue.Queue = queue.Queue(maxsize=max_queued)
        self._opener = urllib.request.build_opener(_NoRedirects)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for target, name in ((self._post_loop, "gossip-post"), (self._announce_loop, "gossip-announce")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def heard(self, announcement: dict) -> None:
        """A new announcement arrived: relay it, and introduce ourselves."""
        body = _encode(announcement)
        for endpoint in self._known_endpoints(exclude={announcement["pubkey"]}):
            self._enqueue(endpoint, body)
        self._enqueue(announcement["endpoint"], _encode(self._own()))

    def announce_everywhere(self) -> None:
        body = _encode(self._own())
        for endpoint in self._known_endpoints():
            self._enqueue(endpoint, body)

    def _known_endpoints(self, exclude: set[str] = frozenset()) -> list[str]:
        held = {pk: ep for pk, ep in self._store.endpoints().items() if pk not in exclude}
        held.pop(self._own_pubkey, None)
        return sorted(set(held.values()) | set(self._seeds))

    def _enqueue(self, endpoint: str, body: bytes) -> None:
        try:
            self._queue.put_nowait((endpoint, body))
        except queue.Full:
            log.warning("gossip queue full; dropping a post")

    def _announce_loop(self) -> None:
        while not self._stop.is_set():
            self.announce_everywhere()
            self._stop.wait(self._reannounce)

    def _post_loop(self) -> None:
        while not self._stop.is_set():
            try:
                endpoint, body = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            self.post(endpoint, body)
            self._stop.wait(self._interval)

    def post(self, endpoint: str, body: bytes) -> None:
        """POST one announcement to `endpoint`/announce, if it is safe to."""
        if not endpoint_allowed(endpoint, self._allow_private):
            log.info("not connecting to refused endpoint")
            return
        request = urllib.request.Request(
            endpoint + "/announce",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": self._user_agent},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                response.read(65536)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.info("announce post failed: %s", type(exc).__name__)


def _encode(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
