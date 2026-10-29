"""Politeness toward the Archive: a concurrency cap, backoff, and a cache.

The Archive's automated access guidance (archive.org/developers/bots.html)
asks clients to honour 429 and Retry-After, back off on errors, and keep
concurrency low. These are hard requirements, so they live in one place.
"""

import threading
import time
from collections import OrderedDict
from email.utils import parsedate_to_datetime

from ..adapter import SourceBusy

MAX_BACKOFF_SECONDS = 600


class Throttle:
    """Gate every request: refuse while backing off, cap concurrency."""

    def __init__(
        self,
        max_concurrent: int,
        base_backoff: float = 2.0,
        min_interval: float = 0.0,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._spacing = threading.Lock()
        self._base = base_backoff
        self._min_interval = min_interval
        self._last_start = float("-inf")
        self._clock = clock
        self._sleep = sleep
        self._failures = 0
        self._resume_at = 0.0

    def acquire(self, wait_seconds: float = 10.0) -> None:
        """Take a request slot, or raise SourceBusy."""
        self._raise_if_backing_off()
        if not self._slots.acquire(timeout=wait_seconds):
            raise SourceBusy(retry_after=int(wait_seconds))
        try:
            self._wait_for_spacing()
            # Another request may have started a backoff while this one waited.
            self._raise_if_backing_off()
        except SourceBusy:
            self._slots.release()
            raise

    def _wait_for_spacing(self) -> None:
        """Start requests at least `min_interval` seconds apart, in order."""
        with self._spacing:
            wait = self._last_start + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last_start = self._clock()

    def release(self) -> None:
        self._slots.release()

    def succeeded(self) -> None:
        with self._lock:
            self._failures = 0

    def failed(self, retry_after: float | None = None) -> None:
        """Start a backoff: Retry-After when given, else exponential."""
        with self._lock:
            self._failures += 1
            if retry_after is None:
                retry_after = self._base * 2 ** (self._failures - 1)
            delay = min(max(retry_after, 0.0), MAX_BACKOFF_SECONDS)
            self._resume_at = max(self._resume_at, self._clock() + delay)

    def seconds_until_resume(self) -> int:
        """Whole seconds left in the current backoff; 0 when not backing off."""
        with self._lock:
            remaining = self._resume_at - self._clock()
        return max(0, int(remaining + 0.999))

    def _raise_if_backing_off(self) -> None:
        remaining = self.seconds_until_resume()
        if remaining > 0:
            raise SourceBusy(retry_after=remaining)


def parse_retry_after(header: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (seconds or HTTP date)."""
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


class TTLCache:
    """A small thread-safe cache for identical searches within a short window."""

    def __init__(self, ttl_seconds: float, max_entries: int = 1024, clock=time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: object) -> object | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if self._clock() - stored_at > self._ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: object, value: object) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._entries[key] = (self._clock(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
