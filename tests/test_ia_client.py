"""The Archive client against recorded-shape fixtures; no network."""

from datetime import datetime, timezone

import pytest
import requests

from conftest import FakeResponse, FakeSession, fixture
from ditta_ia_adapter import __version__
from ditta_ia_adapter.adapter import SourceBusy, SourceError, UnusableQuery
from ditta_ia_adapter.ia import ArchiveSource
from ditta_ia_adapter.ia.mapping import SEARCH_FIELDS, clean_text, truncate
from ditta_ia_adapter.ia.query import all_terms, any_term, escape_term
from ditta_ia_adapter.ia.throttle import Throttle, TTLCache, parse_retry_after


def make_source(*responses, **kwargs) -> tuple[ArchiveSource, FakeSession]:
    session = FakeSession(*responses)
    source = ArchiveSource("https://example.com", session=session, **kwargs)
    return source, session


# --- query translation ------------------------------------------------------


@pytest.mark.parametrize(
    ("term", "escaped"),
    [
        ("title:secret", "title\\:secret"),
        ("wild*", "wild\\*"),
        ("[1 TO 9]", "\\[1 TO 9\\]"),
        ("a&&b||c", "a\\&\\&b\\|\\|c"),
        ('"quoted"', '\\"quoted\\"'),
        ("back\\slash/", "back\\\\slash\\/"),
        ("-not", "\\-not"),
        ("AND", "and"),
        ("NOT", "not"),
        ("plain", "plain"),
    ],
)
def test_every_special_character_and_operator_is_neutralized(term: str, escaped: str) -> None:
    assert escape_term(term) == escaped


def test_terms_join_with_and_then_or() -> None:
    assert all_terms(["a:b", "c"]) == "a\\:b AND c"
    assert any_term(["a:b", "c"]) == "a\\:b OR c"


def test_search_sends_paged_escaped_query_with_only_needed_fields() -> None:
    source, session = make_source(FakeResponse(body=fixture("search_two_terms.json")))
    source.search("lighthouse  keeper", 8)

    params = session.calls[0]["params"]
    assert session.calls[0]["url"] == "https://archive.org/advancedsearch.php"
    assert params["q"] == "lighthouse AND keeper"
    assert (params["page"], params["rows"], params["output"]) == (1, 8, "json")
    assert [params[f"fl[{i}]"] for i in range(7)] == SEARCH_FIELDS
    assert "sort[0]" not in params  # relevance order is the Archive's default


def test_raw_query_string_never_reaches_the_archive() -> None:
    source, session = make_source(FakeResponse(body=fixture("search_two_terms.json")))
    source.search("collection:nasa AND title:*", 8)
    assert session.calls[0]["params"]["q"] == "collection\\:nasa AND and AND title\\:\\*"


def test_zero_hits_retries_once_with_or() -> None:
    source, session = make_source(
        FakeResponse(body=fixture("search_no_hits.json")),
        FakeResponse(body=fixture("search_two_terms.json")),
    )
    results = source.search("zzqx lighthouse", 8)
    assert [call["params"]["q"] for call in session.calls] == ["zzqx AND lighthouse", "zzqx OR lighthouse"]
    assert len(results) == 8


def test_single_term_with_zero_hits_makes_one_request() -> None:
    source, session = make_source(FakeResponse(body=fixture("search_no_hits.json")))
    assert source.search("zzqx", 8) == []
    assert len(session.calls) == 1


@pytest.mark.parametrize("query", ["", "   \t\n"])
def test_empty_query_is_unusable_and_sends_nothing(query: str) -> None:
    source, session = make_source()
    with pytest.raises(UnusableQuery):
        source.search(query, 8)
    assert session.calls == []


def test_too_many_terms_is_unusable() -> None:
    source, _ = make_source(max_terms=3)
    with pytest.raises(UnusableQuery):
        source.search("a b c d", 8)


def test_never_returns_more_than_n() -> None:
    source, _ = make_source(FakeResponse(body=fixture("search_two_terms.json")))
    assert len(source.search("lighthouse", 1)) == 1


# --- result mapping -----------------------------------------------------------


def test_recorded_results_keep_archive_order_and_are_mapped() -> None:
    source, _ = make_source(FakeResponse(body=fixture("search_two_terms.json")))
    results = source.search("lighthouse keeper", 8)

    assert [r.id for r in results][:3] == ["twitch-vod-v1713233461", "portland-head-light", "VOA_Africa_20170817_060000"]
    second = results[1]
    assert second.url == "https://archive.org/details/portland-head-light"
    assert second.title == "Portland Head Light Postcard"
    assert second.snippet.startswith("PORTLAND HEAD LIGHT FIRST LIGHTHOUSE")
    assert all(len(r.snippet) <= 300 for r in results)


def test_item_without_description_gets_a_metadata_snippet() -> None:
    source, _ = make_source(FakeResponse(body=fixture("search_two_terms.json")))
    book = {r.id: r for r in source.search("lighthouse keeper", 8)}["klemwatercrestli0000died"]
    assert book.snippet == "DIEDRECK, JAY · 2017 · texts"


def test_stringified_subject_lists_are_read_as_lists() -> None:
    doc = {"identifier": "x", "subject": "['Radio Program', \"Mass media\", 'Third', 'Fourth']"}
    body = {"response": {"numFound": 1, "docs": [doc]}}
    source, _ = make_source(FakeResponse(body=body))
    assert source.search("x", 8)[0].snippet == "Radio Program, Mass media, Third"


def test_markup_in_descriptions_is_stripped() -> None:
    doc = {"identifier": "x", "description": "<p>A keeper&#39;s log.</p>\n<p>Lamp   <b>hours</b>.</p><script>alert(1)</script>"}
    source, _ = make_source(FakeResponse(body={"response": {"numFound": 1, "docs": [doc]}}))
    assert source.search("x", 8)[0].snippet == "A keeper's log. Lamp hours."


def test_missing_title_falls_back_to_identifier_and_url_is_built() -> None:
    doc = {"identifier": "a b/c", "url": "javascript:alert(1)", "mediatype": "texts"}
    source, _ = make_source(FakeResponse(body={"response": {"numFound": 1, "docs": [doc]}}))
    (result,) = source.search("x", 8)
    assert result.title == "a b/c"
    assert result.url == "https://archive.org/details/a%20b%2Fc"


def test_docs_without_identifier_or_any_snippet_are_skipped() -> None:
    docs = [{"title": "orphan", "description": "d"}, {"identifier": "bare"}, {"identifier": "ok", "description": "d"}]
    source, _ = make_source(FakeResponse(body={"response": {"numFound": 3, "docs": docs}}))
    assert [r.id for r in source.search("x", 8)] == ["ok"]  # no identifier, or nothing for a snippet


def test_clean_text_strips_markup_controls_and_whitespace() -> None:
    assert clean_text("<a href='x'>link</a>\x07 &amp;\n\tmore<style>p{}</style>") == "link & more"


def test_truncate_cuts_on_a_word_boundary() -> None:
    assert truncate("one two three four", 12) == "one two…"
    assert truncate("short", 12) == "short"


# --- counts -------------------------------------------------------------------


def test_count_uses_one_newest_first_request() -> None:
    source, session = make_source(FakeResponse(body=fixture("count_lighthouse.json")))
    stats = source.count("lighthouse")

    params = session.calls[0]["params"]
    assert (params["rows"], params["sort[0]"]) == (1, "publicdate desc")
    assert stats.document_count == 15772
    assert stats.newest == int(datetime(2026, 10, 29, 0, 10, 42, tzinfo=timezone.utc).timestamp())


def test_total_documents_is_the_archives_own_count() -> None:
    source, session = make_source(FakeResponse(body=fixture("total_all.json")))
    assert source.total_documents() == 128351232
    assert (session.calls[0]["params"]["q"], session.calls[0]["params"]["rows"]) == ("*:*", 0)


def test_count_of_unknown_term_is_zero() -> None:
    source, _ = make_source(FakeResponse(body=fixture("search_no_hits.json")))
    stats = source.count("zzqx")
    assert (stats.document_count, stats.newest) == (0, 0)


# --- politeness ---------------------------------------------------------------


def test_identical_searches_are_cached() -> None:
    source, session = make_source(FakeResponse(body=fixture("search_two_terms.json")))
    source.search("lighthouse keeper", 8)
    source.search("lighthouse   keeper", 8)
    assert len(session.calls) == 1


def test_429_with_retry_after_backs_off_without_further_requests() -> None:
    source, session = make_source(FakeResponse(status=429, headers={"Retry-After": "120"}))
    with pytest.raises(SourceBusy) as first:
        source.search("lighthouse", 8)
    assert 119 <= first.value.retry_after <= 120

    with pytest.raises(SourceBusy):
        source.search("different query", 8)
    assert len(session.calls) == 1  # the second query never left the adapter


def test_server_errors_and_network_failures_back_off() -> None:
    source, _ = make_source(FakeResponse(status=503))
    with pytest.raises(SourceBusy):
        source.search("a", 8)

    source, _ = make_source(requests.ConnectionError("down"))
    with pytest.raises(SourceBusy):
        source.search("a", 8)


def test_archive_error_body_is_a_source_error() -> None:
    source, _ = make_source(FakeResponse(body=fixture("search_error.json")))
    with pytest.raises(SourceError):
        source.search("a", 8)


def test_backoff_grows_exponentially_and_resets_on_success() -> None:
    now = [0.0]
    throttle = Throttle(max_concurrent=1, base_backoff=2, clock=lambda: now[0])
    throttle.failed()
    assert throttle.seconds_until_resume() == 2
    now[0] = 3
    throttle.failed()
    assert throttle.seconds_until_resume() == 4
    now[0] = 10
    throttle.succeeded()
    throttle.failed()
    assert throttle.seconds_until_resume() == 2


def test_concurrency_cap_refuses_when_slots_are_taken() -> None:
    throttle = Throttle(max_concurrent=1)
    throttle.acquire()
    with pytest.raises(SourceBusy):
        throttle.acquire(wait_seconds=0.01)
    throttle.release()


def test_retry_after_accepts_seconds_and_http_dates() -> None:
    assert parse_retry_after("30") == 30
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:30 GMT", now=1445412480) == 30
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_cache_expires_entries() -> None:
    now = [0.0]
    cache = TTLCache(ttl_seconds=10, clock=lambda: now[0])
    cache.put("k", "v")
    assert cache.get("k") == "v"
    now[0] = 11
    assert cache.get("k") is None


def test_user_agent_names_the_tool_version_and_contact_but_no_model() -> None:
    source = ArchiveSource("https://example.com")
    expected = f"ditta-ia-adapter/{__version__} (+https://example.com)"
    assert source.user_agent_suffix == expected
    user_agent = source._session.headers["User-Agent"]
    assert user_agent.endswith(expected)
    assert "claude" not in user_agent.lower()


# --- language scoping ---------------------------------------------------------


def test_language_filter_keeps_english_and_unlabelled_items() -> None:
    source, session = make_source(FakeResponse(body=fixture("search_two_terms.json")), languages=["en"])
    source.search("lighthouse keeper", 8)
    assert session.calls[0]["params"]["q"] == (
        "(lighthouse AND keeper) AND (language:(eng OR english OR en) OR (*:* -language:*))"
    )


def test_language_filter_can_require_a_label() -> None:
    source, session = make_source(
        FakeResponse(body=fixture("search_two_terms.json")), languages=["en"], include_unlabeled=False
    )
    source.search("lighthouse", 8)
    assert session.calls[0]["params"]["q"] == "(lighthouse) AND language:(eng OR english OR en)"


def test_language_filter_applies_to_counts_and_corpus_size() -> None:
    source, session = make_source(
        FakeResponse(body=fixture("count_lighthouse.json")),
        FakeResponse(body=fixture("total_all.json")),
        languages=["en"],
    )
    source.count("lighthouse")
    source.total_documents()
    assert all("language:(eng" in call["params"]["q"] for call in session.calls)


def test_no_languages_means_no_filter() -> None:
    source, session = make_source(FakeResponse(body=fixture("search_two_terms.json")))
    source.search("lighthouse", 8)
    assert session.calls[0]["params"]["q"] == "lighthouse"
