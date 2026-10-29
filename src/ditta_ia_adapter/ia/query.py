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


def all_terms(terms: list[str]) -> str:
    return " AND ".join(escape_term(term) for term in terms)


def any_term(terms: list[str]) -> str:
    return " OR ".join(escape_term(term) for term in terms)
