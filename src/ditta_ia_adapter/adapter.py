"""The adapter interface: the only thing the DITTA front knows about a source.

A source (the Internet Archive here, a Wayback adapter some day) implements
two calls. The front never sees the source's query syntax, field names or
rate limits; it sees these types and these exceptions.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Result:
    """One DITTA result. Every string is already safe display text."""

    id: str
    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class TermStats:
    """Statistics format 0 for one term."""

    document_count: int
    newest: int  # Unix seconds of the newest document holding the term; 0 if none


class SourceBusy(Exception):
    """The source is backing off; the caller should answer `rate_limited`.

    Never turn this into an empty result set: a broker would read that as
    "no matches".
    """

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"source busy, retry after {retry_after}s")
        self.retry_after = retry_after


class SourceError(Exception):
    """The source failed in a way retrying soon will not fix."""


class UnusableQuery(Exception):
    """The query cannot be searched (empty, or beyond local limits)."""


class Source(Protocol):
    def search(self, query: str, n: int) -> list[Result]:
        """At most `n` results for the raw query string, best first."""
        ...

    def count(self, term: str) -> TermStats:
        """Document count and freshness for one bare term."""
        ...

    def total_documents(self) -> int:
        """Size of the whole corpus, for the announcement's corpus summary."""
        ...
