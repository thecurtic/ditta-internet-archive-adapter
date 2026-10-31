"""Operator configuration, read from a TOML file plus environment variables.

Secrets never live in the file: Internet Archive keys come from the
internetarchive library's own variables, IA_ACCESS_KEY_ID and
IA_SECRET_ACCESS_KEY (IA_ACCESS_KEY / IA_SECRET_KEY, this adapter's first
names for them, still work), and the node key from its own key file.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Config:
    endpoint: str
    contact_url: str
    key_file: Path
    host: str = "127.0.0.1"
    port: int = 8080
    # Archive
    # "metadata" searches titles, descriptions and subjects; "fulltext" the
    # text inside items. Searches, counts and corpus size all use this one.
    backend: str = "metadata"
    access_key: str | None = None
    secret_key: str | None = None
    max_concurrent: int = 2
    min_request_interval_seconds: float = 1.0
    cache_seconds: float = 600
    timeout_seconds: float = 20
    # Search
    max_results: int = 50
    max_query_terms: int = 32
    snippet_chars: int = 300
    candidate_pool: int = 100  # full-text items fetched per search before picking the top n
    filter_languages: bool = True  # limit searches to `languages`
    include_unlabeled: bool = True  # ...but keep items with no language field
    mediatypes: list[str] = field(default_factory=lambda: ["texts"])  # [] searches all
    # Corpus statistics
    terms_db: Path = Path("terms.sqlite")
    languages: list[str] = field(default_factory=lambda: ["en"])
    seed_terms: list[str] = field(default_factory=list)
    count_interval_seconds: float = 5
    refresh_after_days: int = 7
    expire_after_days: int = 90
    max_corpus_terms: int = 5000
    # Discovery
    peers_db: Path = Path("peers.sqlite")
    seed_endpoints: list[str] = field(default_factory=list)
    reannounce_hours: float = 6
    min_post_interval_seconds: float = 1
    allow_private_addresses: bool = False
    # Logging
    log_queries: bool = False

    @property
    def path_prefix(self) -> str:
        """The endpoint's path, which the server strips before routing."""
        return urlsplit(self.endpoint).path.rstrip("/")


def load(path: Path) -> Config:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    base = path.parent
    node, archive = raw.get("node", {}), raw.get("archive", {})
    search, corpus = raw.get("search", {}), raw.get("corpus", {})
    discovery, server = raw.get("discovery", {}), raw.get("server", {})

    for name, value in (("node.endpoint", node.get("endpoint")), ("archive.contact_url", archive.get("contact_url"))):
        if not value:
            raise ValueError(f"{name} is required")

    config = Config(
        endpoint=node["endpoint"].rstrip("/"),
        contact_url=archive["contact_url"],
        key_file=base / node.get("key_file", "node.key"),
        access_key=_env("IA_ACCESS_KEY_ID", "IA_ACCESS_KEY"),
        secret_key=_env("IA_SECRET_ACCESS_KEY", "IA_SECRET_KEY"),
        terms_db=base / corpus.get("db_path", "terms.sqlite"),
        peers_db=base / discovery.get("db_path", "peers.sqlite"),
    )
    _apply(config, server, ["host", "port"])
    _apply(config, archive, ["backend", "max_concurrent", "min_request_interval_seconds", "cache_seconds", "timeout_seconds"])
    _apply(config, search, ["max_results", "max_query_terms", "snippet_chars", "candidate_pool", "filter_languages", "include_unlabeled", "mediatypes"])
    _apply(config, corpus, [
        "languages", "seed_terms", "count_interval_seconds",
        "refresh_after_days", "expire_after_days", "max_corpus_terms",
    ])
    _apply(config, discovery, [
        "seed_endpoints", "reannounce_hours", "min_post_interval_seconds", "allow_private_addresses",
    ])
    _apply(config, raw.get("logging", {}), ["log_queries"])
    _check(config)
    return config


def _check(config: Config) -> None:
    if config.backend not in ("fulltext", "metadata"):
        raise ValueError('archive.backend must be "fulltext" or "metadata"')
    # The top n come out of the pool, so a smaller pool could not fill them.
    if config.candidate_pool < config.max_results:
        raise ValueError("search.candidate_pool must be at least search.max_results")


def _env(*names: str) -> str | None:
    """The first of these environment variables that is set and non-empty."""
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    return None


def _apply(config: Config, section: dict, names: list[str]) -> None:
    for name in names:
        if name in section:
            setattr(config, name, section[name])
