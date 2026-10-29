"""Background worker: counts queued terms one at a time, politely.

It shares the Archive client's throttle, so a backoff started by live
queries pauses counting too, and vice versa.
"""

import logging
import threading
import time

from ..adapter import Source, SourceBusy, SourceError
from .store import DAY, TermStore

log = logging.getLogger(__name__)

# Meta keys in the term store.
CORPUS_UPDATED = "corpus_updated"  # end of the last completed refresh pass
DOCUMENT_COUNT = "document_count"
DOCUMENT_COUNT_AT = "document_count_at"


class CountWorker:
    def __init__(
        self,
        store: TermStore,
        source: Source,
        interval_seconds: float = 5,
        refresh_after_days: int = 7,
        expire_after_days: int = 90,
        clock=time.time,
    ) -> None:
        self._store = store
        self._source = source
        self._interval = interval_seconds
        self._refresh_after = refresh_after_days * DAY
        self._expire_after = expire_after_days * DAY
        self._clock = clock
        self._counted_this_pass = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="count-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def step(self) -> float:
        """Do one unit of work; return how long to wait before the next."""
        try:
            if self._clock() - self._store.get_meta(DOCUMENT_COUNT_AT) > DAY:
                self._store.set_meta(DOCUMENT_COUNT, self._source.total_documents())
                self._store.set_meta(DOCUMENT_COUNT_AT, int(self._clock()))
                return self._interval

            term = self._store.next_to_count(self._refresh_after)
            if term is None:
                self._finish_pass()
                return self._interval * 6
            self._store.record(term, self._source.count(term))
            self._counted_this_pass += 1
            return self._interval
        except SourceBusy as busy:
            return max(self._interval, busy.retry_after)
        except SourceError as exc:
            log.warning("count failed: %s", exc)
            return self._interval * 6

    def _finish_pass(self) -> None:
        # Every term is fresh: the snapshot time moves only now, so it always
        # names a moment when the whole table had been counted.
        if self._counted_this_pass:
            self._store.set_meta(CORPUS_UPDATED, int(self._clock()))
            self._counted_this_pass = 0
        self._store.expire(self._expire_after)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.step())
