# ditta-ia-adapter

An unofficial, third-party adapter that makes the [Internet Archive](https://archive.org) searchable as a [DITTA](https://github.com/ditta-protocol/ditta-spec) indexer. It is not part of the ditta-protocol organisation, not a reference implementation, and not endorsed by the Internet Archive. It is free to query.

![Adapter architecture: three layers, with every Internet Archive rule in the bottom layer](docs/architecture.svg)

## What DITTA is

DITTA is a protocol for federated web search. An **indexer** keeps a corpus and answers queries against it; a **broker** takes a query from a client, decides which indexers to ask, merges what they return, and hands results back. Every node has a secp256k1 key, publishes a small signed **announcement** saying where it can be reached and what its corpus looks like, and signs the messages it sends. There is no central registry: nodes learn about each other from announcements. The specification, [`protocol.md`](https://github.com/ditta-protocol/ditta-spec/blob/main/protocol.md), is the authority on every wire-format question.

## What this adapter does

A broker POSTs a query string to `/query`. The adapter turns it into one Internet Archive search, maps each hit to a DITTA result (identifier, URL, title, snippet), and returns a signed result set, best match first. It never returns more results than the query's `max_results`, and never more than 50 (configurable).

It is thin by design. It holds no copy of the Archive, only a short-lived response cache, a table of term counts, and the announcements other nodes have sent it.

| Path | What it does |
| --- | --- |
| `/query` | Searches the Archive. Only `per_query` at price 0 is offered; other pricing types get `unsupported_pricing_type`. |
| `/corpus` | Per-term statistics (format 0) from the term store. Requests must be signed. |
| `/announce` | Accepts other nodes' announcements, keeps the newest per key, and relays new ones. |
| `/peers` | Returns this node's announcement plus the ones it holds. Requests must be signed. |

Every message the adapter sends is signed: result sets, errors, statistics, peer lists and announcements.

## How queries are read

The query string is untrusted input and is never passed to the Archive as is. It is read as a bag of terms:

1. Split on whitespace. An empty query is answered with an error (`rejected`), not a search. So is one with more than 32 terms.
2. Escape every Lucene special character in each term (`+ - & | ! ( ) { } [ ] ^ " ~ * ? : \ /`), and lowercase the bare operator words `AND`, `OR` and `NOT`. A caller can never send a field query, wildcard, range or boolean expression.
3. Search for items matching **every term**.
4. If that found fewer results than asked for, search for items matching **every term but one** (for 3 to 8 terms).
5. If still short, search for items matching **any term**.

Each step runs only while places are left, and its new results go after the earlier ones, so a stricter match is never pushed down by a looser one. For "Gore street angle vancouver", all four words appear together in a single item, a radio transcript; the all-but-one step then adds Vancouver council minutes and a report on the Stratford Hotel fire, at Gore and Keefer. Any-term on its own is dominated by common words ("street" alone matches thousands of street-photo uploads), which is why it comes last. A query makes at most three requests, and repeats come from the cache.

A pass-through would let anyone send expensive queries to the Archive's servers under this adapter's name. The protocol leaves interpretation to the indexer, so this translation is the adapter's main job.

Searches are limited to the announced languages (`en` by default). The Archive's `language` field is free text (`eng`, `English`, `en` all appear) and about a third of items have none, mostly audio and video. So by default the adapter keeps items labelled English *and* items with no label, and leaves out only items labelled in another language. For "lighthouse keeper" that keeps 475 of 485 hits, where requiring the label would keep 315. Set `include_unlabeled = false` to require the label, or `filter_languages = false` to search everything. The same filter applies to term counts and the corpus size, so the statistics describe exactly what is served.

Results keep the Archive's relevance order (confirmed against the live Archive). Each search is one request in the paged form (`page` and `rows`), asking only for the fields the mapping below uses, so no per-result metadata lookups are ever made.

| DITTA field | Source |
| --- | --- |
| Document identifier | `identifier` |
| URL | `https://archive.org/details/{identifier}`, always built, never taken from metadata |
| Title | `title`, first value if a list, the identifier if missing |
| Snippet | `description`, first value if a list, HTML stripped, whitespace collapsed, cut to 300 characters; without one, a line such as `Diedreck, Jay · 2017 · texts` from creator, year, media type and subjects |

Only texts are searched by default: books, scanned pages and other documents (`mediatype:texts`), so video and podcasts stay out of the results. The `mediatypes` setting under `[search]` changes this, and `[]` searches every media type. The same filter applies to term counts and the corpus size. An item is returned when it has an identifier (so a URL) and some text for a snippet; the rare item with neither a description nor any of those fields is skipped. Every string from the Archive is treated as untrusted display text: markup is stripped, never evaluated.

## How it treats the Archive

The Archive's [automated access guidance](https://archive.org/developers/bots.html) is treated as hard requirements, all enforced in one module (`src/ditta_ia_adapter/ia/`):

- **One library.** Every request goes through the official [`internetarchive`](https://pypi.org/project/internetarchive/) library's session, which also authenticates when the operator has Archive keys (`IA_ACCESS_KEY_ID` and `IA_SECRET_ACCESS_KEY`).
- **User-Agent.** Every request names the tool, its version and the operator's contact URL, for example `ditta-ia-adapter/0.2.0 (+https://example.com)`, after the library's own User-Agent. The running adapter is not an AI agent, so it names no model. When an AI coding agent runs the live tests, it names its model too, as the Archive requires (see Development).
- **Rate limits.** A `429` or a `5xx` starts a backoff, honouring `Retry-After` when present and otherwise doubling from 2 seconds up to 10 minutes.
- **Throttled is not empty.** While backing off, the adapter sends nothing to the Archive and answers queries with `rate_limited` (HTTP 503 with `Retry-After`). It never answers with an empty result set because it was throttled, since a broker would read that as "no matches".
- **Concurrency and spacing.** At most 2 requests to the Archive at once, and each starts at least 1 second after the previous one, across live queries and the background counter.
- **Caching.** Identical searches within 10 minutes are served from memory.

The adapter calls the library's session directly rather than its `Search` helper. The helper parses the response body without looking at the HTTP status, so a `429` and its `Retry-After` would be invisible, and it retries on its own behind our backoff. With the library's retries off, the adapter's backoff is the only retry policy.

## Corpus statistics

Term counts are built from real demand, so the reported terms reflect what people search rather than the Archive's metadata boilerplate.

- **Seeding.** The operator lists `seed_terms` in the config, so the first statistics are not empty.
- **Backfill.** Each served query adds its terms, minus common English stopwords, to the store. A background worker counts queued terms one at a time, a few seconds apart.
- **One request per term.** A term's statistics come from one search for it with one row, newest first: `numFound` is its document count, and the top item's `publicdate` is its freshness marker (`newest`).
- **Staleness.** Counts are refreshed after 7 days, oldest first, and terms nobody has requested for 90 days are dropped.
- **Snapshot time.** The `updated` timestamp is the end of the last completed refresh pass.

Terms are lowercased, because the Archive's search ignores case. `/corpus` returns up to 5000 terms, best covered first.

**What the term store never records.** It holds bare terms, their counts and freshness, when each was counted, and the *day* each was last requested. It never stores whole query strings, requester pubkeys, IP addresses, or the time of any query, so it cannot say who searched for what, or when. Announcements from other nodes live in a separate database that is never joined to it. Query strings are not logged unless the operator turns on `log_queries`, which is off in the shipped config. The HTTP access log carries the request line and status only, with no bodies and no client addresses.

## Discovery

The adapter keeps the newest valid announcement per pubkey, relays each new one unchanged to the other nodes it knows, and announces itself to every node it has seen. It starts from the `seed_endpoints` in the config and re-announces every 6 hours, or sooner when its corpus summary changes.

Announced endpoints are chosen by whoever signed them, so the adapter refuses to connect to loopback, private, link-local and other non-public addresses, follows no redirects, requires HTTPS, and posts at most one announcement per second from a bounded queue. Onion endpoints are skipped, since the adapter does not run a Tor proxy.

## Announcement

The adapter announces the `indexer` role with a single `per_query` pricing option at price 0, and never sends a 402 challenge. Free access avoids charging for the Archive's data and keeps the adapter a clean demonstration.

Its corpus summary reports:

- the Archive's item count as its search API publishes it, under the same media-type and language filters (`numFound`; about 43 million English or unlabelled texts of 128 million items), refreshed daily;
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

The server listens on `127.0.0.1:8080` by default.

### Running as a service

`deploy/ditta-ia-adapter.service` runs the adapter as user `ditta` from `/srv/ditta-internet-archive-adapter` and restarts it if it crashes. It never updates itself: deploying is one command, run when you choose.

```sh
sudo /srv/ditta-internet-archive-adapter/deploy/update.sh
```

`update.sh` fetches `main`, and if it moved, tests the new commit from a clean export, installs it and restarts the service. If the tests fail, nothing changes: the service keeps running the last good commit, the failure is printed, and that commit is skipped on later runs until a newer one is pushed. Git, pip and the tests run as the `ditta` user; root is used only for the restart.

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

Live tests make a few requests through the production throttle and are opt-in:

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
| `src/ditta_ia_adapter/ia/` | Internet Archive client: query translation, mapping, throttle, cache |
| `src/ditta_ia_adapter/terms/` | Term store (SQLite) and its background count worker |

The wire format is implemented here rather than taken from `ditta-wire-py`, which is a readable reference written for vectors and tests, not for serving. Signatures use a native secp256k1 library.

### Dependencies

- [`internetarchive`](https://pypi.org/project/internetarchive/): the Archive's official client. Its access rules require every request to go through it.
- [`coincurve`](https://pypi.org/project/coincurve/): BIP-340 Schnorr signatures through the native libsecp256k1, fast enough to sign every response.
- [`pytest`](https://pypi.org/project/pytest/) (tests only).

Everything else is the standard library, including the HTTP server and SQLite.

## Limits

What v1 leaves out, on purpose:

- **Wayback Machine.** A different corpus with no full-text search, better served by a separate adapter that reuses this front.
- **Changes API.** Not needed for freshness, since the corpus timestamp comes from the statistics refresh.
- **Views API.** A possible ranking input later; the Archive's own relevance order is used as is.
- **Paid queries.** No Lightning, L402 or 402 challenges; the adapter is free.
- **Onion endpoints** are not contacted.

## License

MIT. See [LICENSE](LICENSE).
