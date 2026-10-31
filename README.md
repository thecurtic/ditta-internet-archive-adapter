# ditta-ia-adapter

An unofficial, third-party adapter that makes the [Internet Archive](https://archive.org) searchable as a [DITTA](https://github.com/ditta-protocol/ditta-spec) indexer. It is not part of the ditta-protocol organisation, not a reference implementation, and not endorsed by the Internet Archive. It is free to query.

![Adapter architecture: three layers, with every Internet Archive rule in the bottom layer](docs/architecture.svg)

## What DITTA is

DITTA is a protocol for federated web search. An **indexer** keeps a corpus and answers queries against it; a **broker** takes a query from a client, decides which indexers to ask, merges what they return, and hands results back. Every node has a secp256k1 key, publishes a small signed **announcement** saying where it can be reached and what its corpus looks like, and signs the messages it sends. There is no central registry: nodes learn about each other from announcements. The specification, [`protocol.md`](https://github.com/ditta-protocol/ditta-spec/blob/main/protocol.md), is the authority on every wire-format question.

## What this adapter does

A broker POSTs a query string to `/query`. The adapter searches the text inside the Internet Archive's books and documents, picks the best matches, maps each to a DITTA result (identifier, URL, title, snippet), and returns a signed result set, best match first. The snippet is the passage where the query's words were found. It never returns more results than the query's `max_results`, and never more than 50 (configurable).

It is thin by design. It holds no copy of the Archive, only a short-lived response cache, a table of term counts, a few rank counters, and the announcements other nodes have sent it.

| Path | What it does |
| --- | --- |
| `/query` | Searches the Archive. Only `per_query` at price 0 is offered; other pricing types get `unsupported_pricing_type`. |
| `/corpus` | Per-term statistics (format 0) from the term store. Requests must be signed. |
| `/announce` | Accepts other nodes' announcements, keeps the newest per key, and relays new ones. |
| `/peers` | Returns this node's announcement plus the ones it holds. Requests must be signed. |

Every message the adapter sends is signed: result sets, errors, statistics, peer lists and announcements.

## How queries are read

The query string is untrusted input and is never passed to the Archive as is. It is read as a list of terms:

1. Split on whitespace. An empty query is answered with an error (`rejected`), not a search. So is one with more than 32 terms.
2. Escape every Lucene special character in each term (`+ - & | ! ( ) { } [ ] ^ " ~ * ? : \ /`), and lowercase the bare operator words `AND`, `OR` and `NOT`. A caller can never send a field query, wildcard, range or boolean expression.
3. Gather a pool of up to 100 candidate items (`candidate_pool`), strictest match first. Each step runs only while the pool is short, fetches one page of up to 100 hits, and adds the items it has not seen:
    1. the terms as a **loose phrase**, a few words apart at most (`"gore street vancouver"~3`);
    2. **every term**, anywhere in the item;
    3. **every term but one** (for 3 to 8 terms);
    4. **any term**.
4. Rerank the pool and return the best n.

For "gore street vancouver" the phrase step alone finds 13 items, from British Columbia government gazettes to restaurant guides, each with the passage naming "Gore Street, Vancouver". A search makes at most four requests, usually one, and repeats come from the cache.

A pass-through would let anyone send expensive queries to the Archive's servers under this adapter's name. The protocol leaves interpretation to the indexer, so this translation is the adapter's main job. It does not try to spot names, dates or fields in a query; the loose phrase is its only reading of a query's structure (see Limits).

### Reranking

The Archive matches against an item's whole text, but returns only a few highlighted passages per hit. The reranker (`src/ditta_ia_adapter/rerank.py`, which knows nothing about the Archive) orders the pool by three keys in turn:

1. **Coverage:** the share of the query's words found in the title or passages, never less than the ladder step guarantees (a phrase or every-term hit holds every word, even if its excerpts don't show them all). An item missing a word never outranks one holding them all.
2. **Closeness:** the query's words next to each other and in order, weighted 3, plus the words in the title, weighted 2.
3. **The Archive's own order**, to break ties.

The snippet is the passage holding the most query words, then the longest run of them. Matching is exact after lowercasing, with no stemming and no stop words: "lighthouses" does not match "lighthouse".

### Filters and mapping

Searches are limited to the announced languages (`en` by default) and to texts: books, scanned pages and other documents, so video and podcasts stay out. Items labelled English *and* items with no language label are kept; only items labelled in another language are left out. Set `include_unlabeled = false` to require the label, `filter_languages = false` to search every language, or change `mediatypes` under `[search]` (`[]` searches every media type). The same filters apply to term counts and the corpus size, so the statistics describe exactly what is served. Only English has been checked against the live full-text index; other languages use the index's own names (`French`, `German` and so on).

| DITTA field | Source |
| --- | --- |
| Document identifier | `identifier` |
| URL | `https://archive.org/details/{identifier}`, always built, never taken from metadata |
| Title | `title`, first value if a list, the identifier if missing |
| Snippet | The best matching passage, cut to 300 characters; without one, `description`, then a line such as `Diedreck, Jay · 2017 · texts` from creator, year, media type and subjects |

A document is an Archive item: an item whose text spans several files (a two-volume scan, say) appears once, with the passages from all its files. An item is returned when it has an identifier and some text for a snippet. Every string from the Archive is treated as untrusted display text: markup is stripped, never evaluated.

### The metadata fallback

`[archive] backend = "metadata"` switches to the Archive's metadata search (`advancedsearch.php`), which matches titles, descriptions and subjects only. It was the only backend before 0.4.0 and is kept, unchanged, for when full-text search is unavailable. It walks every term, then every term but one, then any term, keeps the Archive's order, and takes the snippet from the description. Counts and the corpus size then come from the metadata index too.

## How it treats the Archive

The Archive's [automated access guidance](https://archive.org/developers/bots.html) is treated as hard requirements, all enforced in one module (`src/ditta_ia_adapter/ia/`):

- **One library.** Every request goes through the official [`internetarchive`](https://pypi.org/project/internetarchive/) library's session, which also authenticates when the operator has Archive keys (`IA_ACCESS_KEY_ID` and `IA_SECRET_ACCESS_KEY`).
- **User-Agent.** Every request names the tool, its version and the operator's contact URL, for example `ditta-ia-adapter/0.4.0 (+https://example.com)`, after the library's own User-Agent. The running adapter is not an AI agent, so it names no model. When an AI coding agent runs the live tests, it names its model too, as the Archive requires (see Development).
- **Rate limits.** A `429` or a `5xx` starts a backoff, honouring `Retry-After` when present and otherwise doubling from 2 seconds up to 10 minutes.
- **Throttled is not empty.** While backing off, the adapter sends nothing to the Archive and answers queries with `rate_limited` (HTTP 503 with `Retry-After`). It never answers with an empty result set because it was throttled, since a broker would read that as "no matches".
- **Concurrency and spacing.** At most 2 requests to the Archive at once, and each starts at least 1 second after the previous one, across live queries and the background counter.
- **Caching.** Identical searches within 10 minutes are served from memory.
- **Few requests per search.** At most four, one page each, and usually one.
- **Caller details dropped.** The full-text service echoes the caller's IP address and User-Agent (`session_context`) and the signed-in account (`forensics`) in its responses. The adapter removes both as soon as a response arrives, so they never reach a log, a cache or an error message.

Full-text searches go to `GET https://archive.org/services/search/beta/page_production/` with `service_backend=fts`: the search service behind archive.org's own search page, over the same index the library's full-text search reaches through another host. This one can also sort by date, which counts need. Its filters use the index's `meta_*` field names (`meta_mediatype`, `meta_languageSorter`). The endpoint is undocumented, so the metadata backend stays as a fallback.

The adapter calls the library's session directly rather than its `Search` helper. The helper parses the response body without looking at the HTTP status, so a `429` and its `Retry-After` would be invisible, and it retries on its own behind our backoff. With the library's retries off, the adapter's backoff is the only retry policy.

## Corpus statistics

Term counts are built from real demand, so the reported terms reflect what people search rather than the Archive's metadata boilerplate.

- **Seeding.** The operator lists `seed_terms` in the config, so the first statistics are not empty.
- **Backfill.** Each served query adds its terms, minus common English stopwords, to the store. A background worker counts queued terms one at a time, a few seconds apart.
- **One request per term.** A term's statistics come from one search for it with one hit, newest first: the hit total is its document count, and the top hit's `publicdate` is its freshness marker (`newest`).
- **Text files, not items.** The full-text index counts text files, and an item can hold several, so counts and the corpus size run slightly above item counts.
- **One index at a time.** The term store remembers which backend and filters its counts were made under. Change either and, at the next start, every count and the corpus size are dropped and recounted (the terms stay queued), so counts from two indexes are never served together. The first start of 0.4.0 does this once, since older stores don't record it. Recounting takes one request per stored term at the normal pace: up to about 7 hours for 5000 terms, with the corpus size first.
- **Staleness.** Counts are refreshed after 7 days, oldest first, and terms nobody has requested for 90 days are dropped.
- **Snapshot time.** The `updated` timestamp is the end of the last completed refresh pass.

Terms are lowercased, because the Archive's search ignores case. `/corpus` returns up to 5000 terms, best covered first.

**What the term store never records.** It holds bare terms, their counts and freshness, when each was counted, and the *day* each was last requested. It never stores whole query strings, requester pubkeys, IP addresses, or the time of any query, so it cannot say who searched for what, or when. Announcements from other nodes and rank stats live in separate databases that are never joined to it. Query strings are not logged unless the operator turns on `log_queries`, which is off in the shipped config. The HTTP access log carries the request line and status only, with no bodies and no client addresses.

## Rank stats

The pool of 100 candidates is a guess. To check it, the full-text backend counts where served results sat in the Archive's own order, in its own `stats.sqlite`: per day, pool size and whether the pool was full, how many searches ran, and how many results were served at each position from each ladder step and each band of 10 Archive ranks. Cached answers are not counted, and rows older than `keep_days` (90) are deleted.

```sh
ditta-ia-adapter rank-report --config config.toml --days 30
```

prints a histogram of served position against Archive rank, how deep the top 10 came from ("90% came from the source's top 40"), the share from each ladder step, and the **tail**: how often a top-10 result came from the last fifth of a full pool. If the tail share is high, the reranker keeps reaching for items near the edge of the pool, and `candidate_pool` should grow.

**What the rank stats never record.** Counters only: never queries, terms, the time of any query, or anything about requesters. The store shares no file with the term store, and the adapter refuses to start if they are configured to. The ladder step does hint at query length (a phrase step means two or more words), so on a quiet day the counters show roughly how many searches ran and how strict their matches were, but nothing about what was searched or by whom.

## Discovery

The adapter keeps the newest valid announcement per pubkey, relays each new one unchanged to the other nodes it knows, and announces itself to every node it has seen. It starts from the `seed_endpoints` in the config and re-announces every 6 hours, or sooner when its corpus summary changes.

Announced endpoints are chosen by whoever signed them, so the adapter refuses to connect to loopback, private, link-local and other non-public addresses, follows no redirects, requires HTTPS, and posts at most one announcement per second from a bounded queue. Onion endpoints are skipped, since the adapter does not run a Tor proxy.

## Announcement

The adapter announces the `indexer` role with a single `per_query` pricing option at price 0, and never sends a 402 challenge. Free access avoids charging for the Archive's data and keeps the adapter a clean demonstration.

Its corpus summary reports:

- the corpus size as the Archive's full-text index publishes it, under the same media-type and language filters (about 36.7 million English or unlabelled text files on 2026-10-31, of 49.8 million in the whole index), refreshed daily;
- the number of counted terms in its store, a lower bound rather than the Archive's real vocabulary, which it cannot know;
- the snapshot time, the configured languages (default `["en"]`), and statistics format `0`.

## Running it

Requirements: Python 3.12 or later, and a TLS reverse proxy (nginx, Caddy or similar) in front of it, since DITTA traffic must not travel in plaintext over links the operator does not control.

```sh
python -m venv .venv && . .venv/bin/activate
pip install .
cp config.example.toml config.toml   # then set node.endpoint and archive.contact_url
ditta-ia-adapter keygen node.key     # prints the node's pubkey; keep the file private
ditta-ia-adapter serve --config config.toml
```

The server listens on `127.0.0.1:8080` by default. `ditta-ia-adapter rank-report --config config.toml` prints the rank stats (see above).

### Running as a service

`deploy/ditta-ia-adapter.service` runs the adapter as user `ditta` from `/srv/ditta-internet-archive-adapter` and restarts it if it crashes. It never updates itself: deploying is one command, run when you choose.

```sh
sudo /srv/ditta-internet-archive-adapter/deploy/update.sh
```

`update.sh` fetches `main`, and if it differs from the commit last installed (recorded in `.venv/installed-commit`, so a manual `git pull` doesn't fool it), tests the new commit from a clean export, installs it and restarts the service. If the tests fail, nothing changes: the service keeps running the last good commit, the failure is printed, and that commit is skipped on later runs until a newer one is pushed. Git, pip and the tests run as the `ditta` user; root is used only for the restart.

One-time setup, as root (the paths are the unit's defaults; edit it if you change them):

```sh
useradd --system --home-dir /srv/ditta-internet-archive-adapter --shell /usr/sbin/nologin ditta
git clone <this repository's URL> /srv/ditta-internet-archive-adapter
chown -R ditta:ditta /srv/ditta-internet-archive-adapter
cd /srv/ditta-internet-archive-adapter
runuser -u ditta -- python3 -m venv .venv
runuser -u ditta -- cp config.example.toml config.toml      # then edit it
runuser -u ditta -- deploy/update.sh --no-restart           # installs and tests
runuser -u ditta -- .venv/bin/ditta-ia-adapter keygen node.key
cp deploy/ditta-ia-adapter.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ditta-ia-adapter.service
```

Put Internet Archive keys, if you have them, in `/etc/ditta-ia-adapter.env` (mode 0600) as `IA_ACCESS_KEY_ID=...` and `IA_SECRET_ACCESS_KEY=...`. Watch the service with `journalctl -u ditta-ia-adapter`.

## Development

```sh
pip install -e '.[test]'
pytest
```

The default test run never contacts the Internet Archive: the Archive client is tested against real responses recorded in `tests/fixtures/`. The wire format is tested against the spec's own test vectors (`tests/vectors/`, copied at a pinned spec commit), all 31 valid and 59 invalid.

Live tests are opt-in. They make four requests when the Archive answers as expected (a few more if a search walks further down its ladder), one at a time and at least 5 seconds apart, through one throttle:

```sh
DITTA_IA_LIVE=1 DITTA_IA_CONTACT_URL=https://example.com pytest tests/test_live.py
```

An AI coding agent running them must also set `DITTA_IA_AGENT` to its model identifier (for example `DITTA_IA_AGENT=claude-opus-5-5`), which the Archive's [rules](https://archive.org/developers/bots.html) require of AI agents. It is added to the User-Agent of those requests only.

### Layout

| Path | Layer |
| --- | --- |
| `src/ditta_ia_adapter/front/` | DITTA front: HTTP server, request handling, announcements, discovery |
| `src/ditta_ia_adapter/wire/` | Wire format: strict parsing, validation, canonical serialization, signatures |
| `src/ditta_ia_adapter/adapter.py` | The adapter interface between the front and a source |
| `src/ditta_ia_adapter/ia/` | Internet Archive client: `fulltext.py` and `metadata.py` (the two backends), `http.py` (every request, the throttle and backoff), `query.py`, `mapping.py`, `throttle.py` |
| `src/ditta_ia_adapter/rerank.py` | The reranker, independent of any source |
| `src/ditta_ia_adapter/terms/` | Term store (SQLite) and its background count worker |
| `src/ditta_ia_adapter/rankstats/` | Rank stats store (SQLite) and `rank-report` |

The wire format is implemented here rather than taken from `ditta-wire-py`, which is a readable reference written for vectors and tests, not for serving. Signatures use a native secp256k1 library.

### Dependencies

- [`internetarchive`](https://pypi.org/project/internetarchive/): the Archive's official client. Its access rules require every request to go through it.
- [`coincurve`](https://pypi.org/project/coincurve/): BIP-340 Schnorr signatures through the native libsecp256k1, fast enough to sign every response.
- [`pytest`](https://pypi.org/project/pytest/) (tests only).

Everything else is the standard library, including the HTTP server and SQLite.

## Limits

What the adapter leaves out, on purpose:

- **Query interpretation.** It does not spot names, dates, phrases or fields in a query; the loose phrase step is its only reading of a query's structure. The rank stats will show whether more is needed.
- **Stemming and stop words** in the reranker. Exact matching is simple, predictable and not tied to one language.
- **Whole-text reranking.** The reranker sees only the passages the Archive highlights, a few per hit, though the Archive's own match covers the whole text.
- **Unique item counts.** Counts and the corpus size are text-file totals, a small overcount.
- **Wayback Machine.** A different corpus with no full-text search, better served by a separate adapter that reuses this front.
- **Changes API.** Not needed for freshness, since the corpus timestamp comes from the statistics refresh.
- **Views API.** A possible ranking input later; the reranker uses only the query, the passages and the Archive's order.
- **Paid queries.** No Lightning, L402 or 402 challenges; the adapter is free.
- **Onion endpoints** are not contacted.

## License

MIT. See [LICENSE](LICENSE).
