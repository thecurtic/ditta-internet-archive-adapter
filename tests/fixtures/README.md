# Archive response fixtures

## Metadata backend

Real `advancedsearch.php?output=json` responses, recorded 2026-10-29 with
one polite run of six requests. The default test run replays them and never
contacts the Archive.

| File | Request |
| --- | --- |
| `search_two_terms.json` | `q=lighthouse AND keeper`, rows 8, page 1, the adapter's search fields |
| `search_no_hits.json` | `q=zzqxjvw AND lighthouse`, rows 8 |
| `count_lighthouse.json` | `q=lighthouse`, rows 1, `sort=publicdate desc` |
| `total_all.json` | `q=*:*`, rows 0 |
| `search_error.json` | `q=title:(`, which the Archive answers with HTTP 200 and an `error` key |

The same run confirmed that the paged search returns relevance order, not
identifier order. To refresh, record again with the same requests and keep
the file names.

## Full-text backend

Real responses from `GET /services/search/beta/page_production/` with
`service_backend=fts`, `page=1` and `aggregations=false`, recorded
2026-10-31 with three spaced requests. Every query ends with
`AND meta_mediatype:texts AND (meta_languageSorter:English OR (*:* -meta_languageSorter:*))`.
Each response's `session_context` (the caller's IP address and User-Agent)
and `forensics` (the account) blocks were removed before saving.

| File | Request |
| --- | --- |
| `fulltext_count_lighthouse.json` | `!L lighthouse`, 3 hits, `sort=publicdate:desc` |
| `fulltext_search_phrase.json` | `!L "gore street vancouver"~3`, 20 hits per page (13 found) |
| `fulltext_total_texts.json` | `!L *:*`, 1 hit |
| `fulltext_no_hits.json` | Built by hand from the count response, with the hits emptied |
| `fulltext_failed.json` | Built by hand: `header.succeeded` false, the service's failure flag |

The service's failure response has not been seen live, so
`fulltext_failed.json` follows only the header shape the recorded responses
share.

The opt-in live test (`tests/test_live.py`) checked on 2026-10-31 that the
filters as the adapter sends them, `meta_mediatype:(texts)` and
`meta_languageSorter:(English)` in brackets, give the same totals (970,507
for `lighthouse`, 36,736,966 for `*:*`), and that a 100-hit page is accepted.
