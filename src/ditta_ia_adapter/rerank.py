"""Reorder a pool of search candidates. Knows nothing about any source.

A source over-fetches a pool of candidates in its own order, and this module
decides which of them to serve, and with which passage as the snippet.

Candidates are ordered by three keys in turn:

1. coverage: the share of the query's words found in its title or passages,
   never less than the share the source guarantees (`min_coverage`);
2. closeness: a weighted sum of adjacency (the longest run of query words
   found next to each other and in the query's order, in the title or any
   one passage, 1 for the whole query) and title match (the share of the
   query's words found in its title), each between 0 and 1;
3. the source's own order.

Coverage is a key of its own, not a weighted feature, so a document missing
one of the words never outranks one holding them all, however long the
query: with weights, a long enough run of the other words could outweigh
one missing word. Matching is exact after lowercasing, with no
stemming and no stop words: "lighthouses" does not match "lighthouse", and
"of" counts as much as any other word. Text is split into words on anything
that is not a letter or digit, so "Gore Street, Vancouver" is three adjacent
words and "c++" is the word "c".

Passages are excerpts the source chose, not the whole document, which is why
the source can vouch for coverage it saw in the full text.
"""

import re
from dataclasses import dataclass, field

# Weights within closeness. Adjacency leads: the words as a phrase is the
# strongest sign of the right page. A title match lifts books about the
# subject over books that mention it once.
ADJACENCY = 3.0
TITLE_MATCH = 2.0
# Coverage is compared at this precision, so a guaranteed 2/3 and an
# observed 2 of 3 words count as equal.
_COVERAGE_DIGITS = 6

_WORD = re.compile(r"\w+")


@dataclass
class Candidate:
    """One document in the pool. Every string is already safe display text."""

    id: str
    url: str
    title: str
    # Matching excerpts from the document's text, in the source's order, with
    # any match markers removed. Empty when the source matched only metadata.
    passages: list[str] = field(default_factory=list)
    # A snippet for when there is no passage: a description or metadata line.
    fallback_snippet: str = ""
    # 0-based position in the source's own order across the whole pool.
    source_rank: int = 0
    # The ladder step that first found it, e.g. "phrase" or "and".
    step: str = ""
    # The share of query words the source knows the full document holds,
    # e.g. 1.0 for a document that matched every word.
    min_coverage: float = 0.0


@dataclass(frozen=True)
class Ranked:
    """A candidate in its served position, with the snippet chosen for it."""

    candidate: Candidate
    coverage: float
    closeness: float
    snippet: str
    position: int  # 0-based


def rerank(terms: list[str], pool: list[Candidate]) -> list[Ranked]:
    """The whole pool, best first."""
    query = words(" ".join(terms))
    scored = [(*_score(query, candidate), candidate) for candidate in pool]
    scored.sort(key=lambda row: (-row[0], -row[1], row[2].source_rank))
    return [
        Ranked(candidate, coverage, closeness, _best_snippet(query, candidate), position)
        for position, (coverage, closeness, candidate) in enumerate(scored)
    ]


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _score(query: list[str], candidate: Candidate) -> tuple[float, float]:
    """(coverage, closeness) for one candidate."""
    wanted = set(query)
    title = words(candidate.title)
    texts = [title] + [words(passage) for passage in candidate.passages]
    found = set().union(*texts)
    coverage = round(max(_share(wanted, found), candidate.min_coverage), _COVERAGE_DIGITS)
    adjacency = max(_adjacency(query, text) for text in texts)
    return coverage, ADJACENCY * adjacency + TITLE_MATCH * _share(wanted, set(title))


def _best_snippet(query: list[str], candidate: Candidate) -> str:
    """The passage holding the most query words, then the longest run of them."""
    if not candidate.passages:
        return candidate.fallback_snippet
    wanted = set(query)

    def quality(passage: str) -> tuple[float, float]:
        text = words(passage)
        return _share(wanted, set(text)), _adjacency(query, text)

    return max(candidate.passages, key=quality)  # max keeps the first of equals


def _share(wanted: set[str], found: set[str]) -> float:
    return len(wanted & found) / len(wanted) if wanted else 0.0


def _adjacency(query: list[str], text: list[str]) -> float:
    """How much of the query appears as one unbroken, in-order run, 0 to 1.

    A run of one word is no adjacency at all, so it scores 0, and a
    one-word query always scores 0 here.
    """
    if len(query) < 2:
        return 0.0
    longest = 0
    for start in range(len(text)):
        for first in range(len(query)):
            length = 0
            while (
                first + length < len(query)
                and start + length < len(text)
                and text[start + length] == query[first + length]
            ):
                length += 1
            longest = max(longest, length)
    return max(0, longest - 1) / (len(query) - 1)
