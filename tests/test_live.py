"""Opt-in checks against the real Internet Archive.

Skipped unless DITTA_IA_LIVE=1. They make four requests when the Archive
answers as expected (a few more if a search has to walk further down its
ladder), one at a time, at least five seconds apart, through one shared
throttle, and print nothing but numbers.

An AI coding agent running them must set DITTA_IA_AGENT to its model
identifier: the Archive's bot rules require AI agents to name their model in
the User-Agent.
"""

import os

import pytest

from ditta_ia_adapter.ia import ArchiveSource
from ditta_ia_adapter.ia.throttle import Throttle

pytestmark = pytest.mark.skipif(os.environ.get("DITTA_IA_LIVE") != "1", reason="set DITTA_IA_LIVE=1")

# Gentler than production's one second: these run from a developer's machine.
LIVE_INTERVAL_SECONDS = 5


@pytest.fixture(scope="module")
def throttle() -> Throttle:
    return Throttle(max_concurrent=1, min_interval=LIVE_INTERVAL_SECONDS)


def make_source(backend: str, throttle: Throttle):
    # The same filters production uses by default.
    return ArchiveSource(
        os.environ.get("DITTA_IA_CONTACT_URL", "https://example.com"),
        backend=backend,
        agent=os.environ.get("DITTA_IA_AGENT") or None,
        languages=["en"],
        mediatypes=["texts"],
        throttle=throttle,
    )


@pytest.fixture(scope="module")
def fulltext(throttle: Throttle):
    return make_source("fulltext", throttle)


@pytest.fixture(scope="module")
def metadata(throttle: Throttle):
    return make_source("metadata", throttle)


def test_live_fulltext_search_returns_reranked_passage_results(fulltext) -> None:
    # A common phrase, so the first ladder step fills the 100-item pool and
    # the search is one request for a full page.
    results = fulltext.search("lighthouse keeper", 5)
    assert 0 < len(results) <= 5
    assert all(r.url.startswith("https://archive.org/details/") for r in results)
    assert not any("{{{" in r.snippet or "}}}" in r.snippet for r in results)
    assert len({r.id for r in results}) == len(results)


def test_live_fulltext_count_returns_count_and_newest(fulltext) -> None:
    stats = fulltext.count("lighthouse")
    print(f"full-text count for lighthouse: {stats.document_count}")
    # About 970k when recorded; far fewer would mean the filters misfire.
    assert stats.document_count > 500_000 and stats.newest > 0


def test_live_fulltext_total_documents_is_filtered(fulltext) -> None:
    total = fulltext.total_documents()
    print(f"full-text English-or-unlabelled texts: {total}")
    # About 36.7M when recorded; the whole index, all media types, was 49.8M.
    assert 10_000_000 < total < 45_000_000


def test_live_metadata_fallback_still_searches(metadata) -> None:
    results = metadata.search("lighthouse keeper", 5)
    assert 0 < len(results) <= 5
    assert all(r.url.startswith("https://archive.org/details/") for r in results)
