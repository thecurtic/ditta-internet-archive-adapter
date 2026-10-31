"""Reorder a pool of search candidates. Knows nothing about any source.

A source over-fetches a pool of candidates in its own order, and this module
decides which of them to serve. For now (0.4.0 in progress) it only defines
the candidate; the scoring arrives in the next step, and until then the pool
is served in the source's order.
"""

from dataclasses import dataclass, field


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

    @property
    def snippet(self) -> str:
        return self.passages[0] if self.passages else self.fallback_snippet
