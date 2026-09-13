# Open issues

Rebuilt on **2026-09-10** from a live five-technology run, not from the previous
list. The runs are recorded verbatim in `EVALUATION-2026-09-10.log`; the
reasoning is in `AUDIT.md`. Where an entry repeats one from the old ledger it
says so, because a defect that comes back after being called fixed is a
different fact from one that was never closed.

What was asked for, exactly as a caller would ask:

| Request | Resolved to | Stored |
|---|---|---|
| Langchain, current | `docs.langchain.com` (from cache) | see `AUDIT.md` §3 |
| Langgraph, current | `github.com/langchain-ai/langgraph/tree/main/libs/langgraph` | 1 page, 6,350 chars, **complete** |
| Django 5.2 | `www.djangoproject.com` | 232 pages, 214 of them `/weblog/` |
| Tensorflow, recent | `tensorflow.github.io/rust/tensorflow` | 1 page, 11,339 chars, **complete** |
| Pytorch, LTS | `pytorch.kr` | 66 pages, Korean, labelled `lts` |

Run against 817 passing tests, 59 skipped behind opt-in gates. Every defect
below was green in that suite.

Each entry says what is wrong, how it was found, and what it would take to fix.
Entries marked **DECISION** need a human answer, not just work.

Status key: `open` · `deferred` (deliberate, revisit later) · `wontfix` ·
`fixed` (with the change that closed it, so the entry still teaches something)

---

## Configuration

### S1 — the MCP surface reads a different store than the rest of the product · **correctness** · fixed

`app.py` calls `load_dotenv()`. `mcp_server.py` does not, and neither does
`forge_tools`. An MCP client launches `python mcp_server.py`, which inherits an
environment with no `DOCSFORGE_DB` in it, so `build_store()` sees no DSN and
returns a `FileStore`. Measured in one process each way:

```
no .env (what an MCP client launches)   store().kind = files
                                        stored_name("effect") -> None
.env loaded (what app.py does)          store().kind = postgres
                                        stored_name("effect") -> "effect"
```

Over MCP, `list_knowledge_base` answered **"Nothing is stored yet"** against a
Postgres store holding 23 technologies, 703-page `effect` among them.

Three consequences, in increasing order of cost:

- `list_knowledge_base`, `read_knowledge_base` and `search_knowledge_base`
  cannot see the user's knowledge base at all.
- `learn_technology`'s "already stored?" short-circuit reads the empty store, so
  **every** technology re-harvests, every time. The one guarantee that stops a
  site being crawled twice does not hold on the surface the README calls the
  headline entry point.
- The storage chip in the UI names Postgres while the MCP surface reads a
  folder. `ARCHITECTURE.md` §7 says hiding which backend answered "would be a
  lie about the quality of the answer"; here two surfaces disagree and neither
  says so.

**The guard for this exists and cannot fire.** `build_store()` records
`degraded` and `wanted_dsn` precisely so that a silent fallback is impossible —
its comment reads *"Falling back silently means everything you ever harvested
appears to have vanished, with the interface calmly reporting an empty store."*
Both fields are only set when a DSN was **present and unreachable**. When the
DSN is simply absent, `degraded` is `""`, `wanted_dsn` is `""`, the 15-second
retry never arms, and the store reports itself healthy. Measured above: exactly
those two empty strings.

**To fix:** load the same `.env` from every entry point rather than only
`app.py` — `mcp_server.py` and the `docsforge` CLI both need it, and putting it
in `forge_tools` covers all three at once. Separately, treat "no DSN configured"
as a state worth naming: a `FileStore` that was never offered a DSN is fine, but
it should be distinguishable from one that reached for a database and missed.
Both surfaces should print which store answered on startup.

---


**Fixed**: `forge_tools._load_env()` reads `.env` in the module every surface
imports, so there is one copy and nothing to forget. It never overrides a
variable already set, so `app.py` and the suite's isolation are unaffected, and
a missing `python-dotenv` is a no-op rather than a new dependency for a core
install. `mcp_server` now also names its store on startup — `location`, never
`dsn`, which carries the password. The second half stands: a `FileStore` that
was never offered a DSN is still not distinguishable in the store object from
one that reached for a database and missed.

## Resolution

### R1 — a second strong signal outranks the project's own documentation site · **correctness** · fixed

`evidence()` orders candidates by what signals *mean* before it counts them —
forge below documentation, `docs-host` above the rest, then `own-domain`. Once
two candidates tie on all three, it falls through to a **raw count of strong
signals**, and the signals are not equally strong. A site that happens to carry
an install command, or whose repository path names the project, collects two;
the project's own documentation site typically has exactly one, `own-domain`.

Both wrong answers in this run came out of that tie-break. Measured, with the
`evidence()` key each candidate produced:

```
tensorflow
  tensorflow.github.io/rust/tensorflow  own-domain, repo-identity, names-it:14
      -> (1, 0, 1, 1, 2, 14, 0.92)   WON
  www.tensorflow.org/guide/             own-domain, names-it:17
      -> (1, 0, 1, 1, 1, 17, 0.70)
  www.tensorflow.org/                   own-domain, names-it:20
      -> (1, 0, 1, 1, 1, 20, 0.55)

pytorch
  pytorch.kr/                           own-domain, install:pypi, names-it:37
      -> (1, 0, 1, 1, 2, 37, 0.75)   WON
  pytorch.org/llms.txt/                 own-domain, names-it:129
      -> (1, 0, 1, 1, 1, 129, 0.97)
```

`www.tensorflow.org` was fetched, read and **verified**. It lost by one signal
to a page titled *"tensorflow - Rust"* whose own description reads *"This crate
provides Rust bindings for the TensorFlow machine learning library."*
`pytorch.org` lost to the Korean user group's site, which qualified because it
carries a `pip install` line.

This is the same failure `evidence()` was written to prevent — its docstring
names `github.com/langchain-ai/langchainjs/tree/main/libs/langchain/` beating
`docs.langchain.com` — reached by a route the fix does not cover. See R2.

**To fix**, in increasing blast radius: weight the signals rather than counting
them, so `own-domain` on the bare project domain is not one unit alongside
`install:`; or treat `names-it` as a magnitude rather than a tiebreak, since
129 mentions against 37 is not a close call; or add a rung above `strong` that
asks whether the host is the project's *primary* domain rather than any host
carrying its name. Each needs a measurement run — R1 is exactly where widening
recall has previously multiplied looseness.


**Fixed**: `name_authority()` grades the claim a host makes on a name — apex,
local, shared — above the strong-signal count in `evidence()`. Graded, not
gated: `is_identified` is untouched, and a mirror or a pages label still wins
when it is the only answer. `tensorflow` -> `www.tensorflow.org`, `pytorch` ->
`docs.pytorch.org/docs/stable/index.html`.

### R2 — `*.github.io` is treated as the project's own domain · fixed

`is_forge()` lists `github.com`, `gitlab.com`, `bitbucket.org`,
`sourceforge.net`. `tensorflow.github.io` is none of them, so:

- `_owns_the_name()` sees the label `tensorflow` and grants **`own-domain`**;
- `evidence()`'s first key — *a forge never outranks a docs site* — never
  engages, because the host is not a forge by that list.

`github.io` is a shared, first-come namespace: the label belongs to whoever
registered the GitHub organisation. That is the same class of claim
`_owns_the_name`'s own docstring dismisses for registries — *"one package in
one namespaced, first-come registry"* — and it is being counted as the
strongest kind of evidence.

Careful: many real projects publish on `<name>.github.io`, so refusing it
outright would lose documentation. This is about *rank*, not admission.

**To fix:** recognise the pages namespaces — `github.io`, `gitlab.io`,
`pages.dev`, `netlify.app`, `vercel.app` — and either drop them a rung in
`evidence()` below a true apex domain, or keep `own-domain` but stop the "not a
forge" key from rewarding them.


**Fixed**: `SHARED_NAMESPACES` recognises the pages namespaces, and
`name_authority` ranks them below a real apex domain. Admission is unchanged —
this was about rank, as the entry said.

### R3 — a sub-project documented on its parent's domain cannot pass the gate · fixed

`langgraph` is documented at `docs.langchain.com`. The identity gate asks
whether the *host* carries the asked-for name, and that host carries
`langchain`. So the real documentation earns no `own-domain` and no
`docs-host`, and the source repository — whose path does name it — wins by
default. Every candidate, from a cleared cache:

```
reference.langchain.com/python/langgraph/       names-it:11
    refused: only names-it:11
docs.langchain.com/oss/python/langgraph/overview  registry-agreement
    refused: only registry-agreement
github.com/langchain-ai/langgraph/tree/main/libs/langgraph
    repo-identity, names-it:10                  VERIFIED, chosen
github.com/Onelevenvy/langgraph-rust             names-it:6
    refused
```

Both real documentation sites were fetched and refused; the source tree passed.
The harvest that followed stored the README — one page, 6,350 characters — and
recorded it **complete**.

Note the second line especially: PyPI names `docs.langchain.com/...` as
langgraph's Homepage, which is `registry-agreement`, one strong signal. It
needed `names-it` to clear the gate and did not get it, because the page renders
client-side and the raw HTML does not say the word three times inside the 40,000
character window.

This is not a squatter problem and R1's remedies do not reach it. It is
structural: monorepos and umbrella projects document children on the parent's
domain, and that is the normal arrangement, not an edge case.

**To fix:** accept a docs host that the registry itself nominates. If PyPI says
langgraph's homepage is `docs.langchain.com/oss/python/langgraph/overview`, the
registry has vouched for the *URL*, and the path names the project even though
the host does not. A `path-identity` signal — the asked-for name as a whole
segment of the path on a registry-nominated URL — would close this without
touching `is_identified()`'s arithmetic. It needs a precision run: paths are
easier to satisfy than hosts.


**Fixed**: `path-identity` — a registry nominated *this URL* and the name is a
whole path segment of it. Two independent sources agreeing, which is the bar
`is_identified` has always held; refused on package hosts, where the path names
the project and the host names nobody. `langgraph` ->
`docs.langchain.com/oss/python/langgraph/overview`, 35 pages.

### R4 — resolution lands on the marketing homepage, not the docs root · fixed for the measured case

Carried over unchanged from the previous ledger, now with a measured cost.
`django` resolved to `https://www.djangoproject.com/` — *"The web framework for
perfectionists with deadlines"* — on `repo-identity, names-it:47`.
`docs.djangoproject.com` was never reached. What that cost is E1.


**Fixed for django**: `project` joined `lang` as a name suffix, so
`djangoproject.com` is Django's own domain and `docs.djangoproject.com` earns
`docs-host`, which outranks the homepage. The general risk — a project whose
docs host does not carry its name at all — is R3's territory and is only
closed where a registry nominates the URL.

### R5 — a wrong resolution is remembered for 30 days · open

All five answers, right and wrong, were filed with `rules=3` and a 30-day TTL.
The next `learn_technology("tensorflow")` returns the Rust crate with **zero
requests** and no opportunity to do better. The cache is working exactly as
designed; the point is that R1–R4 are therefore not one bad afternoon but a
month of them, and `forget_resolution` has to be run by someone who already
knows the answer is wrong.

**To fix:** nothing here, until R1–R3 are settled. Then bump `RULES`, which is
what that mechanism is for — it discards entries decided under rules the build
no longer applies. Worth deciding whether a resolution that was never
*harvested* successfully should get the full TTL.

---

## Coverage

### C1 — `whole` defaults to True when nothing established a denominator · **correctness** · fixed

`_note_coverage()` ends with:

```python
else:
    stats["whole"] = True
...
    stats.setdefault("expected", len(docs))
stats.setdefault("acquired", len(docs))
```

For any strategy other than `llms_txt` that did not set `whole` itself, the
harvest is declared complete, and `expected` is set to the number of pages that
happened to come back. `acquired == expected` is then true by construction.

Measured:

```
langgraph   pages=1  expected=1  complete=True   (strategy: github)
tensorflow  pages=1  expected=1  complete=True   (strategy: crawl)
```

Both of these are the product's core promise inverted. `PRODUCT.md` principle 3
is *"Never report unearned confidence"*; `ARCHITECTURE.md` §5 says `expected` is
*"measured against what the site says exists, not against the slice a page limit
left behind"*. That holds on the manifest and sitemap paths — django reported
232 of 991, pytorch 66 of 72, both correctly `complete=False`. It does not hold
where no denominator was ever established, and there the answer is `True`
rather than `unknown`.

The `COVERAGE UNKNOWN` branch exists in `forge_tools` and reads well. It is
close to unreachable, because `_note_coverage` never yields `None`.

**To fix:** default `whole` to `None`, not `True`, and let each strategy that
genuinely measured something say so. That makes `unknown` the resting state,
which is what the three-state design was for. Expect fallout: several currently
"complete" corpora become "unknown", which is the correct answer and will look
like a regression.


**Fixed**: `whole` is `True` only where the source states its own size —
`github`, `openapi`, `raw_text`, a manifest or a sitemap. A crawl that drained
its frontier now records `unknown` with the reason attached, because pages
nothing links to are invisible to it either way. `unknown` was always in the
design and was simply unreachable.

### C2 — the density note is computed from documents whose bodies were dropped · **correctness** · fixed

`forge_tools.py:931`:

```python
shape_note = llmsfinder.density_note([len(d.markdown) for d in docs])
```

`docs` at that point has been through `_drain()`, which returns
`Doc(doc.url, doc.title, "")` — bodies deliberately released so peak memory
stays proportional to page count rather than corpus size. That was the fix for
the old S5. Every `len(d.markdown)` is therefore `0`.

`reads_as_stubs()` fires when a corpus has at least 20 pages and a median under
1,200 characters. A median of zero satisfies that always, so **every harvest of
20 or more pages is told it is a set of stubs**:

```
django   "these 232 pages have a median of 0 characters and 232 under 500"
pytorch  "these 66 pages have a median of 0 characters and 66 under 500"
```

Against the stored rows, pytorch's real numbers are a median of **2,865**
characters with **5** pages under 500. Run on the true sizes,
`reads_as_stubs()` returns `False` and `density_note()` returns `""`.

So the note is not merely inaccurate, it is inverted: it tells a caller that
prose documentation is an API symbol index. The one signal the product has for
"this corpus is the wrong shape" currently fires on everything, which is the
same as not having it.

The tests did not catch it because they call `density_note(sizes)` with
hand-built lists, and those lists agree with the assumption the function was
written under.

**To fix:** measure at the sink, where the body is still in hand — `_StripSink`
sees every page's real length on its way to the store — or read the sizes back
from the writer's settled entry. Then pin it with a test that harvests through
a real sink rather than calling `density_note` directly.


**Fixed**: `_StripSink` keeps an integer per stored page — it is the last place
that ever sees a body — and the note is computed from those. It still fires on
a corpus that really is stubs; a test pins that, because fixing a broken signal
by removing it is not fixing it.

### C3 — a version label is applied without anything confirming it · partly fixed

`django 5.2` and `pytorch lts` are both stored under labels the caller supplied
and nothing verified.

- The django corpus is 214 weblog posts and 18 other pages from
  `www.djangoproject.com`, none of it version-scoped. It is filed as **5.2**.
- `pytorch.kr` is a community site with no LTS scoping at all. It is filed as
  **lts** — and PyTorch's LTS programme ended after 1.8.2, so the label names
  something that no longer exists.

`versions.py` is careful about *ordering* labels and `same_release()` is strict
about a release answering a request. Neither is consulted about whether the
pages actually came from the release that was asked for; `_version_label` takes
`opts.version` when it has one.

**To fix:** distinguish a *requested* label from a *confirmed* one. Where the
harvest cannot show the pages came from the named release, store it under what
was actually found and record the request separately — or refuse, which is the
house answer everywhere else. Storing an unverified label is the version
equivalent of stamping a corpus `complete`.

---


**Partly fixed**: `_urls_for_release` records `release_confirmed` when the
pages themselves name the release, and `harvest_docs` now says plainly when a
requested version is *the label you asked for, not a finding*. The label is
still applied either way, because storing a corpus under a name the caller
cannot then read it back by is its own defect. Recording confirmation in the
store — a schema change on both backends — is not done.

## Scope

### E1 — the marketing filter is a spelling-matched deny-list · fixed

**This is the old F12 returning.** F12 was recorded fixed: *"narrowing a
whole-host sitemap to its documentation: prefer `/docs`, `/guide`, `/reference`
and friends where enough exist, and otherwise drop `/blog`, `/careers`,
`/pricing` and the rest."*

Django spells it `/weblog/`.

```
_NOT_DOCS matches /blog     -> True
_NOT_DOCS matches /weblog/  -> False
```

Both halves of `_focus_on_docs` then fail together. The allow-list branch needs
five or more `/docs`-shaped URLs and finds none, because Django's documentation
is on a different host entirely. The deny-list branch does not recognise
`weblog`. So the whole weblog passes:

```
django 5.2   232 pages stored
             214 of them under /weblog/          (92%)
               8 /foundation/, 3 /conduct/, 2 /start/, 1 /screencasts/,
               1 /trademarks/, 1 /diversity/, 1 root
```

A caller who asked for Django 5.2 documentation received *"DSF member of the
month"*, *"2026 DSF Board Election Results"* and *"PyCharm & Django Fall
Fundraiser"*, filed as version 5.2 and reported only as incomplete — never as
the wrong material.

**To fix:** the deny-list is the wrong shape for this. Word-boundary matching
would catch `weblog`, but the next site will spell it `/journal/` or `/updates/`
and the list will lose again. A positive test is more robust: a page whose URL
is dated (`/2026/aug/20/`) is an article, not a manual, on every site that has
ever published one. Combine that with the existing allow-list and only fall back
to the deny-list.


**Fixed**: `looks_like_article` adds a dated-path test, which is what does not
depend on guessing the noun, and `weblog` joined the list besides. Two further
findings came out of the same investigation and are fixed with it: the filter
no longer runs at all on a dedicated documentation host — it was cutting
`docs.djangoproject.com` from 11,209 URLs to 66 — and a per-language sitemap
*index* now picks the default language, which is where the choice still
exists.

### E3 — a locale in a query parameter is not recognised as a locale · open

`_locale_of` reads a language from a leading path segment (`/ko/`) and, since
this round, from a sitemap child's filename (`sitemap-ko.xml`). Google's sites
put it in a query parameter instead, and nothing looks there.

Measured on the 2026-09-10 re-harvest of `www.tensorflow.org` — 700 pages
stored of the 705 the sitemap lists, and every one of the five that failed:

```
https://www.tensorflow.org/tfx/guide/tft?hl=ko
https://www.tensorflow.org/lite/guide/ops_select?hl=ko
https://www.tensorflow.org/lite/guide/build_cmake_arm?hl=zh-CN
https://www.tensorflow.org/learn?hl=fr
https://www.tensorflow.org/versions/r2.0/api_docs/java/reference
```

Four of five are `?hl=` translations of pages the harvest already has in
English. They are not a coverage gap — they are the same documentation twice —
but they are counted as one, so the corpus reports 700/705 when it holds
everything that matters. Harmless here; on a site that lists every translation
this way it would multiply the denominator instead of the corpus.

**To fix:** `_prefer_default_locale` should read `?hl=` alongside the path
segment, and drop non-default variants before they are counted. The curated
locale list is already the right vocabulary for it.

### E2 — federation did not discover the documentation subdomain · open

`www.djangoproject.com` links to `docs.djangoproject.com` from its primary
navigation. Federation exists exactly to notice that a technology is several
corpora, admits hosts from link evidence gathered while crawling, and marks
unselected ones `not requested`. It surfaced nothing for django: the result
carries no corpus list, and no second corpus was stored.

Not diagnosed further in this run — it needs its own investigation into whether
the link evidence was gathered, whether the candidate was refused at the
identity gate (which would make it R3 in another costume), or whether intent
deselected it silently.

**To fix:** first establish which of the three it is. The interesting case is
the second, because `docs.djangoproject.com` should pass a host-ownership test
easily, and if it did not, R3 is broader than langgraph.

---

## Providers

### P1 — the read cap assumes a window one shipped provider does not have · open

`MAX_CHARS` is 200,000 characters, and its comment is explicit about why:
*"the binding constraint is not DocsForge but the provider's context window,
and this project speaks to six of them... 200,000 characters is roughly 50-65k
tokens, which sits comfortably inside a 200k-token window."*

One of those six is Ollama, and Ollama's default context is whatever the
model's Modelfile says. Measured 2026-09-10, `qwen3.5:9b` as served locally:

```
/api/ps  ->  "context_length": 4096
MAX_CHARS ->  200,000 characters  (~50,000 tokens)
```

Twelve times the window. Ollama silently truncates the prompt, so a
`read_knowledge_base` result arrives as a fragment with no indication that it
was cut, and the model loops — one question in `measure_answers.py`'s `tools`
phase ran for twenty minutes without settling. Nothing in DocsForge says the
result did not fit, because from its side it did: the cap was honoured.

`providers/ollama.py` never asks for a larger context, and cannot easily: the
OpenAI-compatible endpoint does not carry `num_ctx`, and `OLLAMA_CONTEXT_LENGTH`
belongs to the user's daemon.

**To fix:** give the cap a per-provider budget rather than one global number —
`Provider` already knows which model it is running, and `run_tool` is the only
place that truncates. A provider that declares a small window should get a
result sized for it, and the truncation marker already exists to say so. Until
then a local-model user has to set `DOCSFORGE_MAX_CHARS` by hand, and nothing
tells them to.

## Storage

### S2 — an abandoned harvest leaves a row nobody ever clears · open

Found by querying the live store during a re-harvest on 2026-09-10:

```
technology  version      state        pages
django      5.2          harvesting     274   <- in flight, correct
langchain   2026-09-07   harvesting       0   <- abandoned days ago
langchain   2026-09-06   harvesting       0   <- abandoned days ago
mojo        1.0.0        failed           0   <- abandoned
```

Readers never see them — every read path filters on `state = 'ready'`, which is
the blue/green write doing exactly its job, and the previous version of each
technology stayed intact throughout. So this is not a correctness problem.

It is an accumulation problem. A harvest that dies with its process leaves its
row behind permanently, and nothing sweeps them: three here after a few weeks of
ordinary use, two of them for a technology that has since been harvested
successfully twice. Over a year of real use that is a table of dead versions
nobody can distinguish from the merely slow.

**To fix:** the harvest tracker already knows the difference between running and
stalled — a heartbeat that stopped — and applies it to job records. The same
judgement belongs to storage: a version in `harvesting` whose write has not
advanced for well past any plausible harvest is abandoned, and should be swept
on the next `settle` for that technology. Deleting on read would be wrong; a
harvest genuinely in flight looks identical for as long as it is in flight.

## Reporting

### T1 — the tracker reports `pages=0` for a harvest that stored a page · open

`langgraph` finished `state=done pages=0` in `/api/harvests` and in the JSONL
log, having stored one page. The counter is ticked by `_CountingFetcher` on the
crawl and manifest paths; the `github` strategy does not go through it. Cosmetic
against C1 and R3, but it is the number a watching user sees, and zero reads as
failure.

### T2 — `starting` is published twice for every job · open

Every harvest logs `phase=starting` twice — once from `reserve()` and once as
the adopted job begins. Harmless, and noise in a log whose value is that each
line means something happened.

---

## Documentation

### D1 — the stated test count is stale in three places, three different ways · fixed

```
README.md            "611 offline unit tests"
ARCHITECTURE.md      "740 passing tests"
PRODUCT.md           "757 passing offline unit tests"
measured             817 passed, 59 skipped
```

The skip count is right in both places it appears. `AUDIT.md` recorded this once
before, as F20, for `PRODUCT.md` alone; it has since drifted in two more
documents. For a project whose thesis is calibrated numbers, the numbers about
itself are the ones to keep honest.

**Fixed**: all three now read 860, the count after this round of work. It will
drift again — the durable fix is to quote it in one place, or have CI write it,
and that is still not done.

**To fix:** have CI write the count, or stop quoting it in three places and
quote it in one.

---

## What this list does not contain

The previous ledger's entries were not carried over wholesale. Several are
plainly still live — the identity gate's tolerance for a host that owns a name
(R1's ancestor), soft 404s, provisional thresholds — but they were not measured
in this run and this file is now built only from what was. The old list is at
`git show dce0f0b:"Project Development/ISSUES.md"` and is worth reading beside
this one, particularly for the entries it recorded as fixed.
