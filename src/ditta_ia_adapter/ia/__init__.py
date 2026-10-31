"""The Internet Archive client: every Archive-specific rule lives here."""

from collections.abc import Callable

from internetarchive import ArchiveSession

from .fulltext import FullTextSource
from .http import ArchiveHttp
from .metadata import MetadataSource
from ..rerank import Ranked
from .throttle import Throttle

BACKENDS = ("fulltext", "metadata")


# Named like the class it replaced, so callers construct it the same way.
def ArchiveSource(
    contact_url: str,
    backend: str = "metadata",
    access_key: str | None = None,
    secret_key: str | None = None,
    max_concurrent: int = 2,
    min_interval_seconds: float = 1.0,
    cache_seconds: float = 600,
    timeout_seconds: float = 20,
    max_terms: int = 32,
    snippet_chars: int = 300,
    candidate_pool: int = 100,
    agent: str | None = None,
    languages: list[str] | None = None,
    include_unlabeled: bool = True,
    mediatypes: list[str] | None = None,
    session: ArchiveSession | None = None,
    throttle: Throttle | None = None,
    record_ranks: Callable[[list[Ranked], int, bool], None] | None = None,
) -> FullTextSource | MetadataSource:
    """The Archive source for `backend`, sharing one politeness layer.

    `agent` names the AI model driving the client (see ArchiveHttp).
    `record_ranks` receives each full-text search's served results; the
    metadata backend has no pool, so it records nothing.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown archive backend {backend!r}; use one of {', '.join(BACKENDS)}")
    http = ArchiveHttp(
        contact_url,
        access_key=access_key,
        secret_key=secret_key,
        max_concurrent=max_concurrent,
        min_interval_seconds=min_interval_seconds,
        timeout_seconds=timeout_seconds,
        agent=agent,
        session=session,
        throttle=throttle,
    )
    shared = dict(
        cache_seconds=cache_seconds,
        max_terms=max_terms,
        snippet_chars=snippet_chars,
        languages=languages,
        include_unlabeled=include_unlabeled,
        mediatypes=mediatypes,
    )
    if backend == "fulltext":
        return FullTextSource(http, candidate_pool=candidate_pool, record_ranks=record_ranks, **shared)
    return MetadataSource(http, **shared)


__all__ = ["ArchiveSource", "BACKENDS", "FullTextSource", "MetadataSource"]
