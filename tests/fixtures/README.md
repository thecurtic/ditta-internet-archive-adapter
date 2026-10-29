# Archive response fixtures

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
