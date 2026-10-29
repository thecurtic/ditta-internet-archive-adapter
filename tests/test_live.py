"""Opt-in checks against the real Internet Archive.

Skipped unless DITTA_IA_LIVE=1. They make three requests in total, through
the same throttle as production, and print nothing but numbers.

An AI coding agent running them must set DITTA_IA_AGENT to its model
identifier: the Archive's bot rules require AI agents to name their model in
the User-Agent.
"""

import os

import pytest

from ditta_ia_adapter.ia import ArchiveSource

pytestmark = pytest.mark.skipif(os.environ.get("DITTA_IA_LIVE") != "1", reason="set DITTA_IA_LIVE=1")


@pytest.fixture(scope="module")
def source() -> ArchiveSource:
    contact = os.environ.get("DITTA_IA_CONTACT_URL", "https://example.com")
    agent = os.environ.get("DITTA_IA_AGENT") or None
    return ArchiveSource(contact, max_concurrent=1, agent=agent, languages=["en"])


def test_live_search_returns_mapped_results_best_first(source: ArchiveSource) -> None:
    results = source.search("lighthouse keeper", 5)
    assert 0 < len(results) <= 5
    assert all(r.url.startswith("https://archive.org/details/") for r in results)


def test_live_count_returns_count_and_newest(source: ArchiveSource) -> None:
    stats = source.count("lighthouse")
    assert stats.document_count > 0 and stats.newest > 0


def test_live_total_documents(source: ArchiveSource) -> None:
    total = source.total_documents()
    print(f"English-or-unlabelled corpus size: {total}")
    assert total > 1_000_000
