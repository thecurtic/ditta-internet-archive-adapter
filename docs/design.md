# DITTA Internet Archive Adapter — Design

Oct 28, 2026

## Purpose and scope

The adapter is a free DITTA indexer whose corpus is the Internet Archive's item catalogue. It takes a DITTA query string, translates it into an Internet Archive search, and returns DITTA results: identifier, URL, title, snippet.

It is thin by design. It holds no copy of the corpus, only a response cache and a table of term counts. Everything the Internet Archive asks of automated clients is enforced in one place, so the adapter can run politely from outside the Archive today and serve as a working model for an indexer the Archive runs itself.

## Architecture

The adapter has three layers, and only the bottom one knows anything about the Internet Archive.

![Adapter architecture: three layers, with every Internet Archive rule in the bottom layer](architecture.svg)

The front is the same for every DITTA indexer. Another source, such as a Wayback adapter, implements the same two calls in the middle layer and reuses the front unchanged.

## Query interpretation

The query string is treated as a bag of terms: all terms must match, and if that returns nothing, any term may match. The protocol leaves interpretation to the indexer, so this translation is the adapter's main job.

1. Split the string on whitespace.
2. Escape every Lucene special character in each term, so the caller can never inject field queries, wildcards or ranges.
3. Join the terms with AND and run the search.
4. If that returns zero results, rerun once with OR.
5. An empty query returns an error rather than a search.

The raw string is never passed through. The spec treats it as attacker-controlled input at the indexer, and here a pass-through would also let callers send expensive queries to the Archive's servers under the adapter's name.

## Result mapping

Each query is one search request that asks only for the fields below, so no per-result metadata lookups are ever made.

| DITTA field | Internet Archive source | Shaping |
| --- | --- | --- |
| Document identifier | `identifier` | As is; stable and unique within the Archive |
| URL | `https://archive.org/details/{identifier}` | Built, never taken from metadata |
| Title | `title` | First value if an array; identifier if missing |
| Snippet | `description` | First value if an array, HTML tags stripped, whitespace collapsed, truncated |

Results keep the Archive's order, best first, since a DITTA result set carries no score. The search must use the paged form with `page` and `rows`: the library's default returns every match sorted by identifier, which breaks the best-first rule. Relevance ordering of the paged form should be confirmed in testing.

The result count never exceeds the query's max result count.

## Internet Archive access rules

Every request goes through the official `internetarchive` Python library, which is the Archive's supported path to search and handles the User-Agent suffix and credentials. The Archive's [automated access guidance](https://archive.org/developers/bots.html) sets the rules below.

- **User-Agent:** a tool name and version on every request, such as `ditta-ia-adapter/0.1.0 (+https://example.com)`. The URL is the operator's contact page, set in configuration, so the Archive can see who runs it and how to reach them. No model name, since the adapter is not an AI agent.
- **Rate limits:** honour `429` responses and `Retry-After`, with exponential backoff on errors.
- **Caching:** identical searches within a short window are served from cache instead of re-sent.
- **Concurrency:** a small fixed cap on simultaneous requests.
- **Credentials:** authenticate when an account key is available.

While the client is backing off, the adapter returns an error to new queries instead of searching. It never answers a query with an empty result set because it was throttled, since a broker would read that as no matches.

## Corpus statistics

Term counts are built lazily from real demand, so the reported terms reflect what people search rather than the Archive's metadata boilerplate. A count is the `numFound` of a search for that term with zero rows requested.

- **Seeding:** the term table starts from the operator's own targeted searches, which breaks the cold start where no stats means no routed queries.
- **Backfill:** each served query adds its terms, minus stopwords, to a queue. A background worker counts queued terms one at a time with a few seconds between requests.
- **Staleness:** each term stores `count`, `counted_at` and `last_requested`. The oldest counts are refreshed first, and terms not requested for a long period age out.
- **Snapshot time:** the corpus-updated timestamp is the time of the last completed refresh pass.

The queue stores bare terms only. It never records whole query strings, requester pubkeys or per-query timestamps, so it cannot reconstruct who searched for what. Per-broker data, if ever needed for rate limiting, lives in a separate short-lived store that is never joined to the term table.

## Announcement and pricing

The adapter is free: it announces a single `per_query` pricing option at a price of 0 and never sends a 402 challenge. Free access avoids charging for the Archive's data and keeps the adapter a clean demonstration.

The announcement names the indexer role with a corpus description pointing to the Internet Archive, and serves corpus statistics from the term table.

## Repository and README

The adapter lives in a public repo on the maintainer's GitHub account, not under the ditta-protocol organisation, because it is a third-party implementation rather than an official part of DITTA. It starts empty, so an MIT LICENSE file and a Python .gitignore go in with the first commit; the .gitignore also excludes the SQLite term store and the response cache.

The README is the adapter's front page for two readers: the Internet Archive, checking what is hitting its servers, and developers who want to run it or build a similar adapter. It has to be thorough and plain, and cover:

- **What DITTA is:** a short explanation of the protocol, linking to the DITTA spec. The architecture diagram goes in near the top, as an SVG file in the repo.
- **What this adapter does:** queries in, Internet Archive results out, free, and unofficial.
- **How queries are read:** term escaping, AND with one OR fallback, and why the raw string is never passed through.
- **How it treats the Archive:** the User-Agent, rate limits, backoff, caching and the concurrency cap, with a link to the Archive's automated access guidance.
- **Corpus statistics:** how term counts are built from served queries, and what the term store never records.
- **Running it:** requirements, configuration, credentials and how to start it.
- **Limits:** what v1 leaves out, such as the Wayback Machine.

## Out of scope for v1, and open items

Three Internet Archive services are deliberately left out of v1:

- **Changes API:** not needed for freshness, since the corpus timestamp comes from the stats refresh.
- **Views API:** possible ranking input later; the Archive's relevance order is used as is.
- **Wayback Machine:** a different corpus with no full-text search, better served by a separate adapter.

Open items:

- [ ] Whether to filter by `mediatype`, for example to texts only, which gives better snippets.
- [ ] Snippet length limit.
- [ ] Whether result sets are signed.
- [ ] Cache window and the cap on concurrent requests.
- [ ] Confirm that the paged search returns results in relevance order.
- [x] Choose the license: MIT.
