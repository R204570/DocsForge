# Issues

The register. Rebuilt on **2026-09-21** from the live benchmark suite
(`scripts/benchmark/`, published as `benchmarks/bench-1/`), the hosted
read-only run of 2026-09-19 against build `5826f61`, and the independent
evaluation of 2026-09-16 (`Evaluation.md`, since retired into this file).
Entries carried from the 2026-09-10 register keep their ids and say whether
they were re-measured.

Each entry says what is wrong, how it was found, and what it would take to
fix. Entries marked **DECISION** need a human answer, not just work. Fixed
entries stay, with the change that closed them, so the entry still teaches
something — and because a defect that comes back after being called fixed
is a different fact from one that was never closed.

Status: `open` · `mitigated` · `deferred` · **DECISION** · `fixed (commit)`.
A benchmark case that covers the entry is named where one exists; a KNOWN
verdict in a bench cites the id here.

---

## Resolution

### R5 — a wrong resolution is remembered for 30 days · mitigated

Carried. Every answer is filed with the `RULES` revision that decided it and
discarded on recall if the revision changed, so a fix reaches the cache
instead of being outlived by it; a refusal reached without reading the
candidates, or for want of examination (R9), is never filed. What remains:
a *successful* wrong answer decided under unchanged rules still lives its 30
days, and `forget_resolution` has to be run by someone who already knows.
**To fix:** worth deciding whether a resolution that was never harvested
successfully should get the full TTL.

### R6 — the version label came from the first registry to answer · fixed (`d5ef8df`, 2026-09-17)

`click` resolved to click.palletsprojects.com via PyPI and was stored as
`0.1.0`, npm's `dist-tags.latest` for an unrelated package of the same name;
`markdownify` likewise. Found live 2026-09-16 (`Evaluation.md` §2.3). The
release is now the winning candidate's own, scoped to its registry, and a
candidate is judged against its own registry's facts rather than the pool's.
bench-1 `offline/whole_corpus_stored` asserts `click` is filed under `8.x`.

### R7 — a scoped npm name cannot pass the identity gate · **DECISION**

`find_docs("@tanstack/react-query")` refuses: npm nominates
`tanstack.com/query` and `github.com/TanStack/query`, the pages write
"TanStack Query" and "React Query", and `registry-agreement` is one strong
signal where two are required. Measured hosted and offline, every run;
bench-1 issue 4. The gate is doing what `Design.md` §1 says. Accepting the
unscoped tail (`react-query`) or the scope (`tanstack`) as the name would
resolve every scoped package — and let `@anyone/click` be identified by a
page about `click`. **The decision:** whether a scope-qualified match on the
registry's *own* nominated homepage counts as identification, given the
registry already binds the scoped name to that URL.

### R8 — a name nobody knows walks the whole ladder · open

`zzqx-no-such-package-9182` took 53 s to refuse (bench-1 issue 5): own-domain
guesses, three registries, name shapes, evidence, search, to the 40-request
budget. The bound was respected; it is just spent on a name whose answer was
known after the second lap. **To fix:** when no registry knows the name and
no own-domain probe answered at all, skip the shape and search laps unless
the name has more than one word (the case they exist for).

### R9 — a candidate that could not be read let a weaker one win · fixed (`a8d27a9`, 2026-09-20)

With click.palletsprojects.com answering HTTP 429, `click` resolved to
`github.com/databricks/click/wiki` — a Kubernetes CLI — and was harvested and
stored under `click`; `requests` resolved to `docs.rs/requests`, a Rust
crate. Both verified, because both document a project of that name. Found
by the offline benchmark on a cold cache. A registry-nominated candidate
whose fetch failed for a transient reason (429, 5xx, no answer) is now
*unexamined*; nothing weaker wins in its place, not another registry's
same-named project, not the ladder's tail, not the same project's code
host; the refusal names the page and is not cached. Four regression tests
in `tests/test_resolver.py`.

### R10 — hostname ownership plus repeated mentions is the one path a name-squatter satisfies · **DECISION**

Carried, not re-measured this round. `flask` reached a to-do app at
flask.io and `polars` a third-party site on `own-domain` + mentions.
Closing it means raising the gate — requiring a structural signal
(`install:`, `repo-backlink`, `repo-identity`) beside ownership — which
would also refuse some genuine sites that publish nothing but prose.

### R11 — evidence links included stylesheets, decoded nothing, and matched "api" inside "googleapis" · fixed (`a8d27a9`)

`find_docs("@tanstack/react-query")` offered
`fonts.googleapis.com/css2?family=…&amp;display=swap` and a sponsor's
`/api/checkout` as documentation candidates at confidence 0.60. Anchors only
now, entities decoded, "looks like documentation" judged by the URL's own
words with a never-documentation list (`checkout`, `login`, …).

---

## Detection

### D1 — `detect_source_type` classifies garbage as `html` · open (`Evaluation.md` §2.6)

`""` and `"not a url"` both answer `html`. Detection's origin probes are
refused by the guard for a URL with no scheme or host, and detection
swallows that and falls through to its default. `fetch_docs` on the same
input is refused correctly, so nothing is fetched. Benchmark cases
`detect/empty_url_is_an_error` and `detect/garbage_is_an_error` are KNOWN
against this entry. **To fix:** call `Fetcher.guard` on the URL before
detection begins; one line, two cases go FIXED.

---

## Harvesting and coverage

### C3 — a version label is applied without anything confirming it · partly fixed

Carried. The harvest now says when the label is the registry's current
release rather than something the pages declared, and when it is the
request repeated back. What remains is a site that documents several
releases on one host with no version in the path.

### C4 — the crawl resolved relative links against the normalised URL · fixed (`afd64f1`, 2026-09-19)

`fetch_docs(url="…/en/stable/", crawl=True, max_pages=3)` returned one page
after 25 s: `/en/stable` (slash stripped) made `quickstart/` into
`/en/quickstart`, and every one of 38 links 404'd. Found by the hosted
benchmark's `fetch/crawl_three_pages`. Links resolve against where the page
landed, or its `<base href>`. `tests/test_crawl_base.py`.

### C5 — a rate-limited page was reported as one with nothing on it · fixed (`afd64f1`, 2026-09-20)

Thirty-three HTTP 429s from Read the Docs were filed as "reached but not
extractable — nothing on them read like documentation", a five-page corpus
stored as COVERAGE UNKNOWN, and no pause taken. `Retry-After` is honoured
(30 s cap, two retries), three refusals in a row stop the harvest, refused
pages are listed as refused, and the coverage reason says "the site's pace,
not its size". `tests/test_rate_limit.py`.

### E2 — federation did not discover the documentation subdomain · open

Carried, not re-measured. `www.djangoproject.com` links
`docs.djangoproject.com` from its primary navigation and no second corpus
was surfaced. Needs its own investigation: was the link evidence gathered,
refused at the gate, or deselected by intent.

### E3 — a locale in a query parameter is not recognised as a locale · open

Carried, not re-measured. `?hl=ko` translations counted as coverage gaps
(700 of 705 on tensorflow.org). **To fix:** `_prefer_default_locale` reads
`?hl=` beside the path segment.

---

## Search and reading

### S2 — an abandoned harvest leaves a row nobody ever clears · open

Carried, not re-measured. Offline, `--reset` clears everything; hosted, a
row for a harvest the instance died under stays until something notices.

### S3 — an OpenAPI operation is not searchable by its operationId · open

bench-1: `search_knowledge_base("findByStatus", technology="benchmark-petstore")`
answers "Nothing stored … matches". The rendered spec puts the operation in
a table cell under an endpoint heading; sectioning is by `h2`/`h3` and the
cell is not a section. An honest miss, and the case passes as one, but a
spec's operationIds are exactly what a model asks by. **To fix:** render
each operation under its own heading, or index table cells as sections.

### S4 — the any-word search fallback posed as a match · fixed (`cbda7f2`, 2026-09-19)

Ten ranked passages about injection tokens for a query no page contained.
The header now names the words none of the passages contain.
`tests/test_passages.py`.

---

## Network

### N1 — a connection reset ends the fetch; nothing retries · open

`raw.githubusercontent.com` reset the connection on the fourth of
twenty-five `docs/` files in three of four offline runs on this network
(bench-1 issue 3), never from Vercel's egress. The benchmark judges it
ERROR, correctly. One retry after a short pause, for `ConnectionResetError`
and `RemoteDisconnected` only, would have finished the run and costs
nothing when it does not happen. Not for timeouts, which already took the
full wait.

---

## Hosted

### H1 — the function runs in `iad1`; the database is in Singapore · **DECISION**

Every store-backed call is 8–30 round trips at ~250 ms each: hosted
`list_knowledge_base` ~9 s, `read_` 4–9 s, `search_` 12–15 s; the same calls
offline are under 1 s. Measured 2026-09-19 (`X-Vercel-Id: bom1::iad1`;
database at 168.144.251.227). `"regions": ["sin1"]` in `vercel.json` moves
the function next to the database; Hobby allows one region. A change to
the production deployment, so the operator's call.

### H2 — `scan_project` over a hosted connection scans the server · open (`Evaluation.md` §2.5)

`path="."` reports the deployment's own 19 dependencies as the caller's;
an absolute client path fails with `Not a directory: /var/task/C:\…`,
leaking the working directory. Over a remote transport the tool cannot see
the caller's disk and should say so. Benchmark cases
`store/scan_project_remote_dot` and `_foreign_path` are KNOWN, hosted
only; offline, `scan_this_repo` shows the tool working where the disk is
shared. **To fix:** refuse when the process is hosted (`harvest_jobs.EPHEMERAL`
or a request that arrived over the network) unless the path is under the
server's own root, and never echo the server's cwd.

### H3 — `/connect` reflects `Host` / `X-Forwarded-Host` into a script literal · open (`Evaluation.md` §2.2)

Only when `DOCSFORGE_PUBLIC_URL` is unset, which the self-hosted
`main.py --http` and Containerfile paths allow. The hosted deployment sets
it. **To fix:** JSON-escape the substitution and accept only a hostname
grammar from the headers.

### H4 — page content reaches the reasoning prompt unvalidated · open, off by default (`Evaluation.md` §2.4)

`DOCSFORGE_REASONING=on` feeds up to 4,000 characters of a scraped page into
a model asked, among other things, whether a new host documents the
project. Bounded (12 calls per harvest, veto-only for most decisions), but
the admission question runs against attacker-controlled text.

### H5 — two processes can mint the same harvest id · open, low

`_new_id` scans the on-disk records and the ledger under a lock that is
per-process. Two serverless invocations for the same name at the same
instant can collide, and one status record overwrites the other.

### H6 — a hosted harvest past the deadline is lost · by design

Vercel runs nothing after the response. `harvest_jobs.EPHEMERAL` makes the
answer say so, and `harvest_status` discloses it on every call. Large
harvests are done offline against the same database.

---

## Security (closed)

### X1 — the SSRF guard did not see redirect hops · fixed (`daa0f5d`, 2026-09-17)

`302 Location: http://169.254.169.254/…` walked past the guard that refused
the address directly (`Evaluation.md` §2.1). `Fetcher.get` follows redirects
itself and guards every hop; the browser path intercepts every request.
Benchmark `guard/redirect_to_loopback` and `_metadata`, every run.

### X2 — the guard relied on the platform resolver for `inet_aton` spellings · fixed (`afd64f1`, 2026-09-20)

`2130706433` and `0177.0.0.1` were refused on Linux because glibc resolves
them, and on Windows cost a 16-second DNS wait before failing to resolve.
The guard reads the spellings itself.

---

## Providers and UI

### P1 — the read cap assumes a window one shipped provider does not have · open

Carried. `DOCSFORGE_MAX_CHARS` is tunable per operator; the panel's
smallest provider still overflows on a full `read_knowledge_base`.

### T1 / T2 — the tracker reports `pages=0` for a one-page harvest; `starting` is published twice · open

Carried, not re-measured.

### U1 — `<input>` is allowed by the sanitiser with no attributes · open, cosmetic (`Evaluation.md` §2.8)

Task-list checkboxes render inert. Not a vulnerability.

---

## Retired

- **S1** (the MCP surface read a different store), **R1–R4**, **C1–C2**,
  **E1**, **F1–F9** of the 2026-09-10 audit: fixed then, and the fixes
  have held through every run since. Their lessons are in `Design.md`.
