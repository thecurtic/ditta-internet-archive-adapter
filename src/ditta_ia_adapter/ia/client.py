"""The Archive source: search and term counts over advancedsearch.php.

Requests go through the official `internetarchive` library's ArchiveSession,
which sets the library's User-Agent (we append ours) and holds credentials.
We call the session directly rather than through the library's Search
object for two reasons: Search parses the body without looking at the HTTP
status, so a 429 and its Retry-After would be invisible to us; and it
mounts its own retry adapter, which would retry behind our backoff. Here
the library's retries are off and Throttle is the only retry policy.
"""

import logging
from datetime import datetime, timezone

import requests
from internetarchive import ArchiveSession
from internetarchive.auth import S3Auth

from .. import TOOL_NAME, __version__
from ..adapter import Result, SourceBusy, SourceError, TermStats
from . import query as q
from .mapping import SEARCH_FIELDS, to_result
from .throttle import Throttle, TTLCache, parse_retry_after

log = logging.getLogger(__name__)


class ArchiveSource:
    """Implements the adapter interface against the Internet Archive."""

    def __init__(
        self,
        contact_url: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        max_concurrent: int = 2,
        cache_seconds: float = 600,
        timeout_seconds: float = 20,
        max_terms: int = 32,
        snippet_chars: int = 300,
        languages: list[str] | None = None,
        include_unlabeled: bool = True,
        session: ArchiveSession | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        self.user_agent_suffix = f"{TOOL_NAME}/{__version__} (+{contact_url})"
        self._session = session or _make_session(self.user_agent_suffix, access_key, secret_key)
        self._auth = S3Auth(access_key, secret_key) if access_key and secret_key else None
        self._throttle = throttle or Throttle(max_concurrent)
        self._cache = TTLCache(cache_seconds)
        self._timeout = timeout_seconds
        self._max_terms = max_terms
        self._snippet_chars = snippet_chars
        # Applied to every request, so counts describe exactly what we serve.
        self._language = q.language_filter(languages or [], include_unlabeled)

    def search(self, query: str, n: int) -> list[Result]:
        terms = q.split_terms(query, self._max_terms)
        cached = self._cache.get(("search", tuple(terms), n))
        if cached is not None:
            return cached

        docs, found = self._search(q.all_terms(terms), SEARCH_FIELDS, rows=n)
        if found == 0 and len(terms) > 1:
            docs, found = self._search(q.any_term(terms), SEARCH_FIELDS, rows=n)

        results = [r for r in (to_result(doc, self._snippet_chars) for doc in docs) if r]
        results = results[:n]
        self._cache.put(("search", tuple(terms), n), results)
        return results

    def count(self, term: str) -> TermStats:
        # One request gives both numbers: numFound, and the newest item's
        # publicdate when sorted newest first.
        docs, found = self._search(
            q.escape_term(term), ["identifier", "publicdate"], rows=1, sort="publicdate desc"
        )
        newest = _unix_time(docs[0].get("publicdate")) if docs else 0
        return TermStats(document_count=found, newest=newest)

    def total_documents(self) -> int:
        _, found = self._search("*:*", ["identifier"], rows=0)
        return found

    def _search(self, expression: str, fields: list[str], rows: int, sort: str | None = None):
        """One advancedsearch request, always in the paged form."""
        # page + rows matter: without them the library's default returns every
        # match sorted by identifier, which breaks best-first ordering.
        params = {"q": q.restrict(expression, self._language), "rows": rows, "page": 1, "output": "json"}
        for index, field in enumerate(fields):
            params[f"fl[{index}]"] = field
        if sort:
            params["sort[0]"] = sort

        self._throttle.acquire()
        try:
            response = self._session.get(
                f"{self._session.protocol}//{self._session.host}/advancedsearch.php",
                params=params,
                auth=self._auth,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            self._throttle.failed()
            raise SourceBusy(retry_after=_seconds_left(self._throttle)) from exc
        finally:
            self._throttle.release()

        if response.status_code == 429 or response.status_code >= 500:
            self._throttle.failed(parse_retry_after(response.headers.get("Retry-After")))
            log.warning("archive returned %s; backing off", response.status_code)
            raise SourceBusy(retry_after=_seconds_left(self._throttle))
        if response.status_code != 200:
            raise SourceError(f"archive returned HTTP {response.status_code}")

        try:
            body = response.json()
            found = int(body["response"]["numFound"])
            docs = body["response"]["docs"]
        except (ValueError, KeyError, TypeError):
            # advancedsearch reports bad queries as a 200 with an "error" key.
            raise SourceError("unexpected archive response") from None
        self._throttle.succeeded()
        return docs, found


def _make_session(user_agent_suffix: str, access_key: str | None, secret_key: str | None) -> ArchiveSession:
    config: dict = {"general": {"user_agent_suffix": user_agent_suffix}}
    if access_key and secret_key:
        config["s3"] = {"access": access_key, "secret": secret_key}
    # max_retries=0: Throttle owns every retry decision (see module docstring).
    return ArchiveSession(config=config, http_adapter_kwargs={"max_retries": 0})


def _seconds_left(throttle: Throttle) -> int:
    return max(1, throttle.seconds_until_resume())


def _unix_time(value: object) -> int:
    """Archive dates look like 2019-03-05T12:00:00Z; 0 when unusable."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return 0
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int(when.timestamp()))
