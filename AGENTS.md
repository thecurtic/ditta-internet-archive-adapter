# AGENTS.md

Rules for any coding agent working in this repository. Read this file in full before changing anything. If an instruction here conflicts with a request, stop and ask the maintainer rather than guessing.

## What this repo is

An unofficial, third-party adapter that makes the Internet Archive searchable as a DITTA indexer. A broker sends a query string; the adapter searches the Internet Archive and returns results as DITTA result sets (identifier, URL, title, snippet).

- DITTA is spelled with two T's. The protocol spec lives in the `ditta-protocol/ditta-spec` repo; `protocol.md` there is the authority on every wire-format question.
- This adapter is not part of the ditta-protocol organisation. Never describe it as official, as a reference implementation, or as endorsed by the Internet Archive.
- Describe DITTA as federated web search. Do not call it "pay-per-query"; this adapter is free.

## Ground rules

### Git

Agents may run git, including committing and pushing, when the maintainer asks for it. Commit messages describe the change in plain language. Never force-push, rewrite published history, or change remotes without the maintainer's explicit go-ahead.

### No personal details

Write every file as if the repo had been public from day one.

- Do not put the maintainer's name, handles, email, or other personal details in code, comments, docs, commit message suggestions, or examples. Say "the maintainer" or "the operator".
- Do not reference the maintainer's own infrastructure: hostnames, domains, IP addresses, tailnet names, VM names, home paths, wallets, Lightning nodes, or other services they run. Use `example.com`, `127.0.0.1`, and obviously fake values instead.
- Two exceptions: the copyright line in `LICENSE` names the maintainer, and the operator's contact URL in the User-Agent, which is set in configuration, never hard-coded. Examples and `config.example` use `https://example.com`.

### Secrets

Never commit or print credentials. Internet Archive keys, node private keys, and any tokens come from environment variables or a local config file that `.gitignore` excludes. Ship a `config.example` (or equivalent) with placeholder values only.

## Design rules

These decisions are settled. Changing any of them needs the maintainer's explicit sign-off.

### Architecture

Three layers, each in its own module, talking only through the interface below it.

1. **DITTA front:** HTTP endpoints, message validation, canonical serialization, signing, and the announcement. Nothing in this layer knows about the Internet Archive.
2. **Adapter interface:** three calls only: `search(query, n)` returning ordered results, `count(term)` returning the term's document count and newest document date (statistics format 0), and `total_documents()` returning the corpus size as the source itself publishes it (for the Archive, the hit total for `*:*` from the active backend). If a source publishes no size, estimate it, or count the unique documents the adapter has served.
3. **Internet Archive client:** query translation, rate limiting, backoff, caching, and all requests to the Archive. It has two backends behind the same interface: **full-text** (the default), which searches the text inside items, and **metadata** (`advancedsearch.php`), which searches titles, descriptions and subjects and is kept as a fallback. `[archive] backend` picks one. Searches, counts and the corpus size always come from the active backend, so the statistics describe the index actually served.

Beside the client:

- **Reranker** (`rerank.py`): reorders a pool of candidates using their titles, passages and source rank. It knows nothing about the Archive, so any source can use it.
- **Term store** (SQLite) and its background count worker.
- **Rank stats store** (`stats.sqlite`, its own module): counters that show whether the candidate pool is big enough. See Privacy.
- **Announcement store**, which the front uses for discovery.

A **document** is an Archive item, served at one URL. The full-text index counts text files, and one item can hold several, so full-text counts and corpus size are text-file totals used as an approximation of item counts. Most items hold one text file, so the overcount is small. We accept it rather than spend requests counting unique items.

The wire format (strict parsing, validation, canonical serialization, signing) is implemented in this repo, in its own module under the front. `ditta-wire-py` is a reference to read, not a dependency: do not import or copy it. Signatures use `coincurve` (native libsecp256k1). The spec's test vectors are the conformance tests for this module, and every vector must pass.

### Query handling

- Treat the query string as untrusted input. Never pass it to the Archive as is.
- Split on whitespace and escape every Lucene special character in each term. An empty query returns an error.
- Limit every search, count and corpus-size request to the configured languages (default `en`), keeping unlabelled items unless configured otherwise. The filter comes from configuration only, never from the query.
- Always search in the paged form. The library's default returns every match sorted by identifier, which breaks DITTA's best-first ordering.
- Never return more results than the request's max result count.

**Full-text backend.** Search in up to four steps:

1. The loose phrase `"t1 t2 … tk"~3`.
2. All terms joined with AND.
3. Every term but one: an OR of the AND of each subset missing one term, for 3 to 8 terms only.
4. All terms joined with OR.

Run each step only while the candidate pool holds fewer than `search.candidate_pool` unique items (default 100). Each step fetches one page of up to that many hits and appends new items after the earlier ones. That makes the worst case four requests per search, with no further paging.

Hits are per text file. Hits from the same item are merged into one candidate that keeps its best Archive rank, its earliest ladder step and all its passages. The reranker orders the pool and the top n are returned.

**Metadata backend.** Unchanged from 0.3.0: three steps (AND, all-but-one, OR), each run only while fewer than the requested number of results have been found, with no reranking and no rank stats.

### Reranking

- The weights are fixed and named in code, not in configuration. Changing them is a code change with a test.
- Features:
  - the share of query terms found in the title and passages
  - query terms adjacent and in order
  - a title match
  - the source's rank, used only as a tie-break
- Matching is exact after lowercasing, with no stemming, so "lighthouses" does not match "lighthouse". This is a known limit.
- The reranker sees only the passages the Archive highlights, a few per hit, not the whole text. The Archive's own match covers the full text, so the reranker reorders whole-text matches using excerpts.

### Result mapping

| DITTA field | Source |
| --- | --- |
| Document identifier | `identifier` |
| URL | `https://archive.org/details/{identifier}`, always built, never taken from metadata |
| Title | `title`, first value if an array, identifier if missing |
| Snippet | Full-text: the best-scoring highlight passage, `{{{ }}}` match markers removed. Otherwise, and for the metadata backend: `description`, first value if an array, HTML stripped, whitespace collapsed, truncated. If there is none, a line from creator, year, media type and subjects |

Each item appears at most once in a result set.

Searches, counts and the corpus size are limited to the configured media types, `texts` by default (books, scanned pages, documents), so video and audio stay out. A result is returned when it has an identifier and some snippet text; otherwise it is skipped.

Treat every string from the Archive as untrusted display content: strip markup, never evaluate it.

### Being a good Internet Archive client

The Archive's automated access rules (<https://archive.org/developers/bots.html>) are hard requirements.

- Every request goes through the official `internetarchive` Python library.
- Full-text requests go to `GET https://archive.org/services/search/beta/page_production/` with:
  - `service_backend=fts`
  - `user_query` set to `!L <expression>`
  - `hits_per_page` and `page`
  - `aggregations=false`

  Filters use the `meta_*` field names (`meta_mediatype`, `meta_languageSorter`), which give the same totals as the library's own full-text endpoint. Plain names match something else. `count(term)` sorts with `publicdate:desc` and takes the newest date from the first hit.
- Full-text responses echo the caller's IP address and User-Agent in `session_context`, and the account in `forensics`. Drop both as soon as a response is parsed. Never log, store or put them in a fixture.
- Every request carries a User-Agent with the tool name, version, and the operator's contact URL from configuration. The deployed adapter is not an AI agent, so it names no model. But any request an AI coding agent makes to the Archive (live tests, probes, recording fixtures) must name that agent's model in the User-Agent, as the Archive's rules require: set `DITTA_IA_AGENT` for live tests, or pass `agent=` to `ArchiveSource`.
- Space requests: every request starts at least `min_request_interval_seconds` (default 1) after the previous one, across live queries and the count worker.
- Honour `429` and `Retry-After`; back off exponentially on errors; cap concurrent requests; cache identical searches for a short window.
- Authenticate when the operator has keys, read from the library's own variables, `IA_ACCESS_KEY_ID` and `IA_SECRET_ACCESS_KEY`.
- Live requests during development are few, sequential, and only when the maintainer has agreed to them.
- While backing off, answer new queries with a DITTA error (`rate_limited`). Never return an empty result set because the adapter was throttled.

### Signing

The adapter signs every message it sends: announcements, corpus statistics responses, result sets, peer list responses, and errors.

### Discovery

- The adapter keeps every valid announcement it receives (one per pubkey, greatest timestamp wins) and relays each new one unchanged to the other nodes it knows.
- It announces itself to every node it has seen, starting from a seed list in configuration, and re-announces periodically.
- `/peers` answers from the announcement store.
- Never connect to loopback, private, or link-local addresses unless configuration allows it, and bound the outbound connection rate.

### Pricing

The adapter is free. It announces a single `per_query` pricing option at price 0 and never sends a 402 challenge. Do not add Lightning, L402, or payment code.

### Privacy

- The term store holds bare terms, counts, and timestamps per term. Never store whole query strings, requester pubkeys, IP addresses, or per-query timestamps.
- If per-requester data is ever needed for rate limiting, it lives in a separate short-lived store that is never joined to the term store.
- Do not log query strings by default. Debug logging that includes queries must be opt-in and off in every shipped config.
- The rank stats store (`stats.sqlite`) holds counters only. Each counter is keyed by:
  - the served position
  - the source's rank band
  - the ladder step
  - a day bucket at most

  Never store queries, terms, per-query times or anything about requesters. Never join it with the term store. Cache hits are not counted. It tracks how often served results come from the tail of the pool, the sign that `search.candidate_pool` is too small. `ditta-ia-adapter rank-report --config …` prints the histogram and lines such as "95% of the top 10 came from the Archive's top 40".

### Intentional omissions

- **Query interpretation:** spotting phrases, names, dates or fields in a query. Left out for now: the ladder's loose phrase step is the only reading of the query's structure. Revisit when the rank stats show where it would help.
- **Stemming in the reranker:** exact matching keeps the reranker simple and predictable. See Reranking.
- **Unique item counts:** see the definition of a document under Architecture.

## Code standards

- **Readable first.** Write for a human reading the code for the first time, and for the next agent. Clear names, short functions, no clever tricks. A comment explains why, not what.
- **Modules are files you can delete and rewrite.** Keep each layer self-contained so someone can replace one without touching the others. Configuration is for operator settings, not for swapping behaviour. The one exception is `[archive] backend`: which Archive index to serve is the operator's call, and the metadata backend exists for when full-text is unavailable.
- **Precise.** Match the spec exactly. When the spec says MUST, the code enforces it and a test proves it.
- **Few dependencies.** Each new dependency needs a one-line reason in the README's dependencies section. Prefer the standard library.
- **Type hints** on all public functions.

## Testing

- The default test run must never contact the Internet Archive. Use recorded responses as fixtures.
- Live tests against the Archive are opt-in (behind an environment variable), make a small number of requests, and respect the same rate limits as production.
- Check message output against the DITTA spec's test vectors wherever they apply.
- Every bug fix gets a test that would have caught it.

## Documentation

- Keep the README current with every change that affects behaviour, configuration, or setup. It should explain what DITTA is (linking to the spec), what this adapter does, how it reads queries, how it treats the Archive, what the term store records and never records, and how to run it.
- When you decide not to decide something, write that down with the reason, so a gap is recorded as an intentional omission.
- Record design decisions and their reasons near the code they govern, or in the README's design section, not only in a summary message.

## Working with the maintainer

- Explain what you changed and why in plain language. Don't assume the diff will be read line by line.
- Ask before large refactors, new dependencies, or anything that touches the design rules above.
- If the spec is ambiguous, say so and propose a reading; do not quietly pick one.
