"""Rank stats store, report and command; no network."""

import sqlite3
from pathlib import Path

import pytest

from conftest import FakeResponse, FakeSession
from ditta_ia_adapter import config
from ditta_ia_adapter.__main__ import main
from ditta_ia_adapter.ia import ArchiveSource
from ditta_ia_adapter.rankstats import RankStats, build_report
from ditta_ia_adapter.rankstats.report import format_report
from ditta_ia_adapter.rankstats.store import DAY, SearchCount, ServedCount
from ditta_ia_adapter.rerank import Candidate, Ranked

TODAY = 20000 * DAY


def ranked(position: int, source_rank: int, step: str = "phrase") -> Ranked:
    c = Candidate(id=f"i{source_rank}", url="https://example.com", title="t", source_rank=source_rank, step=step)
    return Ranked(c, coverage=1.0, closeness=0.0, snippet="s", position=position)


def make_stats(tmp_path, now: list[float] | None = None, **kwargs) -> RankStats:
    now = now or [TODAY + 4321]
    return RankStats(str(tmp_path / "stats.sqlite"), clock=lambda: now[0], **kwargs)


# --- store --------------------------------------------------------------------


def test_store_holds_day_bucketed_counters_and_nothing_else(tmp_path) -> None:
    stats = make_stats(tmp_path)
    stats.record([ranked(0, 3), ranked(1, 47, "and")], pool_size=100, pool_full=True)

    db = sqlite3.connect(tmp_path / "stats.sqlite")
    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"served", "searches"}
    columns = [row[1] for row in db.execute("PRAGMA table_info(served)")]
    assert columns == ["day", "pool_size", "pool_full", "step", "position", "band", "count"]
    columns = [row[1] for row in db.execute("PRAGMA table_info(searches)")]
    assert columns == ["day", "pool_size", "pool_full", "count"]
    assert sorted(db.execute("SELECT * FROM served")) == [
        (TODAY, 100, 1, "and", 1, 4, 1),
        (TODAY, 100, 1, "phrase", 0, 0, 1),
    ]


def test_counts_add_up_across_searches_and_days(tmp_path) -> None:
    now = [TODAY + 10]
    stats = make_stats(tmp_path, now)
    stats.record([ranked(0, 3)], 100, True)
    stats.record([ranked(0, 5)], 100, True)  # same band
    now[0] = TODAY + DAY + 10
    stats.record([ranked(0, 9)], 100, False)

    assert stats.served_counts(TODAY) == [
        ServedCount(100, False, "phrase", 0, 0, 1),
        ServedCount(100, True, "phrase", 0, 0, 2),
    ]
    assert sorted(stats.search_counts(TODAY), key=lambda s: s.pool_full) == [
        SearchCount(100, False, 1),
        SearchCount(100, True, 2),
    ]
    assert stats.day_range(TODAY) == (TODAY, TODAY + DAY)
    assert stats.day_range(TODAY + DAY) == (TODAY + DAY, TODAY + DAY)


def test_old_days_expire(tmp_path) -> None:
    now = [TODAY]
    stats = make_stats(tmp_path, now, keep_days=2)
    stats.record([ranked(0, 0)], 100, True)
    now[0] = TODAY + 3 * DAY
    stats.record([ranked(0, 0)], 100, True)
    assert stats.day_range(0) == (TODAY + 3 * DAY, TODAY + 3 * DAY)


def test_a_broken_store_never_breaks_a_search(tmp_path, caplog) -> None:
    stats = make_stats(tmp_path)
    stats.close()
    stats._db = sqlite3.connect(":memory:")  # no tables
    stats.record([ranked(0, 0)], 100, True)
    assert "could not record rank stats" in caplog.text


def test_read_only_store_cannot_write(tmp_path) -> None:
    make_stats(tmp_path).record([ranked(0, 0)], 100, True)
    reader = RankStats(str(tmp_path / "stats.sqlite"), read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        reader._db.execute("DELETE FROM served")


# --- recording from the source ------------------------------------------------


def hits_body(*identifiers: str) -> dict:
    hits = [{"fields": {"identifier": i, "title": i}, "highlight": {"text": ["gore street"]}} for i in identifiers]
    return {"response": {"header": {"succeeded": True}, "body": {"hits": {"total": len(hits), "hits": hits}}}}


def test_fulltext_records_served_results_once_and_never_cache_hits() -> None:
    calls = []
    session = FakeSession(FakeResponse(body=hits_body("a", "b", "c")))
    source = ArchiveSource(
        "https://example.com", backend="fulltext", session=session, min_interval_seconds=0,
        candidate_pool=3, record_ranks=lambda served, size, full: calls.append((served, size, full)),
    )
    source.search("gore", 2)
    source.search("gore", 2)  # cached

    assert len(calls) == 1
    served, size, full = calls[0]
    assert [(r.position, r.candidate.source_rank, r.candidate.step) for r in served] == [(0, 0, "and"), (1, 1, "and")]
    assert (size, full) == (3, True)


def test_a_pool_the_archive_could_not_fill_is_not_full() -> None:
    calls = []
    source = ArchiveSource(
        "https://example.com", backend="fulltext", session=FakeSession(FakeResponse(body=hits_body("a"))),
        min_interval_seconds=0, candidate_pool=3, record_ranks=lambda *args: calls.append(args),
    )
    source.search("gore", 2)
    assert calls[0][2] is False


def test_metadata_backend_records_nothing() -> None:
    calls = []
    body = {"response": {"numFound": 1, "docs": [{"identifier": "a", "description": "d"}]}}
    source = ArchiveSource(
        "https://example.com", backend="metadata", session=FakeSession(FakeResponse(body=body)), min_interval_seconds=0,
        record_ranks=lambda *args: calls.append(args),
    )
    source.search("gore", 2)
    assert calls == []


# --- report -------------------------------------------------------------------


def seeded() -> tuple[list[SearchCount], list[ServedCount]]:
    """100 full-pool searches serving 10 results each, plus 5 partial ones."""
    searches = [SearchCount(100, True, 100), SearchCount(100, False, 5)]
    served = [
        ServedCount(100, True, "phrase", 0, 0, 80),
        ServedCount(100, True, "and", 0, 1, 20),
        ServedCount(100, True, "phrase", 5, 0, 600),
        ServedCount(100, True, "and", 5, 3, 250),
        ServedCount(100, True, "or", 5, 6, 100),
        ServedCount(100, True, "or", 5, 9, 50),  # the tail
        ServedCount(100, False, "or", 5, 9, 40),  # tail, but the pool wasn't full
    ]
    return searches, served


def test_report_histogram_coverage_tail_and_steps() -> None:
    searches, served = seeded()
    report = format_report(searches, served, (TODAY, TODAY + 2 * DAY))
    lines = report.splitlines()

    assert lines[0] == "Rank stats, 2024-10-04 to 2024-10-06: 105 searches (100 with a full pool), pool 100"
    assert lines[2].split()[:3] == ["Served", "position", "Source"]
    assert lines[2].split()[-1] == "91-100"
    assert lines[3].split() == ["1", "80%", "20%"] + ["0%"] * 8
    assert lines[4].split()[0] == "4-10"  # 2-3 had nothing served, so no row
    # 1140 top-10 results: 680 from ranks 1-10 (60%), 700 by 20, 950 by 40 (83%),
    # 1050 by 70 (92%), 1140 by 100.
    assert "Top 10: 90% came from the source's top 70, 95% from its top 100, 99% from its top 100." in lines
    assert "Tail: 4.5% of top-10 results from full pools came from source ranks 81-100." in lines
    assert "By ladder step: phrase 60%, and 24%, or 17%." in lines


def test_report_without_full_pools_says_there_is_no_tail_signal() -> None:
    report = format_report([SearchCount(100, False, 1)], [ServedCount(100, False, "and", 0, 0, 1)], (TODAY, TODAY))
    assert "Tail: no full pools yet, so no tail signal." in report


def test_report_has_a_section_per_pool_size() -> None:
    searches = [SearchCount(100, True, 1), SearchCount(200, True, 1)]
    served = [ServedCount(100, True, "and", 0, 0, 1), ServedCount(200, True, "and", 0, 15, 1)]
    report = format_report(searches, served, (TODAY, TODAY))
    assert "pool 100" in report and "pool 200" in report
    assert "source ranks 161-200" in report


def test_empty_store_reports_no_searches(tmp_path) -> None:
    assert build_report(make_stats(tmp_path), days=30) == "No searches recorded in the last 30 days."


def test_report_window_counts_back_from_today(tmp_path) -> None:
    now = [TODAY - 40 * DAY]
    stats = make_stats(tmp_path, now)
    stats.record([ranked(0, 0)], 100, True)
    now[0] = TODAY
    assert build_report(stats, days=30).startswith("No searches")
    assert build_report(stats, days=41).startswith("Rank stats")


# --- command and configuration ------------------------------------------------


def write_config(tmp_path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[node]\nendpoint = "https://example.com/ditta"\n'
        '[archive]\ncontact_url = "https://example.com"\n' + extra
    )
    return path


def test_rank_report_command_prints_the_report(tmp_path, capsys) -> None:
    path = str(write_config(tmp_path))
    assert main(["rank-report", "--config", path]) == 0
    assert "No rank stats yet" in capsys.readouterr().out

    RankStats(str(tmp_path / "stats.sqlite")).record([ranked(0, 0)], 100, True)
    assert main(["rank-report", "--config", path, "--days", "7"]) == 0
    assert "1 searches (1 with a full pool), pool 100" in capsys.readouterr().out


def test_config_reads_stats_settings(tmp_path) -> None:
    loaded = config.load(write_config(tmp_path, '[stats]\ndb_path = "r.sqlite"\nkeep_days = 30\n'))
    assert (loaded.stats_db, loaded.stats_keep_days) == (tmp_path / "r.sqlite", 30)


@pytest.mark.parametrize("stats", ['db_path = "terms.sqlite"', 'db_path = "peers.sqlite"', "keep_days = 0"])
def test_config_keeps_the_stats_store_separate_and_bounded(tmp_path, stats: str) -> None:
    with pytest.raises(ValueError):
        config.load(write_config(tmp_path, f"[stats]\n{stats}\n"))
