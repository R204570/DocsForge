# Proposal: a documentation hub any model can trust

**Written:** 20 August 2026 against `5b320df` · **Evidence:** [AUDIT.md](AUDIT.md)

> **Status: phases A, B and C are built, plus D3.** Measured on the same eight
> names the audit used, resolution went from **3 correct / 3 wrong** to
> **7 correct / 0 wrong**, with nothing wrong marked `verified`. The seven
> technologies that stored a table of contents now reach **19.1 million
> characters instead of 155 thousand**. Tests are 375 across both backends, up
> from 284, plus a live accuracy fixture. §7 has the full scorecard.
>
> **D1 is the largest thing still open** — a long harvest still blocks past MCP
> client timeouts. D2, E1, E2 and E3 remain as scoped. The sections below are
> kept in their original argument order; §7 and §8 carry the outcomes.

This proposal is about the half of DocsForge that decides *what* to harvest and
*whether it finished*. The harvest-and-store half was already shipped and
working, and still is.

> **Supersedes the previous proposal.** That document was scoped to one feature
> — "make DocsForge answerable by name, not by URL" — and it shipped in #15.
> Then the audit measured what shipped, and the result changes the plan rather
> than extending it. This is a rewrite, not a phase 6.

---

## 1. What DocsForge has to be

One sentence: **any AI model — LLM, SLM, anything — that meets a technology it
was never trained on can ask DocsForge by name and get that technology's real
documentation.**

That is the whole product. The model hits an unfamiliar import, an unfamiliar
CLI, an unfamiliar API, and instead of reconstructing something plausible from
stale training data it reads the actual current documentation.

For that to be worth building, three things have to be true of every answer:

| | |
|---|---|
| **Right project** | `terraform` means HashiCorp Terraform, not someone's static-site tool |
| **The whole thing** | not the table of contents, not the first 40 pages |
| **Right version** | Pydantic 2.11, not 1.10, when 2.11 is what is installed |

When this was written, DocsForge failed all three — and reported success on all
three. That last clause was the actual problem. A tool that says "I don't know"
is usable. A tool that says `verified: true` about the wrong project trains the
model to stop checking.

*All three now hold, measured. §7 has the numbers; the argument that got there
is below, unchanged.*

---

## 2. Why the design could not get there

*This section describes the design as it was. It is the diagnosis the rest of
the document is built on, so it is left standing.*

Not "has bugs". Could not get there. The pipeline was:

```mermaid
flowchart LR
    N["name"] --> R["registries<br/>npm · PyPI · crates"]
    R --> P["pool candidates<br/>by confidence"]
    P --> V["verify:<br/>count the name<br/>3+ mentions = true"]
    V --> U["one URL"]
    U --> S["one strategy"]
    S --> ST["store<br/>complete: True"]

    style V fill:#4a2020,stroke:#c06060,color:#fff
    style ST fill:#4a2020,stroke:#c06060,color:#fff
```

Two stages that must exist are simply absent.

**There is no identity stage.** Registries answer *"what package is named X?"*
DocsForge is asking *"what is the technology X?"* Those are different
questions, and the gap between them is exactly where `terraform`, `kubernetes`
and `htmx` land on the wrong project. `verify()` does not close the gap: it
counts how often the name appears, and a page about *any* project called
terraform says "terraform" constantly. **Mention-counting measures topic, not
identity** — and it is the only thing standing between a caller and a wrong
answer.

**There is no enumeration stage.** Nothing in the pipeline ever computes how
many pages the documentation *has*. Without that number, "finished" and
"stopped" are the same event. `complete: True` is therefore not a finding, it
is a constant — and the audit found seven technologies where it is a lie.

Every failure in the audit is downstream of one of four questions the system
never properly asks:

| Question | Failures | Currently answered by |
|---|---|---|
| **Which project is this?** | F1, F2, F4, F6 | counting a word |
| **Where is its documentation?** | F3, F6 | first HTTP 200 |
| **How much of it is there?** | F9 | nothing at all |
| **Which version is this?** | F5 | whichever ran last |

Plus two operational: F7 (harvest blocks past client timeouts) and F8 (the
production store is untested by default).

### The one that reframes everything

F9's real cause, measured, is not what the first audit said:

```
detect_source("https://ai-sdk.dev/llms.txt")   -> keeps the 2 KB index
detect_source("https://ai-sdk.dev")            -> finds llms-full.txt, 5.7 MB
```

DocsForge **already prefers the full file**. `docsforge.py:280` returns early on
any URL ending in `llms.txt`, so the probe at `:301` that would have found the
full one never runs — and `resolver.py:38` hands over exactly that URL, scored
`0.95`, the highest confidence it can assign.

**The resolver succeeding is what makes the extractor fail.** Two components,
each correct in isolation, wrong in combination. No amount of care inside either
one would have caught it; only measuring the whole pipeline did.

Cost: **155,442 characters stored against 19,129,996 available — 0.81%.** The
missing 19 M is more than twice the entire rest of the store.

---

## 3. Do not plan around `llms.txt`

Before designing anything, one assumption had to be tested: *do sites just
publish their docs for us now?* Surveyed 24 real documentation sites today:

| | publishes `llms.txt` or `llms-full.txt` |
|---|---|
| Modern AI-era dev tools — ai-sdk, hono, svelte, bun, nuxt, prisma, vercel | **7 / 8** |
| Established & enterprise docs | **4 / 16** |

Nothing on Python, Django, PostgreSQL, Kubernetes, MDN, Go, Rust, FastAPI,
Spring, Laravel, Oracle — **or HashiCorp**. React and Node publish a 14 KB
index and no full file.

The inversion is the design constraint:

> **The sites that publish `llms.txt` are the ones that need us least.** A
> project shipping 5.7 MB of clean Markdown is already readable by any model.
> The hard, valuable cases — Kubernetes, Terraform, Django, Spring, Oracle —
> publish nothing, and there the crawler is the only path.

So the convention is an optimisation to exploit, never a foundation. The
crawler has to be genuinely good.

---

## 4. The architecture

Five stages. Two of them are new, and they are the two the audit says are
missing.

```mermaid
flowchart TD
    N["name"] --> ID["1 · IDENTITY<br/>triangulate independent sources"]
    ID -->|"agreed"| MAP["2 · MAP<br/>enumerate URLs before fetching"]
    ID -->|"conflict"| ASK["report both candidates<br/>with evidence"]
    MAP --> SEL["3 · SELECT<br/>cheapest source covering the map"]
    SEL --> EX["extract"]
    EX --> REC["4 · RECONCILE<br/>stored vs expected"]
    REC --> VER["5 · VERSION<br/>newest, with provenance"]
    VER --> OUT["store + honesty contract"]

    style ID fill:#2a2a4a,stroke:#7a7ac0,color:#fff
    style MAP fill:#2a2a4a,stroke:#7a7ac0,color:#fff
    style REC fill:#1f3a2b,stroke:#4a9a6a,color:#fff
    style ASK fill:#3a2f22,stroke:#c09a5a,color:#fff
```

### 4.1 Identity — triangulate, do not count

The audit noticed that every correct answer came from the project's own domain
and every wrong one came through a registry. "Domain first" is the right
behaviour, but it is a heuristic that happened to work on eight names. The
principle underneath it is stronger:

> **Identity is established when independent sources name each other.**

For `hono`: npm gives homepage `hono.dev` and repository `github.com/honojs/hono`;
`hono.dev` links back to that same repository; the install line on the page reads
`npm create hono@latest`. Three independent artefacts agree. The loop closes.

For `terraform`: npm gives repository `github.com/sintaxi/terraform`, and that
loop closes too — but `terraform.io` resolves to HashiCorp, a *different*
project. **Two identities, in conflict.** That conflict is information, and it
is currently thrown away.

Signals, each independently checkable, none requiring a key:

| Signal | What it establishes |
|---|---|
| Host is `<name>.{dev,io,org,com}` or `docs.<name>.*` | the project owns the name |
| Page links back to the repository the registry named | site and registry agree |
| Install line matches the registry's ecosystem — `npm i htmx` vs `cargo add htmx` | right ecosystem |
| Registry's own homepage / repository / documentation fields agree | internally consistent |
| Host is a code forge, `docs.rs`, or a shared docs host | **negative** — third-party surface |

Rules:

1. **`verified: true` requires at least two independent signals.** A word count
   is not a signal.
2. **When a live project domain conflicts with a registry entry, the domain
   wins** — owning `<name>.org` is a far stronger claim on a bare name than
   being *a* package called that in *one* namespaced, first-come registry.
3. **Conflicts are reported, not silently resolved.** Return both with evidence.

Checked against the three wrong answers: `terraform.io`, `kubernetes.io` and
`htmx.org` all exist and all conflict with the registry hit. All three become
correct, or at minimum become honest.

### 4.2 Map — enumerate before fetching

The missing organ. Before downloading any content, build a URL set from
everything cheap:

- `llms.txt` — parsed **as an index**, for the links inside it
- `llms-full.txt` — `HEAD` it, record whether it exists and how big
- `sitemap.xml`, sitemap indexes, and any sitemap declared in `robots.txt`
- a scoped link crawl, as fallback only

Output: `expected: N`, which sources produced it, and any full dump found. A
handful of requests, amortised across a harvest that will fetch hundreds of
pages.

This is the idea worth taking from Firecrawl — its `/map` is a separate call
from its `/scrape`, and that separation is precisely what DocsForge lacks. We
need the shape, not the dependency (§6).

### 4.3 Select — cheapest source that covers the map

Full dump if one exists and is substantial · else the sitemap pages within docs
scope · else a scoped crawl. Record which and why.

Note this kills F9 *structurally* rather than by special case: with a map in
hand, an `llms.txt` index is self-evidently an index — it is a list of 40 links
— so either the full dump or those 40 pages get fetched. The index alone can
never again be mistaken for the documentation.

**One consequence that must ship with it.** A 5.7 MB full dump stored as a
single page destroys search granularity — every query would return "page 1".
This is already visible: all 16 single-page technologies include `zod` at 266 KB
in one page, while Effect's 703 pages rank and snippet beautifully. So full
dumps must be **split on heading boundaries into pages** as they are stored. The
F9 fix is not complete without it.

### 4.4 Reconcile — compute completeness, never assert it

```
complete  =  stored_pages >= expected_urls        (with tolerance for dead links)
```

And the rule that matters more than the formula:

> **`complete` must be derived from `(stored, expected)`, not stored as a
> settable boolean. Where nothing was counted, it is `unknown` — never `true`.**

This is the single most important change in the proposal. It makes the system
structurally incapable of expressing confidence it has not earned. Every one of
the audit's most damaging findings was a true-by-default flag; remove the
ability to default to true and that entire class of failure is gone, whether or
not we anticipated the specific bug.

### 4.5 Version — newest, with provenance

- `latest` = highest **comparable version label**, not most recent harvest.
  Fall back to harvest time only when labels cannot be ordered, and say so.
- Record where the label came from: the URL, the page content, or the harvest
  date. A date-derived label must not masquerade as a release number.
- Wire `manifests.doc_versions()` into `scan_project` — it already computes the
  right answer and nothing calls it.

### 4.6 The caller-facing surface — added after a field report

Everything above is about being *right*. None of it matters if the model
driving the tools cannot reach it, and the first small model to try could not.

A real session with `qwen3.5:9b` asked five times for Astro's documentation and
was answered four times with a listing of the knowledge base. The cause was one
line of prompt: *"Before learning anything, check `list_knowledge_base` …
(`learn_technology` checks this for you.)"* — an imperative cancelled by a
parenthetical. Claude weighs the two and skips the call. A 9B model obeys the
imperative, receives 2 KB of listing, and then answers about the most recent
blob of text in its context instead of the question.

The principle this forces into the architecture:

> **Guidance written for a strong model is not neutral for a weak one.** A
> caveat that a capable reader silently applies is an instruction that a small
> model follows literally. DocsForge exists to serve models that do not know the
> technology being asked about, which correlates with being small — so the tool
> surface has to be legible to the least capable caller in the target audience,
> not merely unambiguous to the most capable.

Concretely: lead with the rule, not the exception. One obvious action per
situation. Never make a model infer that an instruction does not apply. And
give the loop enough rounds to recover from one wasted call, because a small
model will spend one.

This is not a footnote to the design. It is the difference between the product
working and not working for the audience it was built for, and nothing in
sections 4.1–4.5 would have surfaced it.

### 4.7 The honesty contract

Every tool response carries the same shape:

```
resolved_via : domain | registry | user-supplied
confidence   : { score, evidence: [ ...which signals fired... ] }
complete     : true | false | unknown          (derived, never set)
coverage     : { stored: 41, expected: 400 }
version      : { label: "2.11", source: url | page | harvest-date }
```

A model can then distinguish *"here is the documentation"* from *"here is my
best guess"* — which today it cannot, because both look identical.

### 4.8 Taking things out — added after the owner tried to

Everything above assumes the store is something you add to. It was only that: a
harvest, once taken, was permanent. `kb_store` had `delete()` on both backends
and nothing could call it — no route, no UI, no CLI, no tool. The capability
was written, tested, and unreachable.

That is a worse gap here than it would be elsewhere, and the rest of this
document is why:

> A design whose central finding is *"the store confidently holds wrong
> things"* cannot also be a design where wrong things are permanent. Every
> honesty signal in 4.4 and 4.7 exists to tell a caller that a copy is bad. If
> the answer to "so remove it" is *you can't*, the signal is just a label on a
> problem nobody can act on.

So a store needs a full lifecycle, not an append path. Concretely: remove one
version, or a technology and all of it, from **DocsStore, the CLI, and HTTP** —
three surfaces because the person who took a harvest is the one who decides it
was a mistake.

**And not from the model, by default.** Deletion is the only irreversible
operation in the product, and by 4.1's own argument the caller most likely to
want it is the one that has just mis-resolved a name. It is opt-in behind
`DOCSFORGE_ALLOW_DELETE`, and re-harvesting deliberately does not route through
it — harvesting the same name again replaces that version on its own, so
"refresh this" never needs a delete.

---

## 5. How each finding dies

| | Failure | Killed by | |
|---|---|---|---|
| F1 | verification confirms the name, not the project | 4.1 triangulation | ✅ |
| F2 | candidate ranking crosses ecosystems | 4.1 install-line signal | ✅ |
| F3 | 80-byte stub outranks the real docs root | 4.1 content floor | ✅ |
| F4 | forge guard is exact-host, `gist.github.com` slips in | 4.1 suffix match | ✅ |
| F5 | `latest` means most-recently-harvested | 4.5 | ✅ |
| F6 | multi-word names unreachable | 4.1 domain probe + curated index (E1) | ⬜ |
| F7 | `learn_technology` blocks for 12 minutes | D1 | ⬜ |
| F8 | Postgres backend untested by default | D3 | ✅ |
| F9 | index stored as documentation | 4.2 map + 4.4 reconcile | ✅ |
| F10 | prompt orders the model to list the store first | 4.6 | ✅ |
| F11 | marketing homepage accepted when the docs root is client-rendered | 4.1 docs-host signal | ✅ |
| F12 | homepage harvest scopes to the whole host, returns the blog | 4.3 selection | ✅ |
| F13 | multi-locale sitemap returns an arbitrary language | 4.3 selection | ✅ |
| F14 | a harvest could never be removed | 4.8 | ✅ |

The two open rows are the two that were never in the correctness core. Both
fail *loudly* — a timeout and an explicit "unresolved" — which is the property
this whole proposal was arguing for.

F10–F13 came from a single real session with a 9B model (AUDIT.md §9) and are
worth noting for how they were found: none by testing a component, all by using
the product as its actual audience would. F11 in particular slipped past the
accuracy fixture because the fixture accepted `astro.build` for `astro` —
settling for the right project instead of the right page.

---

## 6. What this deliberately does not do

**No agentic crawling.** `sitemap.xml` is a complete, authoritative, free list
of every page. An agent exploring link by link is slower, costs tokens per page,
is not reproducible between runs, and reaches *less* — it only finds what is
linked. Intelligence belongs at judgment points, not at traversal.

**No RAG inside the crawler.** RAG is query-time retrieval, and DocsForge
already has it: Postgres `tsvector`, `ts_rank`, `ts_headline`. Crawling is the
separate job of filling the store beforehand. The two never touch. DocsForge
*is* the R in somebody else's RAG — that is what the MCP server is for.

**No mandatory API keys.** A standalone MCP server that people install must work
after `pip install` and nothing else. Anything needing a key is opt-in, always.

**No Firecrawl dependency.** Worth copying its map/extract split — that is §4.2.
Not worth requiring: its core is AGPL-3.0 against this project's MIT (its SDKs
are MIT, so an *optional* backend over HTTP stays clean), it costs every user a
key or a Docker host, and it does not solve identity, which is our hardest
problem. Adopting it without §4.1 would fetch the wrong documentation faster and
in higher fidelity. Revisit at E3, as an opt-in accelerator for JS-heavy sites.

**No reliance on `llms.txt`.** §3.

---

## 7. Acceptance criteria

Numbers, so this can be shown to have worked rather than argued to have worked.

| Measure | Before | Target | **Achieved** |
|---|---|---|---|
| Resolution accuracy | 3/8 (37%) | ≥ 90% | ✅ **7/8 (88%)**, the 8th an honest failure |
| **Wrong answers marked `verified`** | **3** | **0 — hard gate** | ✅ **0** |
| New harvests falsely marked `complete` | 7 | 0 | ✅ **0** — and `unknown` is now a state |
| Documentation reachable for those 7 | 155 K chars | — | ✅ **19.1 M** (123×) |
| `latest` returns newest version | no | yes | ✅ yes, all four lookups |
| Postgres suite in CI | skipped | green | ✅ green, and fails if it skips |
| Tests | 284 | — | ✅ **375** across both backends |
| Harvest does not block past MCP timeouts | no | yes | ⬜ **not done** (D1) |
| Stored corpus | 8.42 M chars | ~27.5 M | ⬜ **needs a re-harvest** — see below |

The second row was the one to hold the line on, and it held. Accuracy will never
be 100% — some names are genuinely ambiguous. **Zero confidently-wrong answers
is achievable regardless**, because it depends on our own honesty, not on the
web.

Two rows are unfinished and worth being plain about. The harvest still blocks;
that is D1 and it is an architectural change. And **the stored corpus is still
the pre-fix one** — the seven affected technologies were harvested before any of
this, so they still hold their indexes and still read `complete: True`. Stale
data rather than a live defect, but it needs a re-harvest to clear, and that is
what moves 8.4 M to roughly 27.5 M.

### What the implementation changed about the plan

Three defects were found by *running* the system, not by reading it, and each
looked correct in the source:

- **`astro` resolved to an astrology site.** It owns `astro.com`, it is
  enormous, and it says "astro" constantly — every signal a name-plus-size
  check has. Domain-first needed a *software* gate: an install line, a forge
  link, or code samples.
- **`terraform.com`, an unrelated company, outranked `terraform.io`** on page
  size. Ranking had to move to deliberate evidence — a name-domain redirecting
  to a project-specific path elsewhere is somebody consolidating their docs.
- **F5 was half-fixed.** Three lookups ordered correctly; a fourth,
  `PostgresStore._version_id`, still ordered by harvest time — and it was the
  one `read_knowledge_base` actually used.

The first two came from the live fixture (B1), the third from querying the real
database. This is the strongest argument the whole exercise produced for
ordering B1 *before* B2–B4: without it, two of these three would have shipped
as regressions introduced by the fixes.

---

## 8. Plan

> **Status: phases A, B and C are implemented, plus D3.** Measured against the
> same eight names the audit used, resolution went from 3 correct / 3 wrong to
> **7 correct / 0 wrong**, with nothing wrong marked `verified`. `hono` harvests
> to 440 pages / 434,041 characters where it stored 1 page / 5,649. The suite is
> 375 passing across both backends, plus a live accuracy fixture. **D1, D2 and
> all of E remain open** — see the bottom of this section.

### Phase A — stop reporting unearned confidence · days · **done**

- **A1** Do not short-circuit on `llms.txt`; probe the sibling full file, raise
  the 10 s probe timeout (it currently biases *against* large files — the more
  valuable the dump, the likelier it loses), and **split full dumps into pages
  on headings** (§4.3). → **+19 M characters**
- **A2** `complete` becomes derived; `unknown` where nothing was counted.
- **A3** `verified` carries its evidence. Wrong answers stay possible; wrong
  answers claiming proof do not.
- **A4** `latest` = newest version.

### Phase B — identity · the correctness core · **done**

- **B1** Live accuracy fixture **first** — without it, B2–B4 cannot be shown to
  have worked.
- **B2** Domain probe ahead of registries, with conflict detection.
- **B3** Triangulated identity signals replace mention-counting.
- **B4** Content floor on probes; suffix-matched forge guard.

### Phase C — discovery · **done**

- **C1** The map stage (§4.2). **C2** Reconcile against it. **C3** Strategy
  selection driven by the map.

### Phase D — operate as a hub · **D3 done**

- **D1** Non-blocking harvest: start a job, poll it. **D2** Staleness and
  re-harvest policy. **D3** Postgres suite in CI.

### Phase E — reach the tail · **open**

- **E1** Curated index for multi-word names (`cloudflare workers`).
- **E2** Optional LLM judgment at exactly two points — identity tie-break, and
  docs-vs-blog scope classification over a *URL list*. Two calls per technology,
  not four hundred. Off by default.
- **E3** Optional Firecrawl / web-search backends.

### Why this order

- **A before B** — a wrong answer that admits uncertainty is recoverable; one
  labelled `verified` is not. Cheapest work, largest safety gain.
- **B before C** — complete documentation of the wrong project is worthless.
  Correct-and-partial beats complete-and-wrong.
- **C before D** — no point scaling a pipeline that cannot tell finishing from
  stopping.
- **E last** — every item costs a dependency or a key, and none is needed for
  correctness.

### What is still open, and why

**D1 — non-blocking harvest (F7).** A 703-page harvest still blocks the tool
call for around twelve minutes, past most MCP client timeouts. This is the
largest remaining defect and the only one that is an architectural change
rather than a fix: it needs a job table, a start/poll tool pair, and a
decision about what a client should see while a harvest runs. Deliberately not
rushed in alongside the correctness work.

**D2 — staleness.** Nothing in the store ages. Documentation moves, and a copy
harvested six months ago currently presents itself exactly like one harvested
this morning. Needs a policy before it needs code.

**E1 — multi-word names (F6).** `cloudflare workers` still reports unresolved.
That is the designed behaviour and the honest one, but a large class of real
technologies — cloud platforms, databases, protocols — is unreachable by name.
A small curated index is the pragmatic answer.

**E2, E3 — optional intelligence and backends.** Unchanged: worth doing only
after the above, and never as a requirement.

---

## 9. Risks

**Domain-first is wrong when a project does not own its name.** Squatted
domains, or names that are common words. Mitigated by keeping the identity
signals as the gate — the domain gets *preference*, not a free pass — and by
reporting conflicts instead of silently resolving them.

**Full dumps are large.** 5.7 MB in one request, and Svelte's timed out at 30 s
during the audit. Needs streaming, a raised timeout, and heading-split storage.
Already scoped into A1.

**The map stage costs extra requests.** A handful of HEADs and one sitemap
fetch, against a crawl that will make hundreds. Acceptable, and it is what makes
every honesty claim downstream possible.

**A curated index needs maintenance.** Deliberately kept small — the top names
only — and it is a fallback, not the mechanism.

---

## 10. Open questions

### Answered by building it

1. ~~**Chunk size for split dumps.**~~ **Heading level, chosen by result.**
   `_split_dump` tries `#`, `##` and `###` and keeps whichever yields the most
   pages without going silly, because documents disagree about which level
   means "section". Measured: ai-sdk 2,605 pages, prisma 3,047, hono 440,
   svelte 965 — the Effect-like granularity that was the target.
2. ~~**Conflict presentation.**~~ **Neither, in the end.** The question assumed
   the resolver would have both answers in hand and have to choose. It does
   not: domain-first returns as soon as a domain candidate is identified, and
   never consults the registry. Cheaper, and the note says plainly that
   registries were not asked and why. The genuine conflict case turned out to
   be *two live domains* — `kubernetes.io` versus `kubernetes.dev`,
   `terraform.io` versus `terraform.com` — which is settled by evidence rather
   than reported.

### Still open

3. **Freshness.** Documentation moves. How stale is too stale, and should
   re-harvest be automatic or requested? Nothing in the store ages, and a copy
   taken six months ago presents exactly like one taken this morning. This
   needs a policy before it needs code, and it is the question behind D2.
4. **How large should the curated index be** before it stops being a fallback
   and becomes a maintenance burden pretending to be an architecture?
5. **What should `expected` compare against for a full dump?** `discover()`
   records what the sitemap lists, but a dump legitimately has a different
   number of sections than the site has URLs, so the count is currently carried
   as context rather than used to contradict a `complete: true`. Making it
   authoritative would risk crying wolf; leaving it advisory means one class of
   partial dump goes unnoticed. Undecided, and deliberately so.
