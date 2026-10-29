"""Shared fakes. Nothing here contacts the network."""

import json
from pathlib import Path

import pytest

from ditta_ia_adapter.adapter import Result, SourceBusy, TermStats

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, status: int = 200, body: object = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Stands in for ArchiveSession: replays queued responses, records calls."""

    protocol = "https:"
    host = "archive.org"

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url: str, params: dict, auth=None, timeout=None) -> FakeResponse:
        self.calls.append({"url": url, "params": dict(params)})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSource:
    """Stands in for the adapter interface in front tests."""

    def __init__(self, results: list[Result] | None = None, busy: bool = False) -> None:
        self.results = results or []
        self.busy = busy
        self.searches: list[tuple[str, int]] = []

    def search(self, query: str, n: int) -> list[Result]:
        self.searches.append((query, n))
        if self.busy:
            raise SourceBusy(retry_after=30)
        if not query.split():
            from ditta_ia_adapter.adapter import UnusableQuery

            raise UnusableQuery("empty query")
        return self.results[:n]

    def count(self, term: str) -> TermStats:
        return TermStats(document_count=len(term), newest=1786000000)

    def total_documents(self) -> int:
        return 1000


@pytest.fixture
def sample_results() -> list[Result]:
    return [
        Result(f"item-{i}", f"https://archive.org/details/item-{i}", f"Title {i}", f"Snippet {i}")
        for i in range(10)
    ]
