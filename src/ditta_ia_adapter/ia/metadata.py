"""The metadata backend: search and term counts over advancedsearch.php.

This searches titles, descriptions and subjects, not the text inside items.
It was the only backend until 0.4.0 and is kept, unchanged in behaviour, as
the fallback for when full-text search is unavailable. Requests, spacing and
backoff live in ArchiveHttp, shared with the full-text backend.
"""

from ..adapter import Result, SourceBusy, SourceError, TermStats
from . import query as q
from .http import ArchiveHttp
from .mapping import SEARCH_FIELDS, to_result, unix_time
from .throttle import TTLCache


class MetadataSource:
    """Implements the adapter interface against advancedsearch.php."""

    def __init__(
        self,
        http: ArchiveHttp,
        cache_seconds: float = 600,
        max_terms: int = 32,
        snippet_chars: int = 300,
        languages: list[str] | None = None,
        include_unlabeled: bool = True,
        mediatypes: list[str] | None = None,
    ) -> None:
        self._http = http
        self.user_agent_suffix = http.user_agent_suffix
        self._cache = TTLCache(cache_seconds)
        self._max_terms = max_terms
        self._snippet_chars = snippet_chars
        # Applied to every request, so counts describe exactly what we serve.
        self._language = q.language_filter(q.language_codes(languages or []), include_unlabeled)
        self._mediatype = q.mediatype_filter(mediatypes or [])

    def search(self, query: str, n: int) -> list[Result]:
        terms = q.split_terms(query, self._max_terms)
        cached = self._cache.get(("search", tuple(terms), n))
        if cached is not None:
            return cached

        # Strictest first: every term, then every term but one, then any term.
        # Each step runs only while places are left, and its results go after
        # the earlier ones, so a stricter match is never pushed down by a
        # looser one.
        steps = [q.all_terms(terms)]
        if len(terms) > 1:
            steps += [q.all_but_one(terms), q.any_term(terms)]

        results: list[Result] = []
        seen: set[str] = set()
        complete = True
        for expression in filter(None, steps):
            if len(results) >= n:
                break
            try:
                docs, _ = self._search(expression, SEARCH_FIELDS, rows=n)
            except SourceBusy:
                # Throttled is never answered with an empty set; with matches
                # in hand, return those, uncached so a retry can fill the rest.
                if not results:
                    raise
                complete = False
                break
            for result in self._to_results(docs):
                if result.id not in seen:
                    seen.add(result.id)
                    results.append(result)

        results = results[:n]
        if complete:
            self._cache.put(("search", tuple(terms), n), results)
        return results

    def _to_results(self, docs: list[dict]) -> list[Result]:
        return [r for r in (to_result(doc, self._snippet_chars) for doc in docs) if r]

    def count(self, term: str) -> TermStats:
        # One request gives both numbers: numFound, and the newest item's
        # publicdate when sorted newest first.
        docs, found = self._search(
            q.escape_term(term), ["identifier", "publicdate"], rows=1, sort="publicdate desc"
        )
        newest = unix_time(docs[0].get("publicdate")) if docs else 0
        return TermStats(document_count=found, newest=newest)

    def total_documents(self) -> int:
        _, found = self._search("*:*", ["identifier"], rows=0)
        return found

    def _search(self, expression: str, fields: list[str], rows: int, sort: str | None = None):
        """One advancedsearch request, always in the paged form."""
        # page + rows matter: without them the library's default returns every
        # match sorted by identifier, which breaks best-first ordering.
        query = q.restrict(expression, self._mediatype, self._language)
        params = {"q": query, "rows": rows, "page": 1, "output": "json"}
        for index, field in enumerate(fields):
            params[f"fl[{index}]"] = field
        if sort:
            params["sort[0]"] = sort

        body = self._http.get_json("/advancedsearch.php", params)
        try:
            found = int(body["response"]["numFound"])
            docs = body["response"]["docs"]
        except (KeyError, TypeError, ValueError):
            # advancedsearch reports bad queries as a 200 with an "error" key.
            raise SourceError("unexpected archive response") from None
        return docs, found
