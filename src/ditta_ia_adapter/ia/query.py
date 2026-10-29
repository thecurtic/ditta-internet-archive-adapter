"""Translate a DITTA query string into an Archive search expression.

The query string is attacker-controlled. It is treated as a bag of terms:
split on whitespace, each term escaped so it can only ever match as text,
then joined with AND (or OR on the single fallback). The raw string is
never passed through, so a caller cannot send field queries, wildcards,
ranges or other expensive searches to the Archive under our name.
"""

from ..adapter import UnusableQuery

# Lucene's special characters. "&&" and "||" are covered by "&" and "|".
_SPECIAL = set('+-&|!(){}[]^"~*?:\\/')
# Bare uppercase operator words are syntax even with no special character.
# Lowercasing makes them plain terms; the Archive's search is case-blind.
_OPERATORS = {"AND", "OR", "NOT"}


def split_terms(query: str, max_terms: int) -> list[str]:
    """The query's terms, in order. Raises UnusableQuery if none or too many."""
    terms = query.split()
    if not terms:
        raise UnusableQuery("empty query")
    if len(terms) > max_terms:
        raise UnusableQuery(f"more than {max_terms} terms")
    return terms


def escape_term(term: str) -> str:
    if term in _OPERATORS:
        return term.lower()
    return "".join("\\" + char if char in _SPECIAL else char for char in term)


# How the Archive's `language` field spells a BCP-47 tag. Values are matched
# case-blind, so "English" and "english" are one value. A tag missing here is
# matched as written.
_LANGUAGE_VALUES = {
    "en": ["eng", "english", "en"],
    "fr": ["fre", "fra", "french", "fr"],
    "de": ["ger", "deu", "german", "de"],
    "es": ["spa", "spanish", "es"],
    "it": ["ita", "italian", "it"],
    "pt": ["por", "portuguese", "pt"],
    "nl": ["dut", "nld", "dutch", "nl"],
    "ru": ["rus", "russian", "ru"],
    "ja": ["jpn", "japanese", "ja"],
    "zh": ["chi", "zho", "chinese", "zh"],
    "ar": ["ara", "arabic", "ar"],
}


def language_filter(tags: list[str], include_unlabeled: bool) -> str | None:
    """A clause limiting results to items in `tags`, or None for no limit.

    About a third of Archive items carry no `language` field at all (much
    audio and video), so by default unlabelled items are kept and only items
    labelled in some other language are left out.
    """
    values = [v for tag in tags for v in _LANGUAGE_VALUES.get(tag.lower(), [tag])]
    if not values:
        return None
    labelled = "language:(" + " OR ".join(escape_term(v) for v in values) + ")"
    if include_unlabeled:
        return f"({labelled} OR (*:* -language:*))"
    return labelled


def restrict(expression: str, clause: str | None) -> str:
    return f"({expression}) AND {clause}" if clause else expression


def all_terms(terms: list[str]) -> str:
    return " AND ".join(escape_term(term) for term in terms)


def any_term(terms: list[str]) -> str:
    return " OR ".join(escape_term(term) for term in terms)
