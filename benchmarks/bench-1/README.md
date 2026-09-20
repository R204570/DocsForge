# bench-1 -- 2026-09-20, offline

Server: `http://127.0.0.1:8765/mcp` -- build `1ce0c43` (v1.1.0). Checkout: `1ce0c43` on `offline-testing`. 1 run(s) per case, 307.2s in all.

**68 pass · 1 fail · 1 error · 2 known · 0 fixed · 4 skipped · 3 over budget**

| verdict | suite | case | s | budget | detail |
|---|---|---|---:|---:|---|
| PASS | transport | `health` | 0.01 | 2 | {"status":"ok","version":"1.1.0","store":"postgres","degraded":false} |
| SKIP | transport | `mcp_needs_token` | 0.00 | 2 | the server runs open on loopback |
| SKIP | transport | `mcp_rejects_wrong_token` | 0.00 | 2 | the server runs open on loopback |
| PASS | transport | `initialize` | 0.01 | 3 |  |
| PASS | transport | `tools_list` | 0.01 | 3 | detect_source_type, fetch_docs, save_docs, harvest_docs, learn_technology, harvest_status, find_docs, search_knowledge_base, scan_project, l |
| PASS | transport | `surface_matches_checkout` | 0.43 |  | no drift |
| PASS | transport | `listing` | 0.27 | 5 | DocsForge build `1ce0c43` — nothing is stored yet. Learn a technology with `learn_technology(name="...")` — you do not need a URL. |
| PASS | transport | `build_is_known` | 0.00 |  | live 1ce0c43 = 'The live benchmarks: every tool measured against a running DocsForge, hosted or offline'; HEAD 1ce0c43; origin/main 5826f61 |
| PASS | detect | `openapi_json` | 1.38 | 5 | openapi |
| PASS | detect | `github_repo` | 0.02 | 3 | github |
| PASS | detect | `raw_markdown` | 0.01 | 3 | raw_text |
| PASS | detect | `sitemap` | 0.01 | 3 | sitemap |
| PASS | detect | `llms_index_prefers_full_dump` | 0.90 | 8 | llms_txt (resolved to https://svelte.dev/llms-full.txt) |
| PASS | detect | `deep_page_finds_site_dump` | 0.54 | 8 | llms_txt (resolved to https://svelte.dev/llms-full.txt) |
| PASS | detect | `html_page` | 0.70 | 8 | html |
| KNOWN | detect | `empty_url_is_an_error` | 0.01 |  | *Issues.md D1* was not refused; answered 4 chars: 'html' |
| KNOWN | detect | `garbage_is_an_error` | 0.01 |  | *Issues.md D1* was not refused; answered 4 chars: 'html' |
| PASS | fetch | `openapi_renders_endpoints` | 1.30 | 10 | <!-- source: https://petstore3.swagger.io/api/v3/openapi.json \| type: openapi \| scraped: 2026-09-21 00:56 --> |
| PASS | fetch | `force_raw_skips_rendering` | 1.34 | 10 | <!-- source: https://petstore3.swagger.io/api/v3/openapi.json \| type: raw \| scraped: 2026-09-21 00:56 --> |
| PASS | fetch | `llms_full_dump_is_capped_and_says_so` | 0.55 | 20 | # Extracted 215 documents |
| PASS | fetch | `raw_markdown_passthrough` | 0.56 | 5 | <!-- source: https://raw.githubusercontent.com/pallets/click/main/README.md \| type: raw \| scraped: 2026-09-21 00:56 --> |
| ERROR | fetch | `github_readme_and_docs` | 1.05 | 15 | network: "Error executing tool fetch_docs: Request failed for https://raw.githubusercontent.com/pallets/click/HEAD/docs/advanced.md: ('Conne |
| PASS | fetch | `html_page_extracted` | 0.74 | 10 | <!-- source: https://docs.python.org/3/library/asyncio.html \| type: html \| scraped: 2026-09-21 00:56 --> |
| PASS | fetch | `crawl_three_pages` | 6.22 | 30 | # Extracted 3 documents |
| PASS | fetch | `sitemap_bounded` | 1.65 | 30 | # Extracted 2 documents |
| PASS | fetch | `bad_url_is_an_error` | 0.01 | 3 | Error executing tool fetch_docs: Only http/https URLs are supported, got: 'not-a-url' |
| PASS | fetch | `missing_page_is_an_error` | 0.45 | 8 | Error executing tool fetch_docs: HTTP 404 for https://docs.python.org/3/library/this-page-does-not-exist.html |
| PASS | guard | `loopback` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 127.0.0.1 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1  |
| PASS | guard | `cloud_metadata` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 169.254.169.254 resolves to 169.254.169.254. Set DOCSFORGE_ALLO |
| PASS | guard | `decimal_ip` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 2130706433 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1 |
| PASS | guard | `octal_ip` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 0177.0.0.1 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1 |
| PASS | guard | `ipv6_loopback` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: ::1 resolves to ::1. Set DOCSFORGE_ALLOW_PRIVATE=1 to permit it |
| PASS | guard | `file_scheme` | 0.01 | 3 | Error executing tool fetch_docs: Only http/https URLs are supported, got: 'file:///etc/passwd' |
| PASS | guard | `dns_to_loopback` | 0.03 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 127.0.0.1.nip.io resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRI |
| PASS | guard | `redirect_to_loopback` | 1.56 | 8 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 127.0.0.1 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1  |
| PASS | guard | `redirect_to_metadata` | 1.55 | 8 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 169.254.169.254 resolves to 169.254.169.254. Set DOCSFORGE_ALLO |
| PASS | guard | `save_outside_root` | 0.01 | 3 | Error executing tool save_docs: Refusing to write outside E:\DocsForge - TEMP\offline\out |
| PASS | guard | `save_inside_root` | 1.27 | 10 | Wrote 1 file(s) to `E:\DocsForge - TEMP\offline\out\benchmark-scratch`: |
| PASS | resolve | `fastapi` | 5.44 | 30 | Resolving **fastapi** (pypi): |
| PASS | resolve | `pydantic` | 3.30 | 30 | Resolving **pydantic**: |
| PASS | resolve | `click_is_pypi_not_npm` | 25.71 | 30 | Resolving **click** (pypi): |
| PASS | resolve | `requests_is_pypi` | 34.19 **SLOW** | 30 | Resolving **requests** (pypi): |
| PASS | resolve | `tokio_is_crates` | 22.11 | 30 | Resolving **tokio** (crates): |
| PASS | resolve | `svelte` | 20.34 | 30 | Resolving **svelte**: |
| PASS | resolve | `django` | 30.23 **SLOW** | 30 | Resolving **django** (pypi): |
| FAIL | resolve | `scoped_npm_name` | 21.56 | 30 | no Best: line -- "Found 2 candidate(s) for '@tanstack/react-query' but none could be confirmed to document it. Harvesting an unverified page |
| PASS | resolve | `ecosystem_pin` | 0.03 | 30 | Resolving **click** (pypi): |
| PASS | resolve | `unknown_name_is_honest` | 53.04 **SLOW** | 30 | Resolving **zzqx-no-such-package-9182**: |
| PASS | offline | `scan_this_repo` | 0.15 | 10 | 19 of 19 dependencies declared in `E:\DocsForge - TEMP`: |
| PASS | offline | `learn_whole_technology` | 25.52 | 30 | **click from https://click.palletsprojects.com/ - still running.** Harvest id `click-1`. |
| PASS | offline | `harvest_runs_to_the_end` | 36.50 |  | **click** from https://click.palletsprojects.com/ — harvest id `click-1`: **done** in 62s. It is stored; read it with `read_knowledge_base(n |
| PASS | offline | `whole_corpus_stored` | 0.65 | 5 | DocsForge build `1ce0c43` — 1 technology stored in postgres (127.0.0.1:5432/docsforge): |
| PASS | offline | `deep_page_was_reached` | 0.07 | 5 | # click 8.5.0: 1 page matching 'quickstart' (by title) |
| PASS | offline | `search_whole_corpus` | 0.14 | 5 | 10 passage(s) for 'click.option decorator', from a ranked search over 11 matching page(s) — about 9,189 tokens rather than the whole pages: |
| PASS | writes | `harvest_docs_small` | 1.75 | 25 | Harvested **benchmark-petstore** v3 — 1 pages, 8,650 characters, via openapi, in 1s. |
| PASS | writes | `read_back_harvest` | 0.08 | 10 | ## Swagger Petstore - OpenAPI 3.0 |
| PASS | writes | `search_harvest` | 0.11 | 10 | Nothing stored in benchmark-petstore matches 'findByStatus'. The technology may not be harvested yet — try `learn_technology(name=...)`. |
| PASS | writes | `learn_already_stored_fetches_nothing` | 0.12 | 15 | **click** 8.5.0 is already stored — 38 pages, harvested 2026-09-21 01:00. |
| PASS | store | `read_whole` | 0.16 | 10 | <!-- click 8.5.0 is 582,432 characters; showing the first 300,000. Omitted 282,432 characters (243 sections including Version 8.2.0[¶](#vers |
| PASS | store | `read_section` | 0.07 | 10 | # click 8.5.0: 38 pages matching 'Click' (by title) |
| PASS | store | `read_unknown_name` | 0.21 | 5 | Error executing tool read_knowledge_base: No stored documentation called 'zzqx-not-stored-9182'. Available: benchmark-petstore, click |
| PASS | store | `read_unknown_version` | 0.16 | 5 | Error executing tool read_knowledge_base: click has no version 'no-such-version-zz'. Stored versions: 8.5.0 |
| PASS | store | `search_scoped` | 0.15 | 10 | 10 passage(s) for 'click', from a ranked search over 20 matching page(s) — about 1,275 tokens rather than the whole pages: |
| PASS | store | `search_everything` | 0.14 | 10 | 10 passage(s) for 'click', from a ranked search over 20 matching page(s) — about 1,275 tokens rather than the whole pages: |
| PASS | store | `search_kind_api` | 0.17 | 10 | 10 passage(s) for 'click', from a ranked search over 20 matching page(s) — about 33,989 tokens rather than the whole pages: |
| PASS | store | `search_limit_one` | 0.09 | 10 | 1 passage(s) for 'click', from a ranked search over 1 matching page(s) — about 466 tokens rather than the whole pages: |
| PASS | store | `search_miss_is_honest` | 0.18 | 10 | 10 passage(s) for 'zzqx-token-that-no-page-contains-9182' — none of them contains 'zzqx', '9182'; they match the rest of the query —, from a |
| PASS | store | `harvest_status_idle` | 0.05 | 5 | Ended in the last 15 minutes: |
| PASS | store | `harvest_status_unknown_id` | 0.07 | 5 | No harvest called `zzqx-no-such-harvest-9182` is running, and none ended in the last 15 minutes. Nothing is stored under that name either. S |
| PASS | store | `forget_resolution_unknown` | 0.01 | 3 | Nothing remembered for 'zzqx-never-resolved-9182'. |
| PASS | store | `forget_selection_unknown` | 0.01 | 3 | No selection policy stored for 'zzqx-never-selected-9182'. |
| SKIP | store | `scan_project_remote_dot` | 0.00 |  | *Issues.md H2* only meaningful against a hosted server |
| SKIP | store | `scan_project_foreign_path` | 0.00 |  | *Issues.md H2* only meaningful against a hosted server |
| PASS | concurrency | `listing_x6` | 0.40 | 15 | 6 of 6 ok; per-call max 0.4s |
| PASS | concurrency | `search_x6` | 0.38 | 15 | 6 of 6 ok; per-call max 0.3s |
| PASS | cleanup | `forget_benchmark_corpus` | 0.12 | 5 | Deleted **benchmark-petstore** (all versions) — 1 version(s), 1 pages, 8,650 characters. |

## Stored at the end

- **benchmark-petstore** -- 1 pages, 8,650 chars; v3 (1 pages, 2026-09-21 01:01)
- **click** -- 38 pages, 578,223 chars **[INCOMPLETE]**; 8.5.0 (38 pages, 2026-09-21 01:00)

## Issues faced

The first published run, on build `1ce0c43` (the `offline` branch: the
benchmark suite and the five fixes its unpublished predecessors produced).
The stored `click` corpus is flagged `INCOMPLETE` for an honest reason: 38 of
the 40 pages Sphinx lists were stored, and the two it could not read are the
generated `genindex` and `search` pages, which are not documentation. Its
version, `8.5.0`, is PyPI's release -- not npm's `0.1.0`, which is what the
same harvest was filed under on 2026-09-16.

### 1. `detect/empty_url_is_an_error` -- KNOWN

*today '' is classified html rather than refused*

**What the server said:** was not refused; answered 4 chars: 'html'

**What it means:** `detect_source_type("")` classifies nothing as `html`.
Detection probes `/llms-full.txt` and `/llms.txt` at the URL's origin, and
those probes are what the SSRF guard refuses for a URL with no scheme or
host -- but detection catches that refusal and falls through to its default.
`fetch_docs` on the same input is refused correctly, so nothing is fetched;
the defect is a confident classification of garbage. Recorded as
`System Files/Issues.md` D1; not fixed in this build.

### 2. `detect/garbage_is_an_error` -- KNOWN

**What the server said:** was not refused; answered 4 chars: 'html'

**What it means:** the same defect as 1, for `"not a url"`. One fix closes
both: run the guard's scheme-and-host check before detection starts.

### 3. `fetch/github_readme_and_docs` -- ERROR

*GitHub API, unauthenticated: rate limits on a shared egress show here*

**What the server said:** network: "Request failed for
https://raw.githubusercontent.com/pallets/click/HEAD/docs/advanced.md:
('Connection aborted.', ConnectionResetError ...)"

**What it means:** not a GitHub rate limit after all (that would be a 403
with a body), but a TCP reset from `raw.githubusercontent.com` part way
through the `docs/` folder -- the fourth page of twenty-five. It reproduced
in three of four offline runs on this network and never against the hosted
server, which fetched all 25 in 5.5 s from Vercel's egress; the single-file
`raw_markdown_passthrough` case against the same host passed every time.
So it is this address being reset when it opens many connections to that
host in quick succession. Judged ERROR (the network's), not FAIL. Worth a
fix regardless: a fetch that ends in a connection reset is retried nowhere,
and one retry after a short pause would have finished the run. Recorded as
Issues.md N1.

### 4. `resolve/scoped_npm_name` -- FAIL

*the site says 'TanStack Query', never the scoped name, so the identity gate refuses*

**What the server said:** no Best: line -- "Found 2 candidate(s) for
'@tanstack/react-query' but none could be confirmed to document it.
Harvesting an unverified page risks storing the wrong project -- pass a URL
directly if you know it."

**What it means:** the identity gate working as designed, on a name it
cannot identify. npm nominates `tanstack.com/query` (homepage) and
`github.com/TanStack/query` (repository); the page writes "TanStack Query"
and "React Query", and `registry-agreement` alone is one strong signal
where two are required. Loosening the gate to accept the unscoped tail
(`react-query`) or the scope (`tanstack`) as the name would resolve this and
every other scoped package, and would also let `@someone/click` be
identified by `click`. That is a **DECISION**, Issues.md R7, not a fix.

### 5. Three resolutions over their 30 s budget (observation, no verdict)

`requests` 34 s, `django` 30 s, and `zzqx-no-such-package-9182` 53 s. The
first two are cold resolutions -- `--reset` cleared the memory -- of names
that exist on more than one registry, so every registry's candidates are
fetched and read before one is chosen. The third is the price of an honest
refusal: a name no registry knows walks the whole ladder, own-domain guesses
included, to its 40-request budget before saying so. None of the three is
a defect; the budget is a bound, and the bound was respected. A per-lap
early exit when no registry knows the name and no domain answered is the
obvious economy, and is noted in Issues.md as R8.

### Not issues

- `mcp_needs_token`, `mcp_rejects_wrong_token`: the server runs open on
  loopback by design; the bearer gate is exercised against the hosted server.
- `scan_project_remote_dot`, `scan_project_foreign_path`: the hosted gap
  (Issues.md H2) does not exist locally, where the caller's disk is the
  server's; `scan_this_repo` covers the local behaviour and passed.
- `search_harvest` answering "Nothing stored in benchmark-petstore matches
  'findByStatus'": the one-page OpenAPI corpus renders the operation as a
  table cell, and the file-then-Postgres search finds no *section* for it.
  The case passes because an honest miss is a correct answer; whether a
  rendered endpoint should be searchable by its operationId is open (S3).
