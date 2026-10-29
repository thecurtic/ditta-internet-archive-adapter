"""Term store and count worker."""

import sqlite3

from conftest import FakeSource
from ditta_ia_adapter.adapter import SourceBusy, TermStats
from ditta_ia_adapter.terms import CountWorker, TermStore
from ditta_ia_adapter.terms.store import DAY, normalize
from ditta_ia_adapter.terms.worker import CORPUS_UPDATED, DOCUMENT_COUNT


def make_store(tmp_path, now=None):
    clock = (lambda: now[0]) if now else None
    return TermStore(str(tmp_path / "terms.sqlite"), **({"clock": clock} if clock else {}))


def test_normalize_drops_stopwords_punctuation_and_case() -> None:
    assert normalize("Lighthouse,") == "lighthouse"
    assert normalize("THE") is None
    assert normalize("***") is None
    assert normalize("x" * 65) is None


def test_store_holds_bare_terms_and_day_rounded_timestamps_only(tmp_path) -> None:
    now = [10 * DAY + 4321]
    store = make_store(tmp_path, now)
    store.note_terms("the Lighthouse keeper".split())

    db = sqlite3.connect(tmp_path / "terms.sqlite")
    columns = [row[1] for row in db.execute("PRAGMA table_info(terms)")]
    assert columns == ["term", "document_count", "newest", "counted_at", "last_requested"]
    rows = db.execute("SELECT term, last_requested FROM terms ORDER BY term").fetchall()
    assert rows == [("keeper", 10 * DAY), ("lighthouse", 10 * DAY)]


def test_uncounted_terms_come_first_then_the_stalest(tmp_path) -> None:
    now = [100 * DAY]
    store = make_store(tmp_path, now)
    store.note_terms(["old", "new"])
    store.record("old", TermStats(5, 1))
    assert store.next_to_count(refresh_after=7 * DAY) == "new"
    now[0] += DAY
    store.record("new", TermStats(3, 1))
    assert store.next_to_count(refresh_after=7 * DAY) is None
    now[0] += 8 * DAY
    assert store.next_to_count(refresh_after=7 * DAY) == "old"


def test_unrequested_terms_expire(tmp_path) -> None:
    now = [100 * DAY]
    store = make_store(tmp_path, now)
    store.note_terms(["fading"])
    now[0] += 91 * DAY
    assert store.expire(unused_for=90 * DAY) == 1


def test_top_terms_are_counted_terms_best_covered_first(tmp_path) -> None:
    store = make_store(tmp_path)
    store.note_terms(["small", "big", "pending"])
    store.record("small", TermStats(2, 5))
    store.record("big", TermStats(900, 7))
    assert [t.term for t in store.top_terms(10)] == ["big", "small"]
    assert store.counted_term_count() == 2


def test_worker_counts_then_marks_the_pass_complete(tmp_path) -> None:
    now = [100 * DAY]
    store = make_store(tmp_path, now)
    store.note_terms(["lighthouse"])
    worker = CountWorker(store, FakeSource(), clock=lambda: now[0])

    worker.step()  # first, the corpus size
    assert store.get_meta(DOCUMENT_COUNT) == 1000
    worker.step()  # then the queued term
    assert store.top_terms(1)[0].document_count == len("lighthouse")
    assert store.get_meta(CORPUS_UPDATED) == 0
    worker.step()  # nothing left: the pass is complete
    assert store.get_meta(CORPUS_UPDATED) == now[0]


def test_worker_waits_out_a_backoff(tmp_path) -> None:
    class Busy(FakeSource):
        def total_documents(self) -> int:
            raise SourceBusy(retry_after=90)

    worker = CountWorker(make_store(tmp_path), Busy(), interval_seconds=5)
    assert worker.step() == 90
