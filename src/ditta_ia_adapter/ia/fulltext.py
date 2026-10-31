"""The full-text backend: search the text inside items, not just metadata.

It uses the search service behind archive.org's own search page, with its
full-text backend (`service_backend=fts`). That is the same index the
library's full-text search reaches through another host, and this one also
sorts, which counts need for the newest date. Its filters need the index's
`meta_*` field names; plain names match something else.

A search over-fetches: it fills a pool of up to `candidate_pool` items by
walking the ladder (loose phrase, AND, all-but-one, OR), one page per step,
and stops once the pool is full. Hits are per text file, so one item can
appear several times; its hits merge into one candidate that keeps its first
(best) position and all its passages. The reranker then orders the pool and
the top n are returned. If given a `record_ranks` callback (the rank stats
store), each uncached search reports where its served results sat in the
pool, so an operator can see whether the pool is big enough.
"""

from collections.abc import Callable

from ..adapter import Result, SourceBusy, SourceError, TermStats
from ..rerank import Candidate, Ranked, rerank
from . import query as q
from .http import ArchiveHttp
from .mapping import hit_to_candidate, truncate, unix_time
from .throttle import TTLCache

SEARCH_PATH = "/services/search/beta/page_production/"
# The service echoes the caller's IP address and User-Agent in
# `session_context`, and the signed-in account in `forensics`. They are
# dropped the moment a response arrives, so no log line, cache entry or
# error message can carry them.
_PRIVATE_KEYS = {"session_context", "forensics"}


class FullTextSource:
    """Implements the adapter interface against the full-text index."""

    def __init__(
        self,
        http: ArchiveHttp,
        cache_seconds: float = 600,
        max_terms: int = 32,
        snippet_chars: int = 300,
        candidate_pool: int = 100,
        languages: list[str] | None = None,
        include_unlabeled: bool = True,
        mediatypes: list[str] | None = None,
        record_ranks: Callable[[list[Ranked], int, bool], None] | None = None,
    ) -> None:
        self._http = http
        self._record_ranks = record_ranks
        self.user_agent_suffix = http.user_agent_suffix
        self._cache = TTLCache(cache_seconds)
        self._max_terms = max_terms
        self._snippet_chars = snippet_chars
        self._pool_size = candidate_pool
        # Applied to every request, so counts describe exactly what we serve.
        self._language = q.language_filter(
            q.language_names(languages or []), include_unlabeled, field="meta_languageSorter"
        )
        self._mediatype = q.mediatype_filter(mediatypes or [], field="meta_mediatype")

    def search(self, query: str, n: int) -> list[Result]:
        terms = q.split_terms(query, self._max_terms)
        cached = self._cache.get(("search", tuple(terms), n))
        if cached is not None:
            return cached

        pool, complete = self._fill_pool(terms)
        served = rerank(terms, pool)[:n]
        if self._record_ranks:
            # Below the cache, so a cached answer is never counted twice.
            self._record_ranks(served, self._pool_size, len(pool) >= self._pool_size)
        results = [self._to_result(ranked) for ranked in served]
        if complete:
            self._cache.put(("search", tuple(terms), n), results)
        return results

    def _fill_pool(self, terms: list[str]) -> tuple[list[Candidate], bool]:
        """The candidate pool in ladder order, and whether every step it needed ran.

        Each step asks for a whole pool's worth of hits, not just the places
        left: a looser step mostly finds the stricter step's items again,
        so asking for fewer would leave the pool short.
        """
        pool: dict[str, Candidate] = {}
        for step, expression, coverage in _ladder(terms):
            if len(pool) >= self._pool_size:
                break
            try:
                hits, _ = self._search(expression, rows=self._pool_size)
            except SourceBusy:
                # Throttled is never answered with an empty set; with matches
                # in hand, return those, uncached so a retry can fill the rest.
                if not pool:
                    raise
                return list(pool.values()), False
            for hit in hits:
                self._add(pool, hit, step, coverage)
        return list(pool.values()), True

    def _add(self, pool: dict[str, Candidate], hit: dict, step: str, coverage: float) -> None:
        candidate = hit_to_candidate(hit, source_rank=len(pool), step=step)
        if candidate is None:
            return
        candidate.min_coverage = coverage
        known = pool.get(candidate.id)
        if known is not None:
            # Another file of the same item, or the same file found again by
            # a looser step: keep the first position (and with it the first,
            # strictest step's coverage), gather new passages.
            known.passages += [p for p in candidate.passages if p not in known.passages]
        elif len(pool) < self._pool_size:
            pool[candidate.id] = candidate

    def _to_result(self, ranked: Ranked) -> Result:
        return Result(
            id=ranked.candidate.id,
            url=ranked.candidate.url,
            title=ranked.candidate.title,
            snippet=truncate(ranked.snippet, self._snippet_chars),
        )

    def count(self, term: str) -> TermStats:
        # One request gives both numbers: the hit total, and the newest
        # item's publicdate when sorted newest first.
        hits, total = self._search(q.escape_term(term), rows=1, sort="publicdate:desc")
        newest = unix_time(_fields(hits[0]).get("publicdate")) if hits else 0
        return TermStats(document_count=total, newest=newest)

    def total_documents(self) -> int:
        _, total = self._search("*:*", rows=1)
        return total

    def _search(self, expression: str, rows: int, sort: str | None = None) -> tuple[list[dict], int]:
        """One full-text request, always in the paged form."""
        # `!L` tells the service the query is Lucene syntax.
        params: dict = {
            "service_backend": "fts",
            "user_query": "!L " + q.restrict(expression, self._mediatype, self._language),
            "hits_per_page": rows,
            "page": 1,
            "aggregations": "false",
        }
        if sort:
            params["sort"] = sort

        body = _without_private_keys(self._http.get_json(SEARCH_PATH, params))
        try:
            response = body["response"]
            if response["header"]["succeeded"] is not True:
                raise SourceError("archive full-text search did not succeed")
            hits = response["body"]["hits"]
            total = int(hits["total"])
            found = [hit for hit in hits["hits"] if isinstance(hit, dict)]
        except (KeyError, TypeError, ValueError):
            raise SourceError("unexpected archive response") from None
        return found, total


def _ladder(terms: list[str]) -> list[tuple[str, str, float]]:
    """(step name, expression, coverage) triples, strictest first.

    Coverage is the share of the terms every hit of that step is known to
    hold in its full text, which its highlighted passages may not show. The
    reranker takes it as a floor. Pointless steps are left out.
    """
    k = len(terms)
    if k == 1:
        return [("and", q.all_terms(terms), 1.0)]
    steps = [
        ("phrase", q.loose_phrase(terms), 1.0),
        ("and", q.all_terms(terms), 1.0),
        ("all_but_one", q.all_but_one(terms), (k - 1) / k),
        ("or", q.any_term(terms), 1 / k),
    ]
    return [step for step in steps if step[1]]


def _fields(hit: dict) -> dict:
    fields = hit.get("fields")
    return fields if isinstance(fields, dict) else {}


def _without_private_keys(value: object) -> object:
    if isinstance(value, dict):
        return {k: _without_private_keys(v) for k, v in value.items() if k not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_without_private_keys(v) for v in value]
    return value
