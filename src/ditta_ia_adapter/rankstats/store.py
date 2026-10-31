"""SQLite counters showing where served results sat in the source's own order.

They answer one question: is the candidate pool big enough? If the reranker
often serves items from the tail of the pool, items just past it would
probably have been served too, and `candidate_pool` should grow.

Privacy is part of the design. A row is a counter keyed by the day, the pool
size, whether the pool was full, the ladder step, the served position and
the source's rank band. It never holds a query, a term, a per-query time or
anything about a requester, and this store shares no file or connection with
the term store, so the two cannot be joined. The step does hint at query
length (a phrase step means two or more words), so on a quiet day the
counters show roughly how many searches ran and how strict their matches
were, but nothing about what was searched or by whom.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..rerank import Ranked

log = logging.getLogger(__name__)

DAY = 86400
# Source ranks are counted in bands of this many: band 0 is ranks 1-10.
BAND_WIDTH = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS served (
    day       INTEGER NOT NULL,  -- Unix day start, never a query time
    pool_size INTEGER NOT NULL,
    pool_full INTEGER NOT NULL,  -- 1 if the pool held pool_size items
    step      TEXT    NOT NULL,  -- the ladder step that found the item
    position  INTEGER NOT NULL,  -- 0-based served position
    band      INTEGER NOT NULL,  -- source rank // BAND_WIDTH
    count     INTEGER NOT NULL,
    PRIMARY KEY (day, pool_size, pool_full, step, position, band)
);
CREATE TABLE IF NOT EXISTS searches (
    day       INTEGER NOT NULL,
    pool_size INTEGER NOT NULL,
    pool_full INTEGER NOT NULL,
    count     INTEGER NOT NULL,
    PRIMARY KEY (day, pool_size, pool_full)
);
"""


@dataclass(frozen=True)
class ServedCount:
    pool_size: int
    pool_full: bool
    step: str
    position: int
    band: int
    count: int


@dataclass(frozen=True)
class SearchCount:
    pool_size: int
    pool_full: bool
    count: int


class RankStats:
    def __init__(self, path: str, keep_days: int = 90, clock=time.time, read_only: bool = False) -> None:
        self._clock = clock
        self._keep = keep_days * DAY
        self._lock = threading.Lock()
        self._expired_on: int | None = None
        if read_only:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.executescript(_SCHEMA)

    def record(self, served: list[Ranked], pool_size: int, pool_full: bool) -> None:
        """Count one search and each result it served.

        A failure here is logged, never raised: stats must not break search.
        """
        today = self.today()
        full = int(pool_full)
        rows = [
            (today, pool_size, full, r.candidate.step, r.position, r.candidate.source_rank // BAND_WIDTH)
            for r in served
        ]
        try:
            with self._lock, self._db:
                self._db.executemany(
                    "INSERT INTO served VALUES (?, ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT (day, pool_size, pool_full, step, position, band) "
                    "DO UPDATE SET count = count + 1",
                    rows,
                )
                self._db.execute(
                    "INSERT INTO searches VALUES (?, ?, ?, 1) "
                    "ON CONFLICT (day, pool_size, pool_full) DO UPDATE SET count = count + 1",
                    (today, pool_size, full),
                )
                if self._expired_on != today:
                    self._expire(today)
                    self._expired_on = today
        except sqlite3.Error:
            log.warning("could not record rank stats", exc_info=True)

    def _expire(self, today: int) -> None:
        cutoff = today - self._keep
        self._db.execute("DELETE FROM served WHERE day < ?", (cutoff,))
        self._db.execute("DELETE FROM searches WHERE day < ?", (cutoff,))

    def today(self) -> int:
        return int(self._clock()) // DAY * DAY

    def served_counts(self, since_day: int) -> list[ServedCount]:
        """Counters from `since_day` on, summed over days."""
        with self._lock:
            rows = self._db.execute(
                "SELECT pool_size, pool_full, step, position, band, SUM(count) FROM served "
                "WHERE day >= ? GROUP BY pool_size, pool_full, step, position, band",
                (since_day,),
            ).fetchall()
        return [ServedCount(size, bool(full), step, pos, band, n) for size, full, step, pos, band, n in rows]

    def search_counts(self, since_day: int) -> list[SearchCount]:
        with self._lock:
            rows = self._db.execute(
                "SELECT pool_size, pool_full, SUM(count) FROM searches WHERE day >= ? GROUP BY pool_size, pool_full",
                (since_day,),
            ).fetchall()
        return [SearchCount(size, bool(full), n) for size, full, n in rows]

    def day_range(self, since_day: int) -> tuple[int, int] | None:
        """The first and last day with searches from `since_day` on."""
        with self._lock:
            first, last = self._db.execute(
                "SELECT MIN(day), MAX(day) FROM searches WHERE day >= ?", (since_day,)
            ).fetchone()
        return (first, last) if first is not None else None

    def close(self) -> None:
        self._db.close()
