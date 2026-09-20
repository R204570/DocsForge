# Audit

**Date:** 2026-09-21. **Subject:** build `1ce0c43` (the `offline` branch),
the hosted deployment at build `5826f61`, and the week of work between
them. **Method:** the live benchmark suite run three ways — read-only
against the hosted server, and twice offline on a fresh database with a
technology harvested whole — plus the full offline test suite and a read
of every module the findings touched. The previous audit (2026-09-10) was
built from a five-technology run and is retired; every one of its findings
is closed and re-verified by a benchmark case.

---

## Status at a glance

| Area | Verdict | Evidence |
|---|---|---|
| SSRF guard | 🟢 holds, every spelling, every hop | 10 `guard/*` cases, hosted and offline |
| Bearer gate | 🟢 holds | 401 + `Bearer` challenge, hosted |
| Tool surface | 🟢 thirteen tools, identical to the checkout | `surface_matches_checkout` |
| Detection | 🟡 right for every real source; classifies garbage as `html` | 7 pass, 2 KNOWN (D1) |
| Extraction | 🟢 every kind; cap disclosed; crawl fixed this week | `fetch/*` |
| Resolution | 🟡 right on every judged name that answered; refuses honestly under a rate limit; cannot identify a scoped npm name | `resolve/*`, R7, R9 |
| Whole harvest | 🟢 `click` 38/40 in 36 s, the two missing named, version from the right registry | bench-1 `offline/*` |
| Store, offline | 🟢 every call under 1 s; search discloses partial matches | `store/*` |
| Store, hosted | 🔴 4–15 s per call: wrong region | H1 |
| Politeness | 🟢 429 waited for and stopped on; refused pages named | `test_rate_limit.py`, live on Read the Docs |
| Hosted `scan_project` | 🔴 answers about the server's disk | H2 |
| Tests | 🟢 1,076 passed, 61 skipped behind opt-in gates | full run |

## Verdict in one paragraph

The product does what its README claims on the paths that matter — a name
becomes verified documentation, stored whole, with a coverage figure a
caller can act on — and this week it stopped doing two things that were
worse than failing: storing a same-named stranger when the right page was
briefly unreadable, and describing a rate limit as an empty page. The
benchmark suite that found both is now the way this project is measured,
and it is honest about what it cannot see offline. The largest remaining
defect is not in the code: the hosted function sits on the wrong side of
the planet from its database.

---

## 1. How this audit was produced

1. `python -m scripts.benchmark.run` against `docsforge.vercel.app/mcp` on
   2026-09-19: 64 read-only cases, 58 pass, 3 fail, 4 KNOWN, 252 s.
2. Fixes for what it found (C4, S4, R11), with regression tests.
3. `python -m scripts.benchmark.run --offline --reset` on 2026-09-20, twice:
   the second on a cold cache while Read the Docs was throttling this
   address, which is what exposed R9 and C5.
4. Fixes for those, with regression tests; X2 in passing.
5. `--offline --reset --publish 1` on 2026-09-21: `benchmarks/bench-1/`.
6. `pytest tests` with the store variables set empty: 1,076 passed.

Every number below is from one of those runs and is reproducible from the
JSON beside it.

## 2. The runs, measured

| Run | Mode | Build | Pass | Fail | Error | Known | Skip | Time |
|---|---|---|---|---|---|---|---|---|
| 2026-09-19 | hosted, read-only | `5826f61` | 58 | 3 | 0 | 4 | 0 | 252 s |
| 2026-09-20 #1 | offline, `--reset` | `daa0f5d` + fixes | 63 | 7 | 0 | 2 | 4 | 328 s |
| 2026-09-20 #2 | offline, `--reset`, RTD throttling | same | 61 | 8 | 1 | 2 | 4 | 278 s |
| 2026-09-20 #3 | offline, `httpx` whole | same + R9/C5 | 65 | 5 | 0 | 2 | 4 | 355 s |
| **bench-1** | offline, `--reset`, published | `1ce0c43` | **68** | **1** | **1** | **2** | **4** | **307 s** |

The seven fails of run #1 were: three wrong expectations in the suite
itself (pydantic moved its docs; ecosystem labels; a case-insensitive
"harvested"), one Windows-only guard gap (X2), one hosted-only disclosure
asserted offline, and two real (`click` flagged for two unextractable index
pages — honest — and the click corpus itself). Run #2's eight were the rate
limit: the harvest reported as empty, and `click` and `requests` resolved
to strangers. bench-1's one fail is R7 and its one error is N1.

## 3. What works, and should not be lost while fixing the rest

- **The identity gate's refusals.** `unknown_name_is_honest` never invents;
  `@tanstack/react-query` is refused rather than guessed; under a 429 the
  refusal names the page it could not read. Every wrong answer in this
  project's history has been a *confident* one; the gate's bias toward
  refusal is the product's main asset.
- **Coverage is never rounded up.** `click` 38/40 is `INCOMPLETE` with the
  two pages named. `httpx` 22/22 is whole. A drained crawl is `unknown`.
- **The guard.** Every private-address spelling, both redirect targets, a
  DNS name that resolves to loopback, `file://`, and path traversal in
  `save_docs` — refused, hosted and offline, in under a second.
- **The surface cannot drift.** Generated from one list; compared live.
- **Honesty in the answer.** Truncation, unreadable pages, refused pages,
  the registry a version came from, a partial search match, the serverless
  limitation — all in the text the model reads.
- **The offline runner's isolation.** Every variable set before the process
  starts, a non-loopback database refused outright, nothing written to the
  shared store by any automated path this week.

## 4. What does not work

### H1 — hosted latency 🔴

`list_knowledge_base` 9.4 s, `read_knowledge_base` 4–9 s,
`search_knowledge_base` 12–15 s, six concurrent listings 23 s; the same calls
offline in 0.1–0.8 s. The function runs in `iad1`, the database in
Singapore; `X-Vercel-Id` and the database's address say so. A model waiting
15 s for a search is a model that stops using the tool. One line in
`vercel.json`; the operator's decision.

### H2 — `scan_project` hosted 🔴

Fluent, wrong, and leaks `/var/task`. Two KNOWN cases stand against it.

### R7 — scoped npm names 🟠

A **DECISION** about the gate, with a real proposal in `Issues.md`.

### D1, N1, R8 — small, each with a one-paragraph fix 🟡

Garbage classified as `html`; a connection reset not retried once; an
unknown name spending 53 s to say so.

### The carried entries, not re-measured 🟡

E2, E3, S2, P1, T1, T2 from the 2026-09-10 register. None was in the path
of this round's runs. They stand until a bench measures them.

## 5. What the suite did not catch, and why

- **R9 needed a cold cache and a rate-limited site at the same time.** The
  first two offline runs had `click` cached from the resolve suite and Read
  the Docs answering; only `--reset` on a throttled address produced it.
  `--reset` now clears the caches with the database, so every published
  bench resolves from nothing.
- **C4 needed a trailing-slash directory URL.** `learn_technology("click")`
  starts from the site root, where links are absolute; only a `fetch_docs`
  of `/en/stable/` crawled through relative links. The benchmark has both.
- **Nothing offline can measure the hosted store's latency, the bearer
  gate, or the hosted `scan_project`.** Those cases skip offline with the
  reason, and run against the hosted server read-only. A bench is offline
  by policy; the hosted run is a check, not a bench.

## 6. What to do next, in order

1. **H1** — move the function to `sin1`. One line; the largest measurable
   improvement available.
2. **D1** — guard before detecting. One line; two KNOWN cases go FIXED.
3. **N1** — one retry on a connection reset.
4. **H2** — refuse `scan_project` over a hosted connection, and never echo
   the server's cwd.
5. **R7** — decide, then either implement the scope-qualified match or
   record the refusal as intended and retire the benchmark expectation.
6. **R8** — early exit for names nobody knows.
7. Re-measure the carried entries in a bench that targets them (a
   `tensorflow.org` locale case for E3; a `django` federation case for E2).

## 7. Reproducing this

```
git checkout offline-testing
DOCSFORGE_DB="" DATABASE_URL="" pytest tests               # 1,076 passed
python -m scripts.benchmark.run --offline --reset          # bench-1's run, unpublished
python -m scripts.benchmark.run                            # the hosted read-only check
```

The hosted numbers depend on the day's Vercel and Aiven; the offline
numbers depend on the sites the suite fetches (petstore3.swagger.io,
svelte.dev, docs.python.org, click.palletsprojects.com, httpbin.org, and the
registries). Read the Docs rate-limits an address after three whole
harvests of one of its sites in an hour; set `DOCSFORGE_OFFLINE_WHOLE` to
another technology rather than waiting it out.
