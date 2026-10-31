"""The full-text backend against recorded fixtures; no network."""

from datetime import datetime, timezone

import pytest

from conftest import FakeResponse, FakeSession, fixture
from ditta_ia_adapter import config
from ditta_ia_adapter.adapter import SourceBusy, SourceError, UnusableQuery
from ditta_ia_adapter.ia import ArchiveSource, FullTextSource, MetadataSource
from ditta_ia_adapter.ia.query import language_filter, language_names, loose_phrase

FILTERS = "meta_mediatype:(texts) AND (meta_languageSorter:(English) OR (*:* -meta_languageSorter:*))"


def make_source(*responses, **kwargs) -> tuple[FullTextSource, FakeSession]:
    session = FakeSession(*responses)
    kwargs.setdefault("min_interval_seconds", 0)  # spacing is tested with the metadata backend
    kwargs.setdefault("languages", ["en"])
    kwargs.setdefault("mediatypes", ["texts"])
    source = ArchiveSource("https://example.com", backend="fulltext", session=session, **kwargs)
    return source, session


def hits_body(*items: tuple[str, list[str]]) -> dict:
    """A response in the recorded shape: one text-file hit per (identifier, passages)."""
    hits = [
        {"hit_type": "text", "fields": {"identifier": i, "title": f"Title {i}"}, "highlight": {"text": passages}}
        for i, passages in items
    ]
    return {"response": {"header": {"succeeded": True}, "body": {"hits": {"total": len(hits), "hits": hits}}}}


def queries(session: FakeSession) -> list[str]:
    return [call["params"]["user_query"] for call in session.calls]


# --- requests -----------------------------------------------------------------


def test_search_sends_a_paged_filtered_lucene_query_to_the_fulltext_service() -> None:
    source, session = make_source(FakeResponse(body=fixture("fulltext_search_phrase.json")), candidate_pool=10)
    source.search("gore  street vancouver", 5)

    call = session.calls[0]
    assert call["url"] == "https://archive.org/services/search/beta/page_production/"
    assert call["params"] == {
        "service_backend": "fts",
        "user_query": f'!L ("gore street vancouver"~3) AND {FILTERS}',
        "hits_per_page": 10,
        "page": 1,
        "aggregations": "false",
    }


def test_raw_query_string_never_reaches_the_archive() -> None:
    source, session = make_source(FakeResponse(body=hits_body(("a", ["x"]))), candidate_pool=1)
    source.search('collection:nasa title:* "quote', 1)
    assert queries(session)[0] == f'!L ("collection\\:nasa title\\:\\* \\"quote"~3) AND {FILTERS}'


def test_phrase_escapes_every_term() -> None:
    assert loose_phrase(["a:b", "AND", "c"]) == '"a\\:b and c"~3'


def test_full_ladder_runs_strictest_first_while_the_pool_is_short() -> None:
    source, session = make_source(
        FakeResponse(body=hits_body(("phrase-1", ["p"]))),
        FakeResponse(body=hits_body(("phrase-1", ["p"]), ("and-1", ["a"]))),
        FakeResponse(body=hits_body(("most-1", ["m"]))),
        FakeResponse(body=hits_body(("any-1", ["o"]), ("any-2", ["o"]))),
        candidate_pool=4,
    )
    results = source.search("gore street vancouver", 4)
    assert [query.split(" AND meta_mediatype")[0] for query in queries(session)] == [
        '!L ("gore street vancouver"~3)',
        "!L (gore AND street AND vancouver)",
        "!L ((gore AND street) OR (gore AND vancouver) OR (street AND vancouver))",
        "!L (gore OR street OR vancouver)",
    ]
    # Earlier steps first, no duplicates, pool cut at its size.
    assert [r.id for r in results] == ["phrase-1", "and-1", "most-1", "any-1"]


def test_a_full_pool_stops_the_ladder() -> None:
    source, session = make_source(FakeResponse(body=fixture("fulltext_search_phrase.json")), candidate_pool=10)
    assert len(source.search("gore street vancouver", 5)) == 5
    assert len(session.calls) == 1


def test_one_term_makes_a_single_request() -> None:
    source, session = make_source(FakeResponse(body=fixture("fulltext_no_hits.json")))
    assert source.search("zzqx", 8) == []
    assert queries(session) == [f"!L (zzqx) AND {FILTERS}"]


def test_two_terms_skip_all_but_one() -> None:
    source, session = make_source(
        FakeResponse(body=fixture("fulltext_no_hits.json")),
        FakeResponse(body=fixture("fulltext_no_hits.json")),
        FakeResponse(body=fixture("fulltext_no_hits.json")),
    )
    source.search("lighthouse keeper", 8)
    assert [query.split(" AND meta_mediatype")[0] for query in queries(session)] == [
        '!L ("lighthouse keeper"~3)',
        "!L (lighthouse AND keeper)",
        "!L (lighthouse OR keeper)",
    ]


@pytest.mark.parametrize("query", ["", "   \t\n"])
def test_empty_query_is_unusable_and_sends_nothing(query: str) -> None:
    source, session = make_source()
    with pytest.raises(UnusableQuery):
        source.search(query, 8)
    assert session.calls == []


# --- the pool -----------------------------------------------------------------


def test_hits_from_one_item_merge_and_keep_the_first_position() -> None:
    source, _ = make_source(
        FakeResponse(body=hits_body(("book", ["vol 1 passage"]), ("other", ["x"]), ("book", ["vol 2 passage"]))),
        FakeResponse(body=hits_body(("book", ["vol 1 passage"]), ("third", ["y"]))),
        FakeResponse(body=fixture("fulltext_no_hits.json")),
        candidate_pool=5,
    )
    pool, complete = source._fill_pool(["a", "b"])
    assert complete
    assert [(c.id, c.source_rank, c.step) for c in pool] == [("book", 0, "phrase"), ("other", 1, "phrase"), ("third", 2, "and")]
    # Both volumes' passages, and the repeat from the AND step only once.
    assert pool[0].passages == ["vol 1 passage", "vol 2 passage"]


def test_pool_never_grows_past_its_size_and_results_past_n() -> None:
    three = FakeResponse(body=hits_body(("a", ["x"]), ("b", ["x"]), ("c", ["x"])))
    source, _ = make_source(three, three, candidate_pool=2)
    pool, _ = source._fill_pool(["one"])
    assert [c.id for c in pool] == ["a", "b"]
    assert len(source.search("one", 1)) == 1


def test_identical_searches_are_cached() -> None:
    source, session = make_source(FakeResponse(body=fixture("fulltext_search_phrase.json")), candidate_pool=10)
    source.search("gore street vancouver", 5)
    source.search("gore   street vancouver", 5)
    assert len(session.calls) == 1


# --- result mapping -----------------------------------------------------------


def test_recorded_hits_are_reranked_and_mapped_with_a_passage_snippet() -> None:
    source, _ = make_source(FakeResponse(body=fixture("fulltext_search_phrase.json")), candidate_pool=13)
    results = source.search("gore street vancouver", 13)

    assert len(results) == 13
    ids = [r.id for r in results]
    # Exact phrase plus "Vancouver" in the title first, the Archive's order
    # among equals, and "Gore Street and Cordova Street, Vancouver" last.
    assert ids[:3] == ["tabletalkmosiacs0000unse", "vancouversmanyfa0000grif", "governmentgazett0121gove_t0j5"]
    assert ids[-1] == "caughtinactantho0000unse"
    first = results[2]
    assert first.id == "governmentgazett0121gove_t0j5"
    assert first.url == "https://archive.org/details/governmentgazett0121gove_t0j5"
    assert first.title == "Government Gazette British Columbia (January 22, 1981)"
    # Match markers and line breaks are gone.
    assert first.snippet == (
        "10330— Albert Lai Realty Ltd., Suite 6, 545 Gore Street, Vancouver, B.C. V6A 2Z6. Anderand Holdings Ltd.,"
    )
    assert not any("{{{" in r.snippet or "\n" in r.snippet for r in results)


def test_without_a_passage_the_snippet_falls_back_to_description_then_metadata() -> None:
    no_highlight = {"response": {"header": {"succeeded": True}, "body": {"hits": {"total": 3, "hits": [
        {"fields": {"identifier": "described", "description": ["<p>First</p>", "second"]}},
        {"fields": {"identifier": "bare-book", "creator": ["Doe, Jo"], "date": "1981-01-01T00:00:00Z", "mediatype": "texts"}},
        {"fields": {"identifier": "nothing"}},
        {"fields": {"title": "no identifier"}, "highlight": {"text": ["passage"]}},
    ]}}}}
    source, _ = make_source(FakeResponse(body=no_highlight))
    results = source.search("x", 8)
    assert [(r.id, r.snippet) for r in results] == [("described", "First"), ("bare-book", "Doe, Jo · 1981 · texts")]


def test_markup_in_passages_is_stripped() -> None:
    body = hits_body(("x", ["<script>alert(1)</script>a {{{<b>keeper</b>}}}&#39;s log"]))
    source, _ = make_source(FakeResponse(body=body))
    assert source.search("x", 8)[0].snippet == "a keeper's log"


# --- counts -------------------------------------------------------------------


def test_count_uses_one_filtered_newest_first_request() -> None:
    source, session = make_source(FakeResponse(body=fixture("fulltext_count_lighthouse.json")))
    stats = source.count("lighthouse")

    params = session.calls[0]["params"]
    assert (params["user_query"], params["hits_per_page"], params["sort"]) == (
        f"!L (lighthouse) AND {FILTERS}", 1, "publicdate:desc"
    )
    assert stats.document_count == 970504
    assert stats.newest == int(datetime(2026, 10, 30, 21, 24, 27, tzinfo=timezone.utc).timestamp())


def test_count_of_unknown_term_is_zero() -> None:
    source, _ = make_source(FakeResponse(body=fixture("fulltext_no_hits.json")))
    stats = source.count("zzqx")
    assert (stats.document_count, stats.newest) == (0, 0)


def test_total_documents_is_the_filtered_hit_total() -> None:
    source, session = make_source(FakeResponse(body=fixture("fulltext_total_texts.json")))
    assert source.total_documents() == 36737904
    assert session.calls[0]["params"]["user_query"] == f"!L (*:*) AND {FILTERS}"


# --- failures and privacy -----------------------------------------------------


def test_a_search_that_did_not_succeed_is_a_source_error() -> None:
    source, _ = make_source(FakeResponse(body=fixture("fulltext_failed.json")))
    with pytest.raises(SourceError):
        source.search("x", 8)


def test_429_backs_off_without_further_requests() -> None:
    source, session = make_source(FakeResponse(status=429, headers={"Retry-After": "120"}))
    with pytest.raises(SourceBusy):
        source.search("lighthouse", 8)
    with pytest.raises(SourceBusy):
        source.count("lighthouse")
    assert len(session.calls) == 1


def test_throttled_later_step_returns_the_pool_so_far_uncached() -> None:
    source, _ = make_source(
        FakeResponse(body=hits_body(("strict-1", ["p"]))),
        FakeResponse(status=429, headers={"Retry-After": "1"}),
        candidate_pool=5,
    )
    assert [r.id for r in source.search("two terms", 3)] == ["strict-1"]
    assert source._cache.get(("search", ("two", "terms"), 3)) is None


def test_throttled_first_step_is_busy_not_empty() -> None:
    source, _ = make_source(FakeResponse(status=503))
    with pytest.raises(SourceBusy):
        source.search("two terms", 3)


def test_caller_details_echoed_by_the_archive_never_leave_the_parser(caplog) -> None:
    echo = {"session_context": {"ip": "192.0.2.7", "user_agent": "probe-agent"}}
    leaky = fixture("fulltext_failed.json") | echo
    leaky["response"]["header"]["forensics"] = {"auth": {"email": "someone@example.com"}}
    source, _ = make_source(FakeResponse(body=leaky))
    with caplog.at_level("DEBUG"), pytest.raises(SourceError) as error:
        source.search("x", 8)
    for secret in ("192.0.2.7", "probe-agent", "someone@example.com"):
        assert secret not in str(error.value) and secret not in caplog.text

    from ditta_ia_adapter.ia.fulltext import _without_private_keys

    cleaned = _without_private_keys({"a": [echo, {"forensics": 1, "keep": 2}]})
    assert cleaned == {"a": [{}, {"keep": 2}]}


# --- filters and configuration ------------------------------------------------


def test_fulltext_filters_use_meta_field_names_and_language_names() -> None:
    assert language_names(["en", "FR", "xx"]) == ["English", "French", "xx"]
    assert language_filter(["English"], include_unlabeled=False, field="meta_languageSorter") == (
        "meta_languageSorter:(English)"
    )


def test_no_filters_configured_sends_the_bare_expression() -> None:
    source, session = make_source(FakeResponse(body=fixture("fulltext_no_hits.json")), languages=[], mediatypes=[])
    source.search("x", 8)
    assert queries(session) == ["!L x"]


def test_backend_selects_the_source() -> None:
    assert isinstance(ArchiveSource("https://example.com", session=FakeSession()), FullTextSource)
    assert isinstance(ArchiveSource("https://example.com", backend="metadata", session=FakeSession()), MetadataSource)
    with pytest.raises(ValueError):
        ArchiveSource("https://example.com", backend="both", session=FakeSession())


def write_config(tmp_path, archive: str = "", search: str = "") -> config.Config:
    (tmp_path / "config.toml").write_text(
        f'[node]\nendpoint = "https://example.com/ditta"\n'
        f'[archive]\ncontact_url = "https://example.com"\n{archive}\n'
        f"[search]\n{search}\n"
    )
    return config.load(tmp_path / "config.toml")


def test_config_reads_backend_and_pool(tmp_path) -> None:
    loaded = write_config(tmp_path, 'backend = "fulltext"', "candidate_pool = 200")
    assert (loaded.backend, loaded.candidate_pool) == ("fulltext", 200)
    defaults = write_config(tmp_path)
    assert (defaults.backend, defaults.candidate_pool) == ("fulltext", 100)


@pytest.mark.parametrize(
    ("archive", "search"),
    [('backend = "both"', ""), ("", "max_results = 50\ncandidate_pool = 20")],
)
def test_config_rejects_unknown_backend_and_too_small_pool(tmp_path, archive: str, search: str) -> None:
    with pytest.raises(ValueError):
        write_config(tmp_path, archive, search)


def test_search_count_and_corpus_size_use_the_same_filters() -> None:
    source, session = make_source(
        FakeResponse(body=fixture("fulltext_no_hits.json")),
        FakeResponse(body=fixture("fulltext_count_lighthouse.json")),
        FakeResponse(body=fixture("fulltext_total_texts.json")),
    )
    source.search("lighthouse", 8)
    source.count("lighthouse")
    source.total_documents()
    assert {call["url"] for call in session.calls} == {"https://archive.org/services/search/beta/page_production/"}
    assert all(query.endswith(f") AND {FILTERS}") for query in queries(session))
