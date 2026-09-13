# Resolved

**2026-09-10.** What the five-technology evaluation found, what was changed to
fix it, and what each fix was measured against. The evaluation itself is in
`EVALUATION-2026-09-10.log`; the findings are in `AUDIT.md`, filed as `ISSUES.md`
entries. This file is the other half: what is no longer true of the code.

Two things were asked for, in this order — name-to-URL resolution first, then
harvests stopping short — and neither was to be patched around. Both are fixed
at the mechanism rather than at the symptom, and the work turned up four more
defects on the way, three of which are also fixed.

Every number below was measured, not estimated. Tests went **817 → 864**
offline, and **901 with a Postgres test database**, which had never been
stood up on this machine before — see round two.

---

## The five, before and after

| Asked for | Before | After |
|---|---|---|
| langchain | `docs.langchain.com` — 627 pages in **813 requests, 751s, 186 lost** | same URL — 3,372 pages in **1 request, 3s, 0 lost** |
| langgraph | `github.com/langchain-ai/langgraph/…` — **1 page**, a README, stamped complete | `docs.langchain.com/oss/python/langgraph/overview` — **35 pages**, scoped to LangGraph |
| django 5.2 | `www.djangoproject.com` — 232 pages, **214 of them `/weblog/`** | `docs.djangoproject.com` — **653 URLs under `/en/5.2/`**, the 5.2 manual |
| tensorflow | `tensorflow.github.io/rust/tensorflow` — **the Rust bindings**, 1 page, stamped complete | `www.tensorflow.org` |
| pytorch | `pytorch.kr` — the **Korean user group**, 66 pages of Korean | `docs.pytorch.org/docs/stable/index.html` — 3,438 pages |

Across a 27-name regression set, cold cache, judged by hand:
**19 of 25 → 23 of 25 correct. Four fixed, none broken.**

---

## Part 1 — name to URL

### What was actually wrong

Not one bug. Three, and only the third is the one it looked like.

**`own-domain` was a single bit doing three different jobs.** It was set by
`tensorflow.org` (the project), by `tensorflow.github.io` (whoever registered
that GitHub organisation), and by `pytorch.kr` (a national user group). One bit
cannot rank three things, so ranking fell through to *counting strong signals* —
and there a rustdoc page with an extra `repo-identity` outscored
`www.tensorflow.org`, which had been fetched and verified. Both wrong answers
lost by one unit of arithmetic to a page that is not the project's documentation.

**The identity gate could not see a sub-project.** It asks whether the *host*
carries the name. LangGraph documents itself on its parent's host, so
`docs.langchain.com/oss/python/langgraph/overview` earned neither `own-domain`
nor `docs-host` and was refused with *"only registry-agreement"* — while the
source tree passed on `repo-identity`. Both real documentation sites were
fetched and refused; the README won.

**A published `llms.txt` was assumed to be about documentation.**
`pytorch.org/llms.txt` is 8 KB under a `## Posts` heading — conference
announcements, newsletters, venues, organisers. Taking it as the docs root also
stopped `/docs/` ever being probed, because that loop breaks on a 0.95.

### What changed

**`name_authority()` — the claim a host makes on a name, graded rather than
counted.** `resolver.py`

```
apex    the project's own domain on a TLD the world publishes on, or a host
        its own domain deliberately redirected onto (terraform.io ->
        developer.hashicorp.com)
local   the same name on a country code — a national community, admissible,
        never above the project's own domain
shared  the name as a first-come label under github.io and friends
```

Graded, **not gated**: every candidate still passes `is_identified` on exactly
the evidence it did before, and a local mirror or a `github.io` page still wins
when it is the only answer. This decides which of two *already believed*
answers is better, which was the actual failure. `is_identified`'s bar is
untouched — PROPOSAL-II Invariant 1 stands.

Read off the candidate alone, deliberately: taking the name as a second
argument meant only `verify` could compute it, and `evidence` would then read a
field that anything building a candidate by hand leaves at zero. The test suite
caught that within a minute of my writing it.

**`path-identity` — a registry-nominated URL whose path names the project.**
Two independent things must hold: a registry was asked where this package
documents itself and answered *this URL*, **and** the name is a whole path
segment of it. Either alone is worth nothing — a nomination alone is already
`registry-agreement`, and a name in a path alone is what makes
`github.com/sintaxi/terraform` look like Terraform. Refused on package hosts,
where the path names the project and the host names nobody.

**`project` joins `lang` as a name suffix.** `djangoproject.com` is Django's own
domain. Refusing it that claim is why `docs.djangoproject.com` earned no
`docs-host`. The list stays two entries long — `mojoportal.org` is still refused.

**An `llms.txt` of articles is not a documentation root.** Threshold measured
against the manifests actually in use, not picked:

```
article  docsy  links  manifest
   0.52   0.00     60  pytorch.org          <- the newsroom
   0.14   0.00     14  prisma.io
   0.08   0.20     71  pydantic.dev
   0.03   0.00    176  docs.langchain.com
   0.01   0.94    101  mojolang.org
   0.00   0.57      7  svelte.dev
```

The second column is why the obvious test is the wrong one: asking whether a
manifest links to `/docs/` paths rejects Prisma and LangChain, which are
already *on* a documentation host. 0.3 sits in the middle of a 3.7× gap.

### The cache, and the tripwire

`RULES` 3 → 4, so every wrong answer cached on 2026-09-10 under a 30-day TTL is
discarded on recall instead of being served until October.

`test_rules_is_bumped_when_the_decision_logic_changes` demanded that bump before
I thought to make it. It also had a hole I had just widened: it fingerprints a
function's own source, not its callees, so decision logic moved *into*
`name_authority` would have left it silent. `DECIDERS` now also covers
`name_authority`, `_path_identity`, `probe_docs_root` and
`_indexes_only_articles`.

### Deliberately not changed

- **`is_identified`'s bar.** Raising it is a decision about a stated invariant,
  not something to slip into a fix.
- **`flask` → flask.io and `polars` → polars.dev.** Both still wrong, both
  unchanged, both squarely R1's remaining case: a domain that genuinely owns
  the word and repeats it. Recorded, not quietly claimed.

---

## Part 2 — harvests stopping short

### The root cause was one line, and it was self-inflicted

`docs.langchain.com/llms-full.txt` is **6,749,200 characters of documentation
in a single request** — rung 1 of the acquisition ladder, "stored whole".

`classify_llms_shape` returned `index` for it. Not because of its density —
that measured 0.21 against a 0.50 threshold — but because of this:

```python
if re.search(r"llms-(full|medium)\.txt", body, re.I) or ...:
    return "index"
```

The file mentions `llms-full.txt` **twice**, both times pointing at its own
Python and TypeScript sub-corpora. The rule exists so that a *stub* naming a
fuller file sends us to the fuller file; applied to that fuller file it is
self-defeating. So 6.7 MB already in hand was discarded, 2,616 links were
extracted from the prose, 813 were re-fetched as HTML, and 186 of those failed.

**That is the "unexpected stop": 12.5 minutes and a 23% loss, re-acquiring what
one request had already returned whole.**

### What changed

**The filename is the publisher's own statement.** A file published as
`llms-full.txt` *is* the full text — that is what the convention means, and it
is the same principle that puts `llms.txt` above a sitemap. A stub that merely
calls itself full is still caught, because a body that really is almost all
links is an index whatever it is named.

**Density is measured on manifest lines, not on lines containing a link.** A
link buried in a sentence is a cross-reference. Counting the whole line it sits
on read the LangChain dump as 20.7% links; counting lines that *begin* with a
link reads it as 4.3%.

**`_split_dump` is wired in.** It was written for this, tested, and called from
nowhere but its own tests — the same dead-symbol pattern the ledger records as
W2/W3/W6. Its first test says why it exists: *"5.7 MB stored as one page is
unsearchable: every query matches page 1, and ranking has nothing to choose
between."* One request now yields 3,372 titled pages.

**The split level is chosen by page size, not heading count.** "Whichever
yields the most pages" counts headings, and a heading count says nothing about
what is under one:

```
level   headings   median span
#           2,528          179     per-page titles, nearly empty
##          3,371        1,066     the document's real sections
###         2,081          961
```

I wrote that rule from a mean I had computed in my head and asserted 2,670 for
`#`. The real median is 179. Measuring it is what caught that, and the comment
in the code now carries the measured table.

### A regression I introduced, and caught

Making the dump a `dump` meant it had no links — and `_pathway_for_latest` read
"no links" as "makes no checkable claim", so a **scoped** request took the
site-wide file as published. Asking for `langgraph` returned **3,372 pages of
LangChain, LangSmith and Fleet** as LangGraph's documentation: the exact
broadening that ARCHITECTURE §4 forbids, introduced by my own fix.

A dump *does* make a checkable claim. The LangChain file carries **1,175
`Source:` lines**, one per page. `_dump_under` now narrows a dump to the pages
it says came from under the requested prefix, and refuses it outright when it
covers none of them. Measured: the root file states sources under
`/build-overview` and `/langsmith/` and publishes the Python corpus separately,
so it covers nothing under `/oss/python/langgraph/` — it is refused, the
sitemap answers instead, and LangGraph is 35 pages of LangGraph.

Where a dump states **no** sources the original reasoning stands untouched, and
its two tests still pass: refusing on a suspicion nothing supports would trade a
whole published corpus for a crawl. What changed is only that the claim is now
checked wherever the file makes one.

### Three more, found while verifying Django

**A per-language sitemap index returned Greek.** `docs.djangoproject.com/sitemap.xml`
is twelve per-language children; `sitemap-el.xml` sorts first, so a capped
harvest filled up on 1,077 Greek URLs and never opened `sitemap-en.xml`. The
per-URL locale filter — the old F13 fix — then saw one language and had nothing
to choose between. The same preference now applies to the index's children,
where the choice still exists. `sitemap-1.xml` and `sitemap-posts.xml` are not
languages and keep their whole listing.

**The marketing filter was running on a documentation host.**
`docs.djangoproject.com` publishes 11,209 English URLs and `_focus_on_docs` cut
them to **66** — `/topics/`, `/howto/`, `/ref/` and `/intro/` are not on its
docs-shaped list, and `/releases/`, 4,898 pages, is on its not-docs one. A
request for Django 5.2 came back with three pages. On a host that is entirely
documentation there is no marketing to drop; the filter was written for
`astro.build`, and that is still exactly where it applies.

**`/blog` was on the deny-list and Django spells it `/weblog/`.** Naming the
noun loses to the next site that says `/journal/`. A dated path is an article on
every site that has ever published one, so `looks_like_article` now tests for
that too — one vocabulary, shared with the resolver's newsroom check rather than
duplicated beside it.

**A named release scoped only the label, never the sitemap.** The manifest path
has honoured a named release since `_links_for_release`; the sitemap path never
did, so `django 5.2` harvested `/en/5.0/`, `/en/4.2/` and `/en/dev/` together and
filed the mixture as **5.2** — the same page under four releases, under exactly
the right name. `_urls_for_release` now narrows it, and matches at the
shallowest depth any URL manages: Django files the 5.2 manual at `/en/5.2/…`
and its 5.2 release notes at `/en/dev/releases/5.2/`, and only the first is the
5.2 documentation.

---

## Part 3 — the MCP surface read a different store

Not one of the two asked for, and the largest finding in the audit, so it is
fixed too.

`app.py` called `load_dotenv()` and nothing else did. An MCP client launches
`python mcp_server.py` with the ambient environment, so `DOCSFORGE_DB` was
unset, `build_store()` returned a `FileStore`, and:

```
list_knowledge_base()   ->  "Nothing is stored yet."      (23 technologies in Postgres)
read_knowledge_base(…)  ->  failed for all five           (just harvested through MCP)
```

The guard against exactly this could not fire: `build_store` records `degraded`
and `wanted_dsn` so a silent fallback is impossible, and sets both only when a
DSN was present and *unreachable* — never when it was simply absent.

`.env` is now read by `forge_tools`, the module every surface imports, so there
is one copy and nothing to forget. It never overrides a variable already set, so
`app.py` is unaffected and the suite's isolation still holds. `python-dotenv`
ships with the web extra and the MCP server is a core install, so a missing
package is a no-op rather than a new dependency.

The MCP server also now names its store on startup — `location`, never `dsn`,
because the DSN carries the password:

```
DocsForge: knowledge base = postgres (127.0.0.1:5432/DocsForge)
```

---

## Part 4 — the corpus shape note

`_drain` returns `Doc(url, title, "")` so peak memory stays proportional to page
count. The shape note was measured over those emptied documents, so every median
was zero and **every harvest of twenty pages or more** was told it was "the
shape of an API symbol index or a split dump, not prose documentation".
LangChain's 627 pages averaging 14,283 characters reported *"a median of 0
characters and 627 of them are under 500"*.

`_StripSink` now keeps an integer per stored page — it is the last place that
ever sees a body. The note is computed from those. It still fires on a corpus
that really is stubs; a test pins that, because fixing a broken signal by
removing it is not fixing it.

---

## What is still open

Nothing here claims more than it did.

- **`whole` still defaults to `True`** where no denominator was established
  (`AUDIT.md` F5). The 1-page `complete` corpora are gone because their
  resolutions are fixed, not because the coverage default is. Making `unknown`
  the resting state flips existing corpora from `complete` and is the owner's
  call.
- **Federation still did not surface `docs.djangoproject.com`** as a corpus of
  `www.djangoproject.com` (F9). Django is fixed by resolving to the docs host,
  which routes around the question rather than answering it.
- **A version label is still applied unverified** (F8) for sites that do not
  version their paths. Django 5.2 is now genuinely 5.2 because the sitemap says
  so; `pytorch lts` would still be filed as asked.
- **`flask` and `polars`** remain wrong, as above.
- **The stated test counts** in README, ARCHITECTURE and PRODUCT are still
  stale, now in a fourth way: 855.

---

## Reproducing

```bash
python -m pytest tests/ -q          # 855 passed, 59 skipped
```

Resolution, cold, over the 27-name set — the harness sets a throwaway
`DOCSFORGE_RESOLVE_CACHE`, so it never reads or writes the real one:

```bash
python measure_resolution.py before.json
python measure_resolution.py --diff before.json after.json
```

The two resolution diagnostics at the end of `EVALUATION-2026-09-10.log` print
every candidate for a name with its signals and the reason it was accepted or
refused. They are resolution only — no harvest, no store write — and they are
the cheapest way to re-check this work after a change.

Every live verification in this file ran into a throwaway `FileStore`. The
configured Postgres store still holds the corpora from the original evaluation,
which were harvested under the old resolutions: `tensorflow` is still the Rust
bindings, `langgraph` still a README, `django 5.2` still the weblog. Re-running
the five would replace them.

---

# Round two — the proof, and what it found

Asked for after the round above: *"make it 100% complete, i don't want half
cooked system."* The gap named was the one this project had never closed —
nothing measured whether a harvested corpus actually changes what a model can
answer. `AUDIT.md` had it as M3, "correctness is judged by hand · deferred", and
`PRODUCT.md` still called the whole build experimental for that reason.

It is measured now, and measuring it found the largest defect in the product.

## The benchmark

`measure_answers.py`. Twelve questions across six technologies, each answerable
in one exact identifier, asked three ways:

| phase | what it isolates |
|---|---|
| `closed` | the model alone, no tools, no documentation — the wall |
| `passages` | the corpus retrieved and put in front of it — does the *documentation* clear the wall? |
| `tools` | the model given DocsForge's read tools and left to use them — does the *product* clear it? |

Two things keep it honest. Grading is a string match against an exact
identifier, not one model judging another. And **every expected answer is
checked against the stored corpus before a single question is asked** — a
fixture whose answer is not in the documentation is testing nothing, so the
harness refuses to run rather than report a number nobody should believe.

Subject: `llama3.2` (3B) served locally by Ollama. A small model is the right
subject — the wall is unambiguous for it.

## The result

```
phase             all   unknown tech   known tech
closed           0/12            0/7          0/5
passages         8/12            6/7          2/5
tools            1/12            1/7          0/5     llama3.2 (3B)
tools             3/4            3/4            -     qwen3.5 (9B), 4 questions
```

**The wall is real, and it is worse than not knowing.** Closed-book, the model
got none of the twelve — and only *once* said so. The other eleven answers were
confident inventions:

```
want LlmAgent         got agent_base       want gin.Default     got gin.NewEngine(...)
want google-adk       got agentkit         want ShouldBindJSON  got Next
want SequentialAgent  got AgentRunner      want model_validate  got hydrate
want Effect.gen       got useEffect        want comptime        got eval
```

`useEffect` for the Effect library is the tell: not a gap, a plausible
substitution from a neighbouring ecosystem. This is exactly the failure the
product exists to prevent, and now there is a number for it.

**The documentation clears it.** 0/7 to 6/7 on technologies the model
demonstrably did not know. That is the thesis, and it now has evidence behind it
rather than an intention.

Reported strictly: `Effect.succeed` is graded a failure because the model
answered `succeed` — the right name without its namespace. Call that one right
and it is 9/12. The grader is not being loosened after the fact; the number
above is the strict one.

## What the benchmark found: retrieval could not answer a question

The first run scored **0/12 on `passages`** — with the documentation retrieved
and handed over. That should be impossible, and chasing it found this:

```
search_knowledge_base("In Google's Agent Development Kit (ADK), what is the
                       exact name of the class used to build a simple
                       LLM-backed agent?")

  ->  Nothing stored in google-adk matches ...
      The technology may not be harvested yet - try learn_technology(name=...).
```

Against a 1,799-page corpus that certainly contains `LlmAgent`.

`websearch_to_tsquery` **conjoins**: every significant word of the query must
appear in one page or section. That is right for a phrase and wrong for a
question — and a question is how a model asks. The file store had the same
defect for the same reason, matching the whole query as one substring.

The second-order damage is worse than the first. Told nothing matched, the tool
advises `learn_technology` — so a model following DocsForge's own advice
re-harvests a site it already has. That is not hypothetical: given the full tool
set, `qwen3.5:9b` called `learn_technology` fifteen times in one run and began
crawling live sites. Nothing was damaged, because the "already stored"
short-circuit caught each one — the short-circuit that only works now that every
surface reads `.env` — but the product was steering a model into re-harvesting
because its own search could not find what it already had.

**Fixed** in both backends, precision first: ask for every term, and only if
that finds nothing, ask for any of them, ranked. Postgres does it by putting the
query through the same analyser the index used and OR-ing the lexemes it
produces; the file store falls back to `passages.score`, which is the relevance
the read path already uses — search asking a different question of the same
corpus than reading does is how the two drift apart. No query that worked before
changes its answer.

Measured after, on questions whose retrieval had returned nothing at all:

```
google-adk   want LlmAgent         found in retrieved passages: True
google-adk   want SequentialAgent  found in retrieved passages: True
effect       want Effect.gen       found in retrieved passages: True
mojo         want comptime         found in retrieved passages: True
```

That single fix is what moved `passages` from 0/12 to 8/12.

## What the benchmark did not clear: driving the tools

`tools` scored 1/12. The cause is not retrieval — the same corpus scores 8/12
when handed over. `llama3.2` (3B) does not make tool calls at all; it writes
what a tool call looks like into its answer, naming tools that do not exist:

```
{"name": "create_agent", "parameters": {"namespace": "google-adk"}}
{"name": "sendFinishSignal", "parameters": {"technology": "google-adk"}}
{"name": "runEffect", "parameters": {"k": "effect"}}
```

That is a model limitation rather than a DocsForge defect, and splitting the
phases is what makes it possible to say so rather than guess. Confirmed by
running the same phase on a model that *can* call tools — `qwen3.5:9b`, on the
four `google-adk` questions:

```
model                tools
llama3.2  (3B)        1/12
qwen3.5   (9B)         3/4      same questions, same corpus, same tools
```

So the product's own path — a model asking DocsForge for documentation it has
never seen, and answering correctly from what comes back — works end to end.
That is the whole claim, and it is now measured rather than intended.

It carries one consequence worth stating plainly: **on a model too small to
call tools, the corpus works and the tool loop does not**, so the surface that
reaches such a model is the one that puts passages in front of it rather than
waiting to be asked. Nothing in DocsForge does that today, and the `passages`
column is the evidence that it would be worth doing.

A related defect turned up while chasing it and is filed as `ISSUES.md` P1:
`MAX_CHARS` is 200,000 characters against a stated assumption of a 200k-token
window, and Ollama — one of the six shipped providers — serves `qwen3.5:9b` with
a **4,096-token** context. Twelve times over. Ollama truncates silently, so a
`read_knowledge_base` result arrives as a fragment with nothing saying it was
cut. The cap needs a per-provider budget; it has one global number.

## Coverage: `unknown` is reachable at last

`AUDIT.md` F5. `whole` defaulted to `True` for any strategy that set nothing, so
completeness was definitionally true for a one-page harvest — `expected 1,
acquired 1, complete True`, which is how a 6,350-character README and a single
rustdoc page came to be the most confident rows in the store.

`True` now requires that something stated the size: a manifest, a sitemap, a
repository enumerated through the API, or a single published artifact. **A crawl
that drains its frontier records `unknown`**, with the reason attached — it
reached everything linked inside its scope, and a page nothing links to is
invisible to it either way. The three-state design always had `unknown`; it was
simply unreachable.

## A version label is a finding or it is a caveat

`AUDIT.md` F8. `_urls_for_release` now records `release_confirmed` when the
pages themselves name the release, and `harvest_docs` says so plainly when they
do not: *"Version 'lts' is the label you asked for, not a finding."* The label
is still applied, because storing a corpus under a name the caller cannot read
it back by is its own defect. Recording confirmation in the store is a schema
change on both backends and is not done.

## The Postgres half of the suite now runs

CI stands a database up; a developer machine did not, so the tests covering the
backend that actually ships — schema migration, the `tsvector` search, the
`ts_headline` snippets, the COPY bulk load — were skipped locally. They are the
tests the search fix above most needed.

```
offline only                 864 passed,  59 skipped
DOCSFORGE_TEST_DB set        901 passed,  22 skipped
```

The remaining 22 are live-network and browser-rendering gates.

## Documentation brought back to the build

`README.md`, `ARCHITECTURE.md` and `PRODUCT.md` each stated a different, stale
test count (611 / 740 / 757). All three now read the measured number, and
`ISSUES.md` D1 is closed with the note that quoting it in three places is the
actual defect. README's known-limits and PRODUCT's evidence section no longer
describe as live the failures that are fixed, and no longer omit the ones that
are not.

## What is still open, honestly

- **`flask` and `polars`** still resolve to the wrong project. Same remaining
  case: a domain that genuinely owns the word and repeats it. Closing it means
  raising the identity gate — a decision about a stated invariant.
- **The tool loop on small models**, and **P1**, the read cap against a
  provider's real window.
- **Federation** still did not surface `docs.djangoproject.com` as a corpus of
  `www.djangoproject.com`; django is fixed by resolving to the docs host, which
  routes around the question rather than answering it.
- **`release_confirmed` is reported, not stored.**
- The benchmark is **twelve questions on one 3B model**. It is enough to show
  the wall is real and that documentation clears it. It is not enough to claim
  a rate, and nothing here does.
