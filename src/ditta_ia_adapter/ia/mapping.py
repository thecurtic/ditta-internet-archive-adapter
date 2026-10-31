"""Map Archive search documents and full-text hits to results.

Every string from the Archive is untrusted display content: markup is
stripped and never evaluated, and the URL is built from the identifier
rather than taken from metadata.
"""

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote

from ..adapter import Result
from ..rerank import Candidate

DETAILS_URL = "https://archive.org/details/"
# Everything a result needs, fetched in the one search request. The last
# four only feed the snippet when an item has no description.
SEARCH_FIELDS = ["identifier", "title", "description", "creator", "date", "mediatype", "subject"]
MAX_SUBJECTS = 3
_WHITESPACE = re.compile(r"\s+")
# C0/C1 controls other than whitespace, which collapsing already removes.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")
# Some items carry `subject` as one string that looks like a Python list.
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
# The full-text index marks matches in its passages {{{like this}}}.
_MATCH_MARKERS = re.compile(r"\{\{\{|\}\}\}")


def to_result(doc: dict, snippet_chars: int) -> Result | None:
    """A Result for one search document, or None if it is not worth returning.

    An item is worth returning when it has an identifier (so a URL) and some
    text for a snippet: its description, or failing that a line built from
    its creator, year, media type and subjects.
    """
    identifier = _first(doc.get("identifier"))
    if not identifier:
        return None
    snippet = clean_text(_first(doc.get("description"))) or _metadata_line(doc)
    if not snippet:
        return None
    title = clean_text(_first(doc.get("title"))) or identifier
    snippet = truncate(snippet, snippet_chars)
    return Result(
        id=identifier,
        url=DETAILS_URL + quote(identifier, safe=""),
        title=title,
        snippet=snippet,
    )


def hit_to_candidate(hit: dict, source_rank: int, step: str) -> Candidate | None:
    """A Candidate for one full-text hit, or None if it is not worth returning.

    The same rule as to_result: an identifier, and some snippet text, here
    a matching passage or failing that the description or metadata line.
    """
    fields = hit.get("fields")
    if not isinstance(fields, dict):
        return None
    identifier = _first(fields.get("identifier"))
    if not identifier:
        return None
    passages = [p for p in (_clean_passage(text) for text in _highlights(hit)) if p]
    fallback = clean_text(_first(fields.get("description"))) or _metadata_line(fields)
    if not passages and not fallback:
        return None
    return Candidate(
        id=identifier,
        url=DETAILS_URL + quote(identifier, safe=""),
        title=clean_text(_first(fields.get("title"))) or identifier,
        passages=passages,
        fallback_snippet=fallback,
        source_rank=source_rank,
        step=step,
    )


def _highlights(hit: dict) -> list[str]:
    highlight = hit.get("highlight")
    texts = highlight.get("text") if isinstance(highlight, dict) else None
    return [text for text in texts if isinstance(text, str)] if isinstance(texts, list) else []


def _clean_passage(text: str) -> str:
    return clean_text(_MATCH_MARKERS.sub("", text))


def unix_time(value: object) -> int:
    """Archive dates look like 2019-03-05T12:00:00Z; 0 when unusable."""
    value = _first(value)
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int(when.timestamp()))


def clean_text(value: str) -> str:
    """Plain text: HTML stripped, entities decoded, whitespace collapsed."""
    extractor = _TextExtractor()
    extractor.feed(value)
    extractor.close()
    text = _CONTROLS.sub("", "".join(extractor.parts))
    return _WHITESPACE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    """Cut at a word boundary, marking the cut with an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


def _metadata_line(doc: dict) -> str:
    """e.g. "Diedreck, Jay · 2017 · texts · Lighthouses, Maine"."""
    year = _first(doc.get("date"))[:4]
    parts = [
        clean_text(_first(doc.get("creator"))),
        year if year.isdigit() else "",
        clean_text(_first(doc.get("mediatype"))),
        ", ".join(_subjects(doc.get("subject"))),
    ]
    return " · ".join(part for part in parts if part)


def _subjects(value: object) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            value = [a or b for a, b in _QUOTED.findall(stripped)]
        else:
            value = [value]
    if not isinstance(value, list):
        return []
    cleaned = (clean_text(item) for item in value if isinstance(item, str))
    return [item for item in cleaned if item][:MAX_SUBJECTS]


def _first(value: object) -> str:
    """Archive fields may be a string or a list of strings; take the first."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return value if isinstance(value, str) else ""


class _TextExtractor(HTMLParser):
    """Collects text nodes, skipping the contents of script and style."""

    _SKIPPED = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIPPED:
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)
