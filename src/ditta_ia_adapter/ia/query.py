"""Translate a DITTA query string into an Archive search expression.

The query string is attacker-controlled. It is treated as a list of terms:
split on whitespace, each term escaped so it can only ever match as text,
then combined by the search ladder (a loose phrase, AND, all-but-one, OR)
and ANDed with filters that come from configuration. The raw string is
never passed through, so a caller cannot send field queries, wildcards,
ranges or other expensive searches to the Archive under our name.
"""

from itertools import combinations

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


# How the metadata index's `language` field spells a BCP-47 tag. Values are
# matched case-blind, so "English" and "english" are one value. A tag missing
# here is matched as written.
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

# The full-text index's `meta_languageSorter` holds one normalised name per
# item instead. Only "English" has been checked against the live index; the
# others follow the same pattern. A tag missing here is matched as written.
_LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ar": "Arabic",
}


def language_codes(tags: list[str]) -> list[str]:
    """Every spelling of these tags in the metadata index's `language` field."""
    return [v for tag in tags for v in _LANGUAGE_VALUES.get(tag.lower(), [tag])]


def language_names(tags: list[str]) -> list[str]:
    """These tags as the full-text index's `meta_languageSorter` names them."""
    return [_LANGUAGE_NAMES.get(tag.lower(), tag) for tag in tags]


def language_filter(values: list[str], include_unlabeled: bool, field: str = "language") -> str | None:
    """A clause limiting results to items labelled with `values`, or None.

    About a third of Archive items carry no language at all (much audio and
    video), so by default unlabelled items are kept and only items labelled
    in some other language are left out.
    """
    if not values:
        return None
    labelled = f"{field}:(" + " OR ".join(escape_term(v) for v in values) + ")"
    if include_unlabeled:
        return f"({labelled} OR (*:* -{field}:*))"
    return labelled


def mediatype_filter(mediatypes: list[str], field: str = "mediatype") -> str | None:
    """A clause limiting results to these media types, or None for all.

    `texts` covers books, scanned pages and other documents; leaving out
    `movies` and `audio` keeps video and podcasts out of the results.
    """
    if not mediatypes:
        return None
    return f"{field}:(" + " OR ".join(escape_term(m) for m in mediatypes) + ")"


def restrict(expression: str, *clauses: str | None) -> str:
    """The search expression, ANDed with every filter clause that is set."""
    clauses = tuple(c for c in clauses if c)
    if not clauses:
        return expression
    return " AND ".join((f"({expression})",) + clauses)


# How far apart the phrase step lets its terms be: "gore street vancouver"~3
# also matches "Gore Street, Vancouver" and "Vancouver's Gore Street".
PHRASE_SLOP = 3


def loose_phrase(terms: list[str]) -> str:
    """The terms as one phrase, allowing a few words between or around them."""
    return '"' + " ".join(escape_term(term) for term in terms) + f'"~{PHRASE_SLOP}'


def all_terms(terms: list[str]) -> str:
    return " AND ".join(escape_term(term) for term in terms)


# All-but-one is an OR of n clauses of n-1 terms each, so it grows with the
# square of the term count. Past this many terms the expression gets long
# enough to be an expensive search, and the search goes straight to any-term.
MAX_TERMS_FOR_ALL_BUT_ONE = 8


def all_but_one(terms: list[str]) -> str | None:
    """Items matching every term but one, or None when the step is pointless.

    With two terms, all-but-one is the same as any-term.
    """
    if not 3 <= len(terms) <= MAX_TERMS_FOR_ALL_BUT_ONE:
        return None
    clauses = (all_terms(list(subset)) for subset in combinations(terms, len(terms) - 1))
    return " OR ".join(f"({clause})" for clause in clauses)


def any_term(terms: list[str]) -> str:
    return " OR ".join(escape_term(term) for term in terms)
