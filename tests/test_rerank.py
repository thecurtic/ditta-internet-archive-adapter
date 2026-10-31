"""The reranker: pure functions over hand-built candidates."""

import pytest

from ditta_ia_adapter.rerank import ADJACENCY, TITLE_MATCH, Candidate, rerank, words

QUERY = ["gore", "street", "vancouver"]


def candidate(name: str, title: str = "", passages: list[str] | None = None, rank: int = 0, **kwargs) -> Candidate:
    return Candidate(
        id=name, url=f"https://example.com/{name}", title=title or name, passages=passages or [], source_rank=rank, **kwargs
    )


def order(pool: list[Candidate], terms: list[str] = QUERY) -> list[str]:
    return [ranked.candidate.id for ranked in rerank(terms, pool)]


def score(c: Candidate, terms: list[str] = QUERY) -> tuple[float, float]:
    """(coverage, closeness)."""
    ranked = rerank(terms, [c])[0]
    return ranked.coverage, ranked.closeness


def test_words_are_lowercased_and_split_on_anything_but_letters_and_digits() -> None:
    assert words("Gore Street, VANCOUVER's c++ 1981") == ["gore", "street", "vancouver", "s", "c", "1981"]


def test_each_feature_scores_as_documented() -> None:
    assert score(candidate("x", "Nothing here", ["nor here"])) == (0, 0)
    # Two of three words, apart, not in the title.
    assert score(candidate("x", "Untitled", ["gore and vancouver"])) == (pytest.approx(2 / 3), 0)
    # All three adjacent in a passage.
    assert score(candidate("x", "Untitled", ["at 545 Gore Street, Vancouver, B.C."])) == (1, ADJACENCY)
    # Two adjacent words: half of the longest possible run.
    assert score(candidate("x", "Untitled", ["gore street and cordova, vancouver"])) == (1, ADJACENCY / 2)
    # The title counts for coverage, adjacency and title match.
    assert score(candidate("x", "Gore Street Vancouver")) == (1, ADJACENCY + TITLE_MATCH)


def test_adjacency_needs_the_query_order() -> None:
    assert score(candidate("x", "Untitled", ["vancouver street gore"])) == (1, 0)


def test_more_words_always_win_then_adjacency_beats_title() -> None:
    pool = [
        candidate("title-only", "Vancouver", ["gore"], rank=0),
        candidate("adjacent-pair", "Untitled", ["gore street"], rank=1),
        candidate("all-apart", "Untitled", ["gore, then street, then vancouver"], rank=2),
    ]
    assert order(pool) == ["all-apart", "adjacent-pair", "title-only"]
    # However long the query: seven adjacent words of eight lose to all eight apart.
    long_query = "a b c d e f g h".split()
    run = candidate("run", "Untitled", ["a b c d e f g"], rank=0)
    apart = candidate("apart", "Untitled", ["a, b, c, d, e, f, g, h"], rank=1)
    assert order([run, apart], long_query) == ["apart", "run"]


def test_ties_keep_the_source_order() -> None:
    pool = [candidate(f"c{rank}", "Untitled", ["gore street vancouver"], rank=rank) for rank in (2, 0, 1)]
    assert order(pool) == ["c0", "c1", "c2"]
    assert [r.position for r in rerank(QUERY, pool)] == [0, 1, 2]


def test_source_guaranteed_coverage_is_a_floor() -> None:
    # Matched every word in its full text, but the excerpt shows only one.
    vouched = candidate("vouched", "Untitled", ["gore"], rank=1, min_coverage=1.0)
    shown = candidate("shown", "Untitled", ["gore and street"], rank=0, min_coverage=1 / 3)
    assert order([shown, vouched]) == ["vouched", "shown"]
    # What it shows still counts when it beats the guarantee.
    assert score(candidate("x", "Untitled", ["gore street vancouver"], min_coverage=1 / 3)) == (1, ADJACENCY)


def test_guaranteed_and_observed_coverage_compare_as_equal() -> None:
    # Vouched for 2 of 3 words by the source vs seen 2 of 3: closeness decides.
    vouched = candidate("vouched", "Untitled", ["nothing"], rank=0, min_coverage=2 / 3)
    seen = candidate("seen", "Untitled", ["gore street"], rank=1)
    assert order([vouched, seen]) == ["seen", "vouched"]


def test_snippet_is_the_passage_with_most_words_then_longest_run() -> None:
    c = candidate(
        "x", passages=["only gore", "vancouver, gore and street", "gore street vancouver", "gore street vancouver again"]
    )
    assert rerank(QUERY, [c])[0].snippet == "gore street vancouver"


def test_without_passages_the_snippet_is_the_fallback() -> None:
    c = candidate("x", fallback_snippet="Doe, Jo · 1981 · texts")
    assert rerank(QUERY, [c])[0].snippet == "Doe, Jo · 1981 · texts"


def test_no_stemming_is_a_known_limit() -> None:
    assert score(candidate("x", "Untitled", ["lighthouses"]), ["lighthouse"]) == (0, 0)


def test_one_word_queries_rank_on_coverage_and_title() -> None:
    pool = [candidate("passage", "Untitled", ["a lighthouse"], rank=0), candidate("titled", "Lighthouse", rank=1)]
    assert order(pool, ["lighthouse"]) == ["titled", "passage"]


def test_a_query_with_no_words_keeps_the_source_order() -> None:
    pool = [candidate("b", rank=1), candidate("a", rank=0)]
    assert order(pool, ["!!!"]) == ["a", "b"]


def test_empty_pool() -> None:
    assert rerank(QUERY, []) == []
