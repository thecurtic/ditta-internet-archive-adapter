# DITTA Internet Archive Adapter — Design

Oct 28, 2026; updated Oct 31, 2026 for 0.4.0

## Purpose and scope

The adapter is a free DITTA indexer whose corpus is the text of the Internet Archive's books and documents. It takes a DITTA query string, searches the Archive's full-text index, and returns DITTA results: identifier, URL, title, snippet.

It is thin by design. It holds no copy of the corpus, only a response cache, a table of term counts and a few rank counters. Everything the Internet Archive asks of automated clients is enforced in one place, so the adapter can run politely from outside the Archive today and serve as a working model for an indexer the Archive runs itself.

## Architecture

The adapter has three layers, and only the bottom one knows anything about the Internet Archive.

![Adapter architecture: three layers, with every Internet Archive rule in the bottom layer](architecture.svg)

The front is the same for every DITTA indexer. Another source, such as a Wayback adapter, implements the same three calls (search, count, total documents) in the middle layer and reuses the front unchanged.

The Archive client has two backends behind those calls, chosen by `[archive] backend`:

- **Full-text** (the default) searches the text inside items, through the search service behind archive.org's own search page.
- **Metadata** searches titles, descriptions and subjects through `advancedsearch.php`. It was the only backend until 0.4.0 and is kept, unchanged, as the fallback for when full-text search is unavailable.

Searches, counts and the corpus size always come from the active backend, so the statistics describe the index actually served. Beside the client sit the term store and its count worker, a reranker that knows nothing about the Archive, and a rank stats store.

## Query interpretation

The query string is untrusted input and is never passed through. The spec treats it as attacker-controlled input at the indexer, and here a pass-through would also let callers send expensive queries to the Archive's servers under the adapter's name.

1. Split the string on whitespace. An empty query returns an error rather than a search.
2. Escape every Lucene special character in each term, so the caller can never inject field queries, wildcards or ranges.
3. Fill a pool of up to 100 candidate items (`search.candidate_pool`) by walking a ladder, strictest first. Each step runs only while the pool is short, fetches one page of up to 100 hits, and appends the items it has not seen:
    1. the terms as a loose phrase, `"t1 t2 t3"~3`;
    2. all terms (AND);
    3. every term but one (an OR of the AND of each subset missing one term; 3 to 8 terms only);
    4. any term (OR).
4. Rerank the pool and return the top n.

A search makes at most four requests, and in practice usually one: for common phrases the first step fills the pool. Hits are per text file, so one item can appear several times; its hits merge into one candidate that keeps its best position and all its matching passages.

Every request is also limited to the announced languages (`en` by default) and to texts (books, scanned pages and documents), both configurable. Items labelled English and items with no language label are kept; only items labelled in another language are left out. Counts and the corpus size use the same filters.

**Not interpreted, on purpose.** The adapter does not look for names, dates, phrases or fields inside a query. The loose phrase step is its only reading of a query's structure. The rank stats will show whether more is needed.

## Reranking

A small reranker, independent of any source, decides which items of the pool to serve, so that items holding every query word, and pages where the words stand together, come first. It orders the pool by three keys in turn:

1. **Coverage:** the share of the query's words found in the title or the matching passages. It never counts lower than what the ladder step guarantees, since passages are excerpts: phrase and AND hits hold every word, all-but-one hits all but one, OR hits at least one.
2. **Closeness:** adjacency × 3 + title match × 2. Adjacency is the longest run of query words next to each other and in order, in the title or one passage (1 for the whole query). Title match is the share of the query's words in the title.
3. **The Archive's own order**, as a tie-break only.

Coverage is a key of its own rather than a weight, so an item missing a word never outranks one holding them all, however long the query. The weights are fixed in code, so changing them is a reviewed change with a test, not a setting.

Matching is exact after lowercasing: no stemming ("lighthouses" does not match "lighthouse") and no stop words ("of" counts like any word). Both are known limits, kept for simplicity and because a stemmer or stop-word list would be tied to one language.

## Result mapping

| DITTA field | Internet Archive source | Shaping |
| --- | --- | --- |
| Document identifier | `identifier` | As is; stable and unique within the Archive |
| URL | `https://archive.org/details/{identifier}` | Built, never taken from metadata; one URL per item |
| Title | `title` | First value if an array; identifier if missing |
| Snippet | The best matching passage | The passage holding the most query words, then the longest run of them; match markers and HTML stripped, whitespace collapsed, cut to 300 characters. Without a passage: `description`, then a line from creator, year, media type and subjects |

A document is an Archive item, so each item appears at most once in a result set, and the result count never exceeds the query's max result count. An item is returned when it has an identifier and some text for a snippet.

## Internet Archive access rules

Every request goes through the official `internetarchive` Python library's session, which handles the User-Agent suffix and credentials. The Archive's [automated access guidance](https://archive.org/developers/bots.html) sets the rules below.

- **User-Agent:** a tool name and version on every request, such as `ditta-ia-adapter/0.4.0 (+https://example.com)`. The URL is the operator's contact page, set in configuration and never hard-coded, so the Archive can see who runs it and how to reach them. No model name, since the adapter is not an AI agent.
- **AI agents:** any request an AI coding agent makes during development (live tests, probes, recording fixtures) names its model in the User-Agent, as the Archive's rules require.
- **Rate limits:** honour `429` responses and `Retry-After`, with exponential backoff on errors.
- **Caching:** identical searches within a short window are served from cache instead of re-sent.
- **Concurrency:** a small fixed cap on simultaneous requests (2), and a minimum gap between request starts (1 second), shared by searches and the count worker.
- **Credentials:** authenticate when an account key is available, read from the library's own `IA_ACCESS_KEY_ID` and `IA_SECRET_ACCESS_KEY`.

While the client is backing off, the adapter returns an error to new queries instead of searching. It never answers a query with an empty result set because it was throttled, since a broker would read that as no matches.

**The full-text endpoint.** Requests are `GET https://archive.org/services/search/beta/page_production/` with `service_backend=fts`, `user_query=!L <expression>` (`!L` marks Lucene syntax), `hits_per_page`, `page` and `aggregations=false`. It is the same index the library's own full-text search reaches through another host, and it can sort, which counts need. Its filters need the index's `meta_*` field names (`meta_mediatype`, `meta_languageSorter`); plain names match something else. The endpoint is undocumented, which is why the metadata backend stays as a fallback.

**Caller details are dropped.** Its responses echo the caller's IP address and User-Agent (`session_context`) and the signed-in account (`forensics`). The adapter removes both the moment a response arrives, so they never reach a log, a cache entry, an error message or a test fixture.

## Corpus statistics

Term counts are built lazily from real demand, so the reported terms reflect what people search rather than the Archive's boilerplate. A term's statistics come from one search for that term with one hit, newest first: the hit total is its document count and the top hit's `publicdate` is its freshness marker (`newest` in statistics format 0).

- **Seeding:** the term table starts from the operator's own targeted searches, which breaks the cold start where no stats means no routed queries.
- **Backfill:** each served query adds its terms, minus stopwords, to a queue. A background worker counts queued terms one at a time with a few seconds between requests.
- **Staleness:** each term stores `count`, `newest`, `counted_at` and `last_requested`. The oldest counts are refreshed first, and terms not requested for a long period age out.
- **Snapshot time:** the corpus-updated timestamp is the time of the last completed refresh pass.
- **One index at a time:** the term store keeps a fingerprint of the backend and filters its counts were made under. When either changes, every count and the corpus size are dropped and recounted, with the terms kept queued, so counts from two indexes are never served together. A store from before 0.4.0 is reset once, since nothing records what its counts were made under.

**Counts are text-file totals.** The full-text index counts text files, and an item can hold several (a two-volume scan is two). Counts and the corpus size are therefore slightly higher than item counts. The adapter accepts that rather than spend requests counting unique items.

The queue stores bare terms only. It never records whole query strings, requester pubkeys or per-query timestamps, so it cannot reconstruct who searched for what. Per-broker data, if ever needed for rate limiting, lives in a separate short-lived store that is never joined to the term table.

## Rank stats

A pool of 100 is a guess. To check it, the full-text backend counts where served results sat in the Archive's own order, in a separate `stats.sqlite`: per day, pool size and whether the pool was full, how many searches ran, and how many results were served at each position from each ladder step and each band of 10 Archive ranks. Cached answers are not counted. Rows expire after 90 days.

`ditta-ia-adapter rank-report` prints a histogram of served position against Archive rank, how deep the top 10 came from ("90% came from the source's top 40"), and how often a top-10 result came from the last fifth of a full pool. If that tail share is high, the reranker keeps reaching for items near the edge of the pool, and the pool should grow.

The store holds counters only: never queries, terms, per-query times or anything about requesters, and it shares no file with the term store, so the two cannot be joined. The ladder step hints at query length (a phrase step means two or more words), so on a quiet day the counters show roughly how many searches ran and how strict their matches were, but nothing about what was searched or by whom.

## Announcement and pricing

The adapter is free: it announces a single `per_query` pricing option at a price of 0 and never sends a 402 challenge. Free access avoids charging for the Archive's data and keeps the adapter a clean demonstration.

The announcement names the indexer role with a corpus description pointing to the Internet Archive, and serves corpus statistics from the term table. Every message the adapter sends is signed, result sets and errors included. Its total document count is the size the active backend publishes: for full-text, the hit total for `*:*` under the media-type and language filters (36,736,966 English or unlabelled text files on 2026-10-31, of 49.8 million in the whole full-text index), refreshed daily.

For discovery, the adapter keeps the newest valid announcement per pubkey, relays each new one unchanged to the other nodes it knows, and announces itself to every node it has seen, starting from a seed list in configuration. It never connects to loopback, private or link-local addresses unless configured to, and bounds its outbound connection rate.

## Repository and README

The adapter lives in a public repo on the maintainer's GitHub account, not under the ditta-protocol organisation, because it is a third-party implementation rather than an official part of DITTA. It starts empty, so an MIT LICENSE file and a Python .gitignore go in with the first commit; the .gitignore also excludes the SQLite stores and the response cache.

The README is the adapter's front page for two readers: the Internet Archive, checking what is hitting its servers, and developers who want to run it or build a similar adapter. It has to be thorough and plain, and cover:

- **What DITTA is:** a short explanation of the protocol, linking to the DITTA spec. The architecture diagram goes in near the top, as an SVG file in the repo.
- **What this adapter does:** queries in, Internet Archive results out, free, and unofficial.
- **How queries are read:** term escaping, the search ladder and the reranker, and why the raw string is never passed through.
- **How it treats the Archive:** the endpoint, the User-Agent, rate limits, backoff, caching and the concurrency cap, with a link to the Archive's automated access guidance.
- **Corpus statistics and rank stats:** how they are built, and what each store never records.
- **Running it:** requirements, configuration, credentials and how to start it.
- **Limits:** what the adapter leaves out, on purpose.

## Out of scope, and open items

Three Internet Archive services are deliberately left out:

- **Changes API:** not needed for freshness, since the corpus timestamp comes from the stats refresh.
- **Views API:** possible ranking input later; the reranker uses only the query, the passages and the Archive's order.
- **Wayback Machine:** a different corpus with no full-text search, better served by a separate adapter.

Also left out on purpose, with the reason:

- **Query interpretation** (names, dates, phrases, fields): the loose phrase step covers the common case; revisit when the rank stats show where it would help.
- **Stemming and stop words in the reranker:** exact matching is simple, predictable and not tied to one language.
- **Unique item counts:** counts are text-file totals, a small overcount, rather than extra requests per term.

Open items:

- [x] Whether to filter by `mediatype`, for example to texts only, which gives better snippets. Decided: texts only by default, configurable. First decided as no filter; changed on 2026-10-29 because video and podcasts crowded the results.
- [x] Snippet length limit. Decided: 300 characters.
- [x] Whether result sets are signed. Decided: every message is signed.
- [x] Cache window and the cap on concurrent requests. Decided: 10 minutes and 2 requests.
- [x] Confirm that the paged search returns results in relevance order. Confirmed 2026-10-29.
- [x] Choose the license: MIT.
- [x] Search the text inside items, not only metadata. Decided 2026-10-30: full-text by default, metadata kept as a configurable fallback.
- [x] Which full-text endpoint. Decided 2026-10-30: the search service behind archive.org's search page for everything, since it sorts and returns descriptions; `meta_*` filters confirmed live on 2026-10-31.
- [x] What a document is. Decided 2026-10-30: an item, served at one URL; counts stay text-file totals.
- [x] Pool size and requests per search. Decided 2026-10-30: 100 candidates, one page per ladder step, at most four requests.
- [x] How the reranker weighs coverage. Decided 2026-10-31: coverage is its own first key, not a weight, after a test showed that with weights, an item missing a word could outrank one holding them all.
- [x] What happens to stored counts when the backend or filters change. Decided 2026-10-31: recount everything.
