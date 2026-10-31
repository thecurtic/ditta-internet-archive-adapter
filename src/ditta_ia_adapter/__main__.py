"""Command line: `keygen` makes a node key, `serve` runs the adapter,
`rank-report` prints the rank stats."""

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import TOOL_NAME, __version__, config as config_module
from .front import AnnouncementStore, Gossip, Node, make_server
from .front.node import NodeSettings
from .adapter import Source
from .ia import ArchiveSource
from .rankstats import RankStats, build_report
from .terms import CountWorker, TermStore, reset_if_scope_changed
from .wire import NodeKey


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL_NAME)
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="create a new node key file")
    keygen.add_argument("path", type=Path)
    serve = commands.add_parser("serve", help="run the adapter")
    serve.add_argument("--config", type=Path, default=Path("config.toml"))
    report = commands.add_parser("rank-report", help="print where served results sat in the Archive's order")
    report.add_argument("--config", type=Path, default=Path("config.toml"))
    report.add_argument("--days", type=int, default=30, help="how many days back to report (default 30)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "keygen":
        key = NodeKey.generate()
        key.save(args.path)
        print(key.pubkey)
        return 0
    if args.command == "rank-report":
        return rank_report(config_module.load(args.config), args.days)
    return serve_forever(config_module.load(args.config))


def rank_report(config: config_module.Config, days: int) -> int:
    if not config.stats_db.exists():
        print(f"No rank stats yet: {config.stats_db} does not exist.")
        return 0
    stats = RankStats(str(config.stats_db), read_only=True)
    try:
        print(build_report(stats, max(1, days)))
    finally:
        stats.close()
    return 0


@dataclass
class App:
    """Everything `serve` runs, wired together but not yet started."""

    server: ThreadingHTTPServer
    worker: CountWorker
    gossip: Gossip
    node: Node

    def start_background(self) -> None:
        self.worker.start()
        self.gossip.start()

    def stop(self) -> None:
        self.server.server_close()
        self.gossip.stop()
        self.worker.stop()


def build_app(config: config_module.Config, source: Source | None = None) -> App:
    """Wire the layers together. Tests pass a fake `source` to stay offline."""
    key = NodeKey.load(config.key_file)
    user_agent = f"{TOOL_NAME}/{__version__} (+{config.contact_url})"

    if source is None:
        # Only the full-text backend has a pool to measure.
        stats = RankStats(str(config.stats_db), keep_days=config.stats_keep_days) if config.backend == "fulltext" else None
        source = ArchiveSource(
            contact_url=config.contact_url,
            backend=config.backend,
            access_key=config.access_key,
            secret_key=config.secret_key,
            max_concurrent=config.max_concurrent,
            min_interval_seconds=config.min_request_interval_seconds,
            cache_seconds=config.cache_seconds,
            timeout_seconds=config.timeout_seconds,
            max_terms=config.max_query_terms,
            snippet_chars=config.snippet_chars,
            candidate_pool=config.candidate_pool,
            languages=config.languages if config.filter_languages else [],
            include_unlabeled=config.include_unlabeled,
            mediatypes=config.mediatypes,
            record_ranks=stats.record if stats else None,
        )
    terms = TermStore(str(config.terms_db))
    reset_if_scope_changed(terms, counts_scope(config))
    terms.note_terms(config.seed_terms)
    peers = AnnouncementStore(str(config.peers_db))

    reannounce = config.reannounce_hours * 3600
    node = Node(
        key,
        NodeSettings(
            endpoint=config.endpoint,
            languages=config.languages,
            max_results=config.max_results,
            max_corpus_terms=config.max_corpus_terms,
            reannounce_seconds=reannounce,
            log_queries=config.log_queries,
        ),
        source,
        terms,
        peers,
    )
    gossip = Gossip(
        peers,
        node.announcement,
        key.pubkey,
        user_agent,
        config.seed_endpoints,
        reannounce_seconds=reannounce,
        min_post_interval=config.min_post_interval_seconds,
        allow_private=config.allow_private_addresses,
    )
    node.attach_gossip(gossip)
    worker = CountWorker(
        terms,
        source,
        interval_seconds=config.count_interval_seconds,
        refresh_after_days=config.refresh_after_days,
        expire_after_days=config.expire_after_days,
    )

    server = make_server(node, config.host, config.port, config.path_prefix)
    return App(server, worker, gossip, node)


def counts_scope(config: config_module.Config) -> int:
    """A fingerprint of the corpus the term counts describe.

    Changing the backend or any search filter changes it, which makes the
    term store start its statistics again (see reset_if_scope_changed).
    """
    described = {
        "backend": config.backend,
        "languages": sorted(lang.lower() for lang in config.languages) if config.filter_languages else [],
        "include_unlabeled": config.include_unlabeled,
        "mediatypes": sorted(config.mediatypes),
    }
    digest = hashlib.sha256(json.dumps(described, sort_keys=True).encode()).digest()
    # 7 bytes fit SQLite's signed integer; never 0, which means "no scope yet".
    return int.from_bytes(digest[:7], "big") or 1


def serve_forever(config: config_module.Config) -> int:
    if config.log_queries:
        logging.getLogger("ditta_ia_adapter.front.node").setLevel(logging.DEBUG)
    app = build_app(config)
    app.start_background()
    logging.info("node %s listening on %s:%s", app.node.key.pubkey, config.host, config.port)
    try:
        app.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
