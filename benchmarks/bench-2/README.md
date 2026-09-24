# bench-2 -- 2026-09-21, offline

Server: `http://127.0.0.1:8765/mcp` -- build `4cbdbb1` (v1.1.0). Checkout: `4cbdbb1` on `offline-testing`. 1 run(s) per case, 787.6s in all.

**106 pass · 13 fail · 1 error · 2 known · 0 fixed · 4 skipped · 4 over budget**

| verdict | suite | case | s | budget | detail |
|---|---|---|---:|---:|---|
| PASS | transport | `health` | 0.01 | 2 | {"status":"ok","version":"1.1.0","store":"postgres","degraded":false} |
| SKIP | transport | `mcp_needs_token` | 0.00 | 2 | the server runs open on loopback |
| SKIP | transport | `mcp_rejects_wrong_token` | 0.00 | 2 | the server runs open on loopback |
| PASS | transport | `initialize` | 0.01 | 3 |  |
| PASS | transport | `tools_list` | 0.01 | 3 | detect_source_type, fetch_docs, save_docs, harvest_docs, learn_technology, harvest_status, find_docs, search_knowledge_base, scan_project, l |
| PASS | transport | `surface_matches_checkout` | 0.42 |  | no drift |
| PASS | transport | `listing` | 0.19 | 5 | DocsForge build `4cbdbb1` — nothing is stored yet. Learn a technology with `learn_technology(name="...")` — you do not need a URL. |
| PASS | transport | `build_is_known` | 0.00 |  | live 4cbdbb1 = 'The README says what the build is now: the live suite, the benches, the guards, and where the record moved'; HEAD 4cbdbb1; o |
| PASS | detect | `openapi_json` | 1.39 | 5 | openapi |
| PASS | detect | `github_repo` | 0.01 | 3 | github |
| PASS | detect | `raw_markdown` | 0.01 | 3 | raw_text |
| PASS | detect | `sitemap` | 0.01 | 3 | sitemap |
| PASS | detect | `llms_index_prefers_full_dump` | 0.79 | 8 | llms_txt (resolved to https://svelte.dev/llms-full.txt) |
| PASS | detect | `deep_page_finds_site_dump` | 0.64 | 8 | llms_txt (resolved to https://svelte.dev/llms-full.txt) |
| PASS | detect | `html_page` | 0.67 | 8 | html |
| KNOWN | detect | `empty_url_is_an_error` | 0.01 |  | *Issues.md D1* was not refused; answered 4 chars: 'html' |
| KNOWN | detect | `garbage_is_an_error` | 0.01 |  | *Issues.md D1* was not refused; answered 4 chars: 'html' |
| PASS | fetch | `openapi_renders_endpoints` | 1.33 | 10 | <!-- source: https://petstore3.swagger.io/api/v3/openapi.json \| type: openapi \| scraped: 2026-09-21 22:27 --> |
| PASS | fetch | `force_raw_skips_rendering` | 1.38 | 10 | <!-- source: https://petstore3.swagger.io/api/v3/openapi.json \| type: raw \| scraped: 2026-09-21 22:27 --> |
| PASS | fetch | `llms_full_dump_is_capped_and_says_so` | 0.71 | 20 | # Extracted 215 documents |
| ERROR | fetch | `raw_markdown_passthrough` | 0.26 | 5 | network: "Error executing tool fetch_docs: Request failed for https://raw.githubusercontent.com/pallets/click/main/README.md: ('Connection a |
| PASS | fetch | `github_readme_and_docs` | 9.30 | 15 | # Extracted 25 documents |
| PASS | fetch | `html_page_extracted` | 0.62 | 10 | <!-- source: https://docs.python.org/3/library/asyncio.html \| type: html \| scraped: 2026-09-21 22:27 --> |
| PASS | fetch | `crawl_three_pages` | 6.90 | 30 | # Extracted 3 documents |
| PASS | fetch | `sitemap_bounded` | 1.60 | 30 | # Extracted 2 documents |
| PASS | fetch | `bad_url_is_an_error` | 0.01 | 3 | Error executing tool fetch_docs: Only http/https URLs are supported, got: 'not-a-url' |
| PASS | fetch | `missing_page_is_an_error` | 0.47 | 8 | Error executing tool fetch_docs: HTTP 404 for https://docs.python.org/3/library/this-page-does-not-exist.html |
| PASS | guard | `loopback` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 127.0.0.1 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1  |
| PASS | guard | `cloud_metadata` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 169.254.169.254 resolves to 169.254.169.254. Set DOCSFORGE_ALLO |
| PASS | guard | `decimal_ip` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 2130706433 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1 |
| PASS | guard | `octal_ip` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 0177.0.0.1 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1 |
| PASS | guard | `ipv6_loopback` | 0.01 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: ::1 resolves to ::1. Set DOCSFORGE_ALLOW_PRIVATE=1 to permit it |
| PASS | guard | `file_scheme` | 0.00 | 3 | Error executing tool fetch_docs: Only http/https URLs are supported, got: 'file:///etc/passwd' |
| PASS | guard | `dns_to_loopback` | 0.02 | 3 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 127.0.0.1.nip.io resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRI |
| PASS | guard | `redirect_to_loopback` | 1.61 | 8 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 127.0.0.1 resolves to 127.0.0.1. Set DOCSFORGE_ALLOW_PRIVATE=1  |
| PASS | guard | `redirect_to_metadata` | 3.93 | 8 | Error executing tool fetch_docs: Refusing to fetch private/loopback address: 169.254.169.254 resolves to 169.254.169.254. Set DOCSFORGE_ALLO |
| PASS | guard | `save_outside_root` | 0.01 | 3 | Error executing tool save_docs: Refusing to write outside E:\DocsForge - TEMP\offline\out |
| PASS | guard | `save_inside_root` | 1.31 | 10 | Wrote 1 file(s) to `E:\DocsForge - TEMP\offline\out\benchmark-scratch`: |
| PASS | resolve | `fastapi` | 9.04 | 30 | Resolving **fastapi** (pypi): |
| PASS | resolve | `pydantic` | 3.34 | 30 | Resolving **pydantic**: |
| PASS | resolve | `click_is_pypi_not_npm` | 26.95 | 30 | Resolving **click** (pypi): |
| PASS | resolve | `requests_is_pypi` | 36.00 **SLOW** | 30 | Resolving **requests** (pypi): |
| PASS | resolve | `tokio_is_crates` | 24.61 | 30 | Resolving **tokio** (crates): |
| PASS | resolve | `svelte` | 23.34 | 30 | Resolving **svelte**: |
| PASS | resolve | `django` | 31.64 **SLOW** | 30 | Resolving **django** (pypi): |
| FAIL | resolve | `scoped_npm_name` | 27.06 | 30 | no Best: line -- "Found 2 candidate(s) for '@tanstack/react-query' but none could be confirmed to document it. Harvesting an unverified page |
| PASS | resolve | `ecosystem_pin` | 0.04 | 30 | Resolving **click** (pypi): |
| PASS | resolve | `unknown_name_is_honest` | 22.14 | 30 | Resolving **zzqx-no-such-package-9182**: |
| PASS | offline | `scan_this_repo` | 0.15 | 10 | 19 of 19 dependencies declared in `E:\DocsForge - TEMP`: |
| PASS | offline | `learn_whole_technology` | 25.22 | 30 | **click from https://click.palletsprojects.com/ - still running.** Harvest id `click-1`. |
| PASS | offline | `harvest_runs_to_the_end` | 40.85 |  | **click** from https://click.palletsprojects.com/ — harvest id `click-1`: **done** in 66s. It is stored; read it with `read_knowledge_base(n |
| PASS | offline | `whole_corpus_stored` | 0.42 | 5 | DocsForge build `4cbdbb1` — 1 technology stored in postgres (127.0.0.1:5432/docsforge): |
| PASS | offline | `deep_page_was_reached` | 0.20 | 5 | # click 8.5.0: 1 page matching 'quickstart' (by title) |
| PASS | offline | `search_whole_corpus` | 0.15 | 5 | 10 passage(s) for 'click.option decorator', from a ranked search over 11 matching page(s) — about 9,189 tokens rather than the whole pages: |
| PASS | versions | `django_current` | 25.62 | 90 | **django** from https://docs.djangoproject.com/ — harvest id `django-2`: **done** in 25s. It is stored; read it with `read_knowledge_base(na |
| PASS | versions | `django_pinned` | 24.05 | 90 | **django** is stored, but not version '4.2' (have: 6.1.1). Harvesting it now. |
| PASS | versions | `django_two_versions_listed` | 0.69 | 5 | DocsForge build `4cbdbb1` — 2 technologies stored in postgres (127.0.0.1:5432/docsforge): |
| PASS | versions | `django_pinned_pages_are_4.2` | 0.10 | 10 | <!-- django 4.2 is 449,422 characters; showing the first 300,000. Omitted 149,422 characters (96 sections including How to deploy with ASGI  |
| FAIL | versions | `django_current_pages_are_current` | 0.11 | 10 | 24 of the 24 pages shown under '6.1.1' are not the current release's: https://docs.djangoproject.com/en/dev/contents |
| PASS | versions | `django_default_is_current` | 0.12 | 15 | **django** 6.1.1 is already stored — 40 pages, harvested 2026-09-21 22:32. |
| PASS | versions | `django_search_scoped_to_4.2` | 0.09 | 10 | 10 passage(s) for 'custom management command', from a ranked search over 5 matching page(s) — about 5,799 tokens rather than the whole pages |
| PASS | versions | `django_search_names_the_version` | 0.13 | 10 | 10 passage(s) for 'custom management command', from a ranked search over 10 matching page(s) — about 5,253 tokens rather than the whole page |
| PASS | versions | `django_unknown_version_names_both` | 0.09 | 5 | Error executing tool read_knowledge_base: django has no version '0.0.0-zz'. Stored versions: 6.1.1, 4.2 |
| PASS | versions | `poetry_current` | 35.88 | 90 | **poetry** from https://python-poetry.org/ — harvest id `poetry-4`: **done** in 36s. It is stored; read it with `read_knowledge_base(name="p |
| PASS | versions | `poetry_pinned` | 6.87 | 90 | **poetry** is stored, but not version '1.8' (have: 2.5.1). Harvesting it now. |
| PASS | versions | `poetry_two_versions_listed` | 0.56 | 5 | DocsForge build `4cbdbb1` — 3 technologies stored in postgres (127.0.0.1:5432/docsforge): |
| PASS | versions | `poetry_pinned_pages_are_1.8` | 0.10 | 10 | ## Basic usage \| 1.8 \| Documentation \| Poetry - Python dependency management and packaging made easy |
| FAIL | versions | `poetry_current_pages_are_current` | 0.11 | 10 | 11 of the 18 pages shown under '2.5.1' are not the current release's: https://python-poetry.org/docs/1.8/basic-usage |
| PASS | versions | `poetry_default_is_current` | 0.11 | 15 | **poetry** 2.5.1 is already stored — 40 pages, harvested 2026-09-21 22:33. |
| PASS | versions | `poetry_search_scoped_to_1.8` | 0.12 | 10 | 10 passage(s) for 'poetry add', from a ranked search over 10 matching page(s) — about 3,785 tokens rather than the whole pages: |
| PASS | versions | `poetry_search_names_the_version` | 0.11 | 10 | 10 passage(s) for 'poetry add', from a ranked search over 8 matching page(s) — about 5,252 tokens rather than the whole pages: |
| PASS | versions | `poetry_unknown_version_names_both` | 0.10 | 5 | Error executing tool read_knowledge_base: poetry has no version '0.0.0-zz'. Stored versions: 2.5.1, 1.8 |
| PASS | versions | `jest_current` | 76.54 | 90 | **jest** from https://jestjs.io/ — harvest id `jest-6`: **done** in 76s. It is stored; read it with `read_knowledge_base(name="jest")`. |
| PASS | versions | `jest_pinned` | 46.90 | 90 | **jest** from https://jestjs.io/ — harvest id `jest-7`: **done** in 46s. It is stored; read it with `read_knowledge_base(name="jest")`. |
| PASS | versions | `jest_two_versions_listed` | 0.21 | 5 | DocsForge build `4cbdbb1` — 4 technologies stored in postgres (127.0.0.1:5432/docsforge): |
| PASS | versions | `jest_pinned_pages_are_29.7` | 0.13 | 10 | <!-- jest 29.7 is 527,408 characters; showing the first 300,000. Omitted 227,408 characters (236 sections including Additional Configuration |
| FAIL | versions | `jest_current_pages_are_current` | 0.14 | 10 | 13 of the 13 pages shown under '2026-09-21' are not the current release's: https://jestjs.io/docs/29.7/api |
| FAIL | versions | `jest_default_is_current` | 0.18 | 15 | the default version is '29.7'; expected '2026-09-21' (the current release) over '29.7' |
| PASS | versions | `jest_search_scoped_to_29.7` | 0.10 | 10 | 10 passage(s) for 'expect matchers', from a ranked search over 8 matching page(s) — about 3,113 tokens rather than the whole pages: |
| PASS | versions | `jest_search_names_the_version` | 0.15 | 10 | 10 passage(s) for 'expect matchers', from a ranked search over 6 matching page(s) — about 3,113 tokens rather than the whole pages: |
| PASS | versions | `jest_unknown_version_names_both` | 0.11 | 5 | Error executing tool read_knowledge_base: jest has no version '0.0.0-zz'. Stored versions: 29.7, 2026-09-21 |
| PASS | versions | `sequelize_current` | 145.15 **SLOW** | 90 | **sequelize** from https://sequelize.org/ — harvest id `sequelize-8`: **done** in 2m. It is stored; read it with `read_knowledge_base(name=" |
| PASS | versions | `sequelize_pinned` | 100.51 **SLOW** | 90 | **sequelize** from https://sequelize.org/ — harvest id `sequelize-9`: **done** in 2m. It is stored; read it with `read_knowledge_base(name=" |
| PASS | versions | `sequelize_two_versions_listed` | 0.36 | 5 | DocsForge build `4cbdbb1` — 5 technologies stored in postgres (127.0.0.1:5432/docsforge): |
| PASS | versions | `sequelize_pinned_pages_are_v7` | 0.14 | 10 | ## Sequelize v7 (alpha) \| Sequelize |
| PASS | versions | `sequelize_current_pages_are_current` | 0.27 | 10 | <!-- sequelize 2026-09-21 is 341,117 characters; showing the first 300,000. Omitted 41,117 characters (41 sections including Undoing Migrati |
| PASS | versions | `sequelize_default_is_pinned` | 0.13 | 15 | **sequelize** v7 is already stored — 40 pages, harvested 2026-09-21 22:38. |
| PASS | versions | `sequelize_search_scoped_to_v7` | 0.14 | 10 | 2 passage(s) for 'findAll where', from a ranked search over 1 matching page(s) — about 383 tokens rather than the whole pages: |
| PASS | versions | `sequelize_search_names_the_version` | 0.12 | 10 | 10 passage(s) for 'findAll where', from a ranked search over 8 matching page(s) — about 2,944 tokens rather than the whole pages: |
| PASS | versions | `sequelize_unknown_version_names_both` | 0.22 | 5 | Error executing tool read_knowledge_base: sequelize has no version '0.0.0-zz'. Stored versions: v7, 2026-09-21 |
| PASS | versions | `pydantic_current` | 2.54 | 90 | Resolved **pydantic** to https://pydantic.dev/docs/validation/latest/llms.txt |
| FAIL | versions | `pydantic_pinned` | 2.99 | 90 | 1.10 was stored but not confirmed: 'Version '1.10' is the label you asked for, not a finding' |
| PASS | versions | `pydantic_two_versions_listed` | 0.35 | 5 | DocsForge build `4cbdbb1` — 6 technologies stored in postgres (127.0.0.1:5432/docsforge): |
| FAIL | versions | `pydantic_pinned_pages_are_1.10` | 0.21 | 10 | 106 of the 106 pages shown under '1.10' are not under /docs/validation/1.10/: https://pydantic.dev/docs/validation/latest/llms-full.txt#quer |
| PASS | versions | `pydantic_current_pages_are_current` | 0.25 | 10 | <!-- pydantic latest is 2,080,290 characters; showing the first 300,000. Omitted 1,780,290 characters (1977 sections including Enums, Valida |
| FAIL | versions | `pydantic_default_is_current` | 0.16 | 15 | the default version is '1.10'; expected 'latest' (the current release) over '1.10' |
| PASS | versions | `pydantic_search_scoped_to_1.10` | 0.15 | 10 | 10 passage(s) for 'BaseModel validation', from a ranked search over 10 matching page(s) — about 7,118 tokens rather than the whole pages: |
| PASS | versions | `pydantic_search_names_the_version` | 0.18 | 10 | 10 passage(s) for 'BaseModel validation', from a ranked search over 5 matching page(s) — about 6,236 tokens rather than the whole pages: |
| PASS | versions | `pydantic_unknown_version_names_both` | 0.15 | 5 | Error executing tool read_knowledge_base: pydantic has no version '0.0.0-zz'. Stored versions: 1.10, latest |
| PASS | writes | `harvest_docs_small` | 1.85 | 25 | Harvested **benchmark-petstore** v3 — 1 pages, 8,650 characters, via openapi, in 2s. |
| PASS | writes | `read_back_harvest` | 0.08 | 10 | ## Swagger Petstore - OpenAPI 3.0 |
| PASS | writes | `search_harvest` | 0.23 | 10 | Nothing stored in benchmark-petstore matches 'findByStatus'. The technology may not be harvested yet — try `learn_technology(name=...)`. |
| PASS | writes | `learn_already_stored_fetches_nothing` | 0.32 | 15 | **pydantic** 1.10 is already stored — 695 pages, harvested 2026-09-21 22:40. |
| PASS | store | `read_whole` | 0.20 | 10 | <!-- pydantic 1.10 is 2,080,290 characters; showing the first 300,000. Omitted 1,780,290 characters (1977 sections including Enums, Validati |
| PASS | store | `read_section` | 0.10 | 10 | # pydantic 1.10: 1 page matching 'Querying' (by title) |
| PASS | store | `read_unknown_name` | 0.13 | 5 | Error executing tool read_knowledge_base: No stored documentation called 'zzqx-not-stored-9182'. Available: benchmark-petstore, click, djang |
| PASS | store | `read_unknown_version` | 0.12 | 5 | Error executing tool read_knowledge_base: pydantic has no version 'no-such-version-zz'. Stored versions: 1.10, latest |
| PASS | store | `search_scoped` | 0.15 | 10 | 10 passage(s) for 'pydantic', from a ranked search over 10 matching page(s) — about 7,516 tokens rather than the whole pages: |
| PASS | store | `search_everything` | 0.08 | 10 | 10 passage(s) for 'pydantic', from a ranked search over 10 matching page(s) — about 7,516 tokens rather than the whole pages: |
| PASS | store | `search_kind_api` | 0.15 | 10 | No passage in pydantic answers 'pydantic' directly. 10 page(s) matched but were not of kind 'api'. `read_knowledge_base` will show whole pag |
| PASS | store | `search_limit_one` | 0.10 | 10 | 1 passage(s) for 'pydantic', from a ranked search over 1 matching page(s) — about 864 tokens rather than the whole pages: |
| PASS | store | `search_miss_is_honest` | 0.21 | 10 | 10 passage(s) for 'zzqx-token-that-no-page-contains-9182' — none of them contains 'zzqx', '9182'; they match the rest of the query —, from a |
| PASS | store | `harvest_status_idle` | 0.09 | 5 | Ended in the last 15 minutes: |
| PASS | store | `harvest_status_unknown_id` | 0.12 | 5 | No harvest called `zzqx-no-such-harvest-9182` is running, and none ended in the last 15 minutes. Nothing is stored under that name either. S |
| PASS | store | `forget_resolution_unknown` | 0.01 | 3 | Nothing remembered for 'zzqx-never-resolved-9182'. |
| PASS | store | `forget_selection_unknown` | 0.01 | 3 | No selection policy stored for 'zzqx-never-selected-9182'. |
| SKIP | store | `scan_project_remote_dot` | 0.00 |  | *Issues.md H2* only meaningful against a hosted server |
| SKIP | store | `scan_project_foreign_path` | 0.00 |  | *Issues.md H2* only meaningful against a hosted server |
| PASS | concurrency | `listing_x6` | 0.50 | 15 | 6 of 6 ok; per-call max 0.5s |
| PASS | concurrency | `search_x6` | 0.33 | 15 | 6 of 6 ok; per-call max 0.3s |
| PASS | cleanup | `forget_benchmark_corpus` | 0.09 | 5 | Deleted **benchmark-petstore** (all versions) — 1 version(s), 1 pages, 8,650 characters. |
| FAIL | cleanup | `forget_django` | 0.10 | 5 | two versions were stored but the report says: 'Deleted **django** (all versions) — 1 version(s), 80 pages, 845,641 characters.' |
| FAIL | cleanup | `forget_poetry` | 0.09 | 5 | two versions were stored but the report says: 'Deleted **poetry** (all versions) — 1 version(s), 55 pages, 688,955 characters.' |
| FAIL | cleanup | `forget_jest` | 0.10 | 5 | two versions were stored but the report says: 'Deleted **jest** (all versions) — 1 version(s), 77 pages, 1,098,374 characters.' |
| FAIL | cleanup | `forget_sequelize` | 0.12 | 5 | two versions were stored but the report says: 'Deleted **sequelize** (all versions) — 1 version(s), 80 pages, 624,472 characters.' |
| FAIL | cleanup | `forget_pydantic` | 0.12 | 5 | two versions were stored but the report says: 'Deleted **pydantic** (all versions) — 1 version(s), 1,390 pages, 4,014,536 characters.' |

## Stored at the end

- **benchmark-petstore** -- 1 pages, 8,650 chars; v3 (1 pages, 2026-09-21 22:40)
- **click** -- 38 pages, 578,223 chars **[INCOMPLETE]**; 8.5.0 (38 pages, 2026-09-21 22:31)
- **django** -- 80 pages, 845,641 chars **[INCOMPLETE]**; 6.1.1 (40 pages, 2026-09-21 22:32), 4.2 (40 pages, 2026-09-21 22:32)
- **jest** -- 77 pages, 1,098,374 chars **[INCOMPLETE]**; 29.7 (37 pages, 2026-09-21 22:35), 2026-09-21 (40 pages, 2026-09-21 22:34)
- **poetry** -- 55 pages, 688,955 chars **[INCOMPLETE]**; 2.5.1 (40 pages, 2026-09-21 22:33), 1.8 (15 pages, 2026-09-21 22:33)
- **pydantic** -- 1390 pages, 4,014,536 chars; 1.10 (695 pages, 2026-09-21 22:40), latest (695 pages, 2026-09-21 22:40)
- **sequelize** -- 80 pages, 624,472 chars **[INCOMPLETE]**; v7 (40 pages, 2026-09-21 22:38), 2026-09-21 (40 pages, 2026-09-21 22:36)

## Issues faced

The second published run, and the first with the `versions` suite: five
technologies each learned at two releases by name
(`scripts/benchmark/versions.py`), against server build `4cbdbb1`. The
general suites repeat bench-1's picture -- `click` whole under PyPI's
8.5.0, the two D1 gaps, the tanstack refusal, a connection reset from
GitHub's raw host. The new suite is where the run earned its keep: seven of
its forty-five cases failed, and every failure is DocsForge's. The five
cleanup failures are one counting bug, reported five times.

### 1. `detect/empty_url_is_an_error` -- KNOWN

*today '' is classified html rather than refused*

**What the server said:** was not refused; answered 4 chars: 'html'

**What it means:** unchanged from bench-1: `Issues.md` D1, not fixed in
this build.

### 2. `detect/garbage_is_an_error` -- KNOWN

**What the server said:** was not refused; answered 4 chars: 'html'

**What it means:** the same gap, for `"not a url"`.

### 3. `fetch/raw_markdown_passthrough` -- ERROR

**What the server said:** network: "Request failed for
https://raw.githubusercontent.com/pallets/click/main/README.md:
('Connection aborted.', ConnectionResetError"

**What it means:** the reset from `raw.githubusercontent.com` again
(`Issues.md` N1), landing this time on the single-file fetch that passed in
bench-1, while the twenty-five-file `github_readme_and_docs` that failed
there passed here in 9.3 s. Which fetch it lands on is the network's
choice; that nothing retries it is still DocsForge's. Judged ERROR.

### 4. `resolve/scoped_npm_name` -- FAIL

*the site says 'TanStack Query', never the scoped name, so the identity gate refuses*

**What the server said:** no Best: line -- "Found 2 candidate(s) for
'@tanstack/react-query' but none could be confirmed to document it."

**What it means:** `Issues.md` R7, the DECISION on scoped names, still
open. Same answer as bench-1, 27 s instead of 22.

### 5. `versions/django_current_pages_are_current` -- FAIL

*no page stored as the current release comes from another release's path*

**What the server said:** 24 of the 24 pages shown under '6.1.1' are not
the current release's: https://docs.djangoproject.com/en/dev/contents

**What it means:** `learn_technology(name="django")` -- no version, the way
a model asks for the current documentation -- resolved to
`docs.djangoproject.com/`, whose English sitemap lists 11,209 URLs across
every release the project has ever published: `/en/4.2/`, `/en/6.1/`,
`/en/dev/` and a dozen more, side by side. When a release is *asked for*,
`_urls_for_release` narrows the list to it, and the pinned `4.2` harvest in
the row above this one came back all `/en/4.2/`. When none is asked for,
nothing chooses, and the harvest took the sitemap in the sitemap's order:
forty pages of `/en/dev/`, the development branch, stored under PyPI's
current release **6.1.1** with the note that "its URLs name no version".
They name `dev`. Uncapped, this harvest would have stored every release
Django has under one label -- the failure the pinned path was fixed for on
2026-09-19, one branch over. Recorded as `Issues.md` V1.

**Done about it:** `_prefer_current_release` in the engine, run on the
sitemap path when no release was asked for: the registry's release first,
then the unversioned pages, then the front page's redirect and links, then
`stable`/`latest`, then the highest number. Re-run the same day on the
fixed build: forty pages of `/en/6.1/`, labelled 6.1.1, with the note that
the site agrees.

### 6. `versions/poetry_current_pages_are_current` -- FAIL

**What the server said:** 11 of the 18 pages shown under '2.5.1' are not
the current release's: https://python-poetry.org/docs/1.8/basic-usage

**What it means:** V1 on a smaller site. `python-poetry.org` files its
current docs unversioned at `/docs/` and keeps `/docs/1.8/` and
`/docs/main/` beside them; the forty pages stored as **2.5.1** were fifteen
of `/docs/`, twelve of `/docs/1.8/`, twelve of `/docs/main/` and the front
page -- three releases under the current one's number, with 7+ of the real
current pages "still queued" behind them.

**Done about it:** V1's fix; re-run, the current harvest is the sixteen
pages of `/docs/`, whole.

### 7. `versions/jest_current_pages_are_current` -- FAIL

**What the server said:** 13 of the 13 pages shown under '2026-09-21' are
not the current release's: https://jestjs.io/docs/29.7/api

**What it means:** V1 a third time, with a second consequence. `jestjs.io`
was found by its own domain before any registry was asked, so no release
labels the harvest and it is filed under the day: thirty-seven pages of
`/docs/29.7/` and three of `/docs/30.0/`, two majors behind npm's 30.5.2,
while the unversioned `/docs/` -- the current release, thirty-seven pages
-- was never reached. The pinned `29.7` in the row above is the same
thirty-seven pages, correctly labelled. So the store holds Jest 29.7 twice,
under two names, and nothing of Jest 30.

**Done about it:** V1's fix; re-run, the current harvest is the
thirty-seven pages of `/docs/`, whole, still labelled by date (see 8).

### 8. `versions/jest_default_is_current` -- FAIL

*with both stored, a versionless call names the current release and fetches nothing*

**What the server said:** the default version is '29.7'; expected
'2026-09-21' (the current release) over '29.7'

**What it means:** the store's ordering doing what `versions.py` says it
does: a release number outranks a harvest date, because a date means "we
could not establish a version". So once `29.7` is pinned for one project,
every versionless read of `jest` answers from 29.7 -- for a model that
learned the current docs first, a silent downgrade. Whether a date-labelled
*current* corpus should outrank an older pinned release is a question
about what the date means, and a **DECISION**: `Issues.md` V3. (Here the
two corpora are the same pages, so the answer was right by accident; see
7.)

### 9. `versions/pydantic_pinned` -- FAIL

*version='1.10': stored under that label, and confirmed by the pages, not just repeated back*

**What the server said:** 1.10 was stored but not confirmed: 'Version
'1.10' is the label you asked for, not a finding'

**What it means:** honest, and wrong. `pydantic` resolves to
`pydantic.dev/docs/validation/latest/llms.txt`, a manifest scoped to one
release; a release-named request against a *site-wide* manifest is checked
(`_scope_site_wide_llms`), but one already scoped below the root returns
early and is taken as published. So the 2.x `llms-full.txt` was stored
whole under **1.10** -- the v1 that contradicts it on every page -- with
the caveat appended. The site publishes
`/docs/validation/1.10/llms-full.txt`, 218 KB: the release asked for is
one path segment away from the URL the resolver found. Recorded as V2.

**Done about it:** `_url_for_release` in the engine: a release-named
request whose URL names another release line tries the same path under
the release asked for, once, and starts there if the site answers.
Re-run: 105 pages from `/docs/validation/1.10/llms-full.txt`, labelled
1.10, confirmed, no caveat.

### 10. `versions/pydantic_pinned_pages_are_1.10` -- FAIL

**What the server said:** 106 of the 106 pages shown under '1.10' are not
under /docs/validation/1.10/:
https://pydantic.dev/docs/validation/latest/llms-full.txt#querying-this-documentation

**What it means:** the same harvest, read back: every page is `latest`'s.
V2.

### 11. `versions/pydantic_default_is_current` -- FAIL

**What the server said:** the default version is '1.10'; expected 'latest'
(the current release) over '1.10'

**What it means:** the other half of V3. `latest` is what the site calls
its current release, and `versions.py` ranks it as a label with no
ordering, below any release number -- so with 1.10 stored, a versionless
read of pydantic returns 1.10. Here that is 2.x content mislabelled (9);
once V2 is fixed it would be the real v1, handed to every caller who did
not name a version. **DECISION**, with 8.

### 12. `cleanup/forget_django` -- FAIL

*removes both releases and says it removed two*

**What the server said:** two versions were stored but the report says:
'Deleted **django** (all versions) -- 1 version(s), 80 pages, 845,641
characters.'

**What it means:** both versions were deleted (the store held no django
afterwards); the count is the Postgres store's `delete()` returning the
`technology` rowcount -- always 1 -- where the file store returns the
number of versions removed. The tool had already listed the doomed
versions and did not use the list. `Issues.md` V4; a two-line fix.

**Done about it:** the store counts the versions before the cascade.
Re-run: "2 version(s)" on all five.

### 13-16. `cleanup/forget_poetry`, `forget_jest`, `forget_sequelize`, `forget_pydantic` -- FAIL

The same count, four more times. Each deleted both versions.

### Not issues, and things noticed

- `sequelize_current` 145 s and `sequelize_pinned` 101 s for forty pages
  each, against 25-75 s for the others: `sequelize.org` answered at about
  three seconds a page throughout. The site's pace; SLOW, not a verdict.
- `sequelize`'s current harvest is labelled **2026-09-21** although 39 of
  its 40 pages live under `/docs/v6/` and the fortieth is `/docs/v6`
  itself: the label comes from the start URL and the registry, never from
  what the pages unanimously say. Part of V1; after the fix the label
  is **v6**.
- `pydantic_current` labelled **latest**, 695 pages via `llms-full.txt` in
  2 s, ignoring the page cap: a full dump is one request, and the cap
  bounds requests. Correct.
- **Stored at the end** above lists the five versioned technologies and
  `benchmark-petstore`, all of which the cleanup suite then removed; the
  store held `click` 8.5.0 alone when the server stopped. The runner read
  the listing before cleanup, as it did in bench-1. Fixed in the runner
  after this run: the listing is re-read once the last case has run.
- `mcp_needs_token`, `mcp_rejects_wrong_token`, `scan_project_remote_dot`,
  `scan_project_foreign_path`: skipped for the reasons bench-1 gives.
