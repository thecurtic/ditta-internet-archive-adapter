"""SQLite store of bare terms and their corpus statistics.

Privacy is the point of this design. A row holds a term, its statistics,
when it was counted, and the day it was last requested. It never holds a
whole query string, a requester pubkey, an IP address, or a per-query
timestamp, so the store cannot say who searched for what, or when.
`last_requested` is rounded down to the day for the same reason.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass

from ..adapter import TermStats
from .stopwords import STOPWORDS

DAY = 86400
MAX_TERM_CHARS = 64
_EDGE_PUNCTUATION = "\"'`.,;:!?()[]{}<>*~^+-=|/\\"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS terms (
    term           TEXT PRIMARY KEY,
    document_count INTEGER,          -- NULL until first counted
    newest         INTEGER,
    counted_at     INTEGER,
    last_requested INTEGER NOT NULL  -- Unix day start, never a query time
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class StoredTerm:
    term: str
    document_count: int
    newest: int


def normalize(raw: str) -> str | None:
    """The stored form of a query term, or None if it is not worth keeping.

    Terms are lowercased because the Archive's search is case-blind, so
    "Bitcoin" and "bitcoin" are one term here.
    """
    term = raw.strip(_EDGE_PUNCTUATION).lower()
    if not term or len(term) > MAX_TERM_CHARS or term in STOPWORDS:
        return None
    return term


class TermStore:
    def __init__(self, path: str, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    def note_terms(self, raw_terms: list[str]) -> None:
        """Queue a served query's terms (minus stopwords) for counting."""
        today = int(self._clock()) // DAY * DAY
        terms = {t for t in (normalize(raw) for raw in raw_terms) if t}
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO terms (term, last_requested) VALUES (?, ?) "
                "ON CONFLICT(term) DO UPDATE SET last_requested = excluded.last_requested",
                [(term, today) for term in terms],
            )

    def next_to_count(self, refresh_after: int) -> str | None:
        """A never-counted term first, else the stalest count past its age."""
        stale_before = int(self._clock()) - refresh_after
        with self._lock:
            row = self._db.execute(
                "SELECT term FROM terms WHERE counted_at IS NULL "
                "ORDER BY last_requested DESC LIMIT 1"
            ).fetchone() or self._db.execute(
                "SELECT term FROM terms WHERE counted_at < ? ORDER BY counted_at LIMIT 1",
                (stale_before,),
            ).fetchone()
        return row[0] if row else None

    def record(self, term: str, stats: TermStats) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE terms SET document_count = ?, newest = ?, counted_at = ? WHERE term = ?",
                (stats.document_count, stats.newest, int(self._clock()), term),
            )

    def clear_counts(self) -> None:
        """Forget every count but keep the terms, so each is counted again."""
        with self._lock, self._db:
            self._db.execute("UPDATE terms SET document_count = NULL, newest = NULL, counted_at = NULL")

    def expire(self, unused_for: int) -> int:
        """Drop terms nobody has requested for `unused_for` seconds."""
        cutoff = int(self._clock()) - unused_for
        with self._lock, self._db:
            return self._db.execute("DELETE FROM terms WHERE last_requested < ?", (cutoff,)).rowcount

    def top_terms(self, limit: int) -> list[StoredTerm]:
        """Counted terms, best covered first: the most useful to a router."""
        with self._lock:
            rows = self._db.execute(
                "SELECT term, document_count, newest FROM terms WHERE counted_at IS NOT NULL "
                "ORDER BY document_count DESC, term LIMIT ?",
                (limit,),
            ).fetchall()
        return [StoredTerm(*row) for row in rows]

    def counted_term_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM terms WHERE counted_at IS NOT NULL").fetchone()[0]

    def get_meta(self, key: str, default: int = 0) -> int:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        self._db.close()
