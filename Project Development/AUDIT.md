# DocsForge — audit

**2026-09-10.** Five technologies, asked for the way a caller asks, driven
through the MCP stdio surface into the configured Postgres store. Nothing here
is inferred from reading the code alone: every claim below is either a line in
`EVALUATION-2026-09-10.log` or a query against the store that run produced.
Where this file and the code disagree, the code is right and this file is stale.

Open defects are filed in `ISSUES.md`; this document is the evidence and the
reasoning behind them.

---

## Status at a glance

| | |
|---|---|
| Technologies requested | 5 |
| Corpora that are the right documentation | **1** — langchain |
| Corpora that are the wrong project or wrong material | **4** |
| Corpora stamped `complete` that are a single page | **2** |
| Tests passing | 817, 59 skipped — every defect below was green |
| Crashes, hangs, lost harvests | none |
| Harvests visible from the process that started them | all 5 |
| Harvests readable back through the MCP surface | **0 of 5** |

---

## Verdict in one paragraph

The machinery works and the judgement does not. Five concurrent uncapped
harvests ran for thirteen minutes across five hosts without a crash, a hang, or
a lost page; the cross-process handoff, the streaming writer, the progress
tracker, the failed-URL disclosure and the politeness delay all behaved exactly
as `ARCHITECTURE.md` describes. What failed is everything that decides *what to
fetch* and *what to claim about it*. Four of the five requests resolved to
something other than the project's documentation — the Rust bindings for
TensorFlow, a Korean community mirror for PyTorch, a monorepo README for
LangGraph, and Django's weblog for Django — and two of those were stored with
`complete = True`. Meanwhile the one surface that a client actually attaches to,
the MCP server, cannot see the knowledge base at all: it wrote five corpora into
Postgres during this run and could not read a single one of them back. The
product's engineering is in better shape than its accuracy, and its accuracy is
in better shape than its self-knowledge.

---

## 1. How this audit was produced

The five requests, verbatim, as `learn_technology` calls with `max_pages=0`
(uncapped):

```
learn_technology(name="langchain")
learn_technology(name="langgraph")
learn_technology(name="django", version="5.2")
learn_technology(name="tensorflow")
learn_technology(name="pytorch", version="LTS")
```

Driven over **MCP stdio**, with `mcp_server.py` launched the way a client
launches it — inheriting the ambient environment, no curated variables. On
stdio the server sets `DETACHED = True`, so each harvest was handed to the
long-lived `app.py` on `127.0.0.1:8000`, which is the process holding
`DOCSFORGE_DB`. That is the intended production arrangement, and it is why the
writes reached Postgres even though the MCP process could not see it (F1).

Ground truth for every number below is the store itself, queried directly, not
the tool's own summary of what it did.

---

## 2. The five runs, measured

```
tech        version      pages   characters  complete  strategy        expected
langchain   2026-09-10     627    8,956,185    False   llms-full.txt        813
langgraph   2026-09-10       1        6,350     True   github                 1
django      5.2            232      695,273    False   sitemap              991
tensorflow  2026-09-10       1       11,339     True   crawl                  1
pytorch     lts             66      357,492    False   sitemap               72
```

And what each of those sources actually is:

| Asked for | Resolved to | What that page is |
|---|---|---|
| langchain | `docs.langchain.com` | **correct** — the documentation |
| langgraph | `github.com/langchain-ai/langgraph/tree/main/libs/langgraph` | the source tree; one README stored |
| django 5.2 | `www.djangoproject.com` | the marketing site; 214 of 232 stored pages are `/weblog/` |
| tensorflow | `tensorflow.github.io/rust/tensorflow` | *"tensorflow - Rust"* — "Rust bindings for the TensorFlow machine learning library" |
| pytorch LTS | `pytorch.kr` | the Korean PyTorch user group; 66 pages of Korean |

One of five is the documentation the caller asked for.

---

## 3. What works, and should not be lost while fixing the rest

These were exercised hard and held.

- **The cross-process handoff.** Five harvests were started from a per-turn MCP
  subprocess and every one of them survived it, ran to completion in `app.py`,
  and published progress the launcher could read. `/api/harvests` reported all
  five with live phase and page counts. This is the newest machinery in the
  project and it did its job.
- **Concurrency and politeness.** Five uncapped harvests against five hosts ran
  simultaneously for 13 minutes with no host refusing us and no rate-limit
  error in the log.
- **Streaming storage.** 8.9 MB of langchain went to Postgres a page at a time.
  Peak memory stayed flat. Nothing was held to the end.
- **In-flight invisibility.** While langchain was harvesting, the store reported
  27 technologies and `entry("langchain")` returned `None`. A partial corpus is
  not readable as a whole one, exactly as designed.
- **Honest incompleteness, where a denominator existed.** langchain reported
  *"627 of 813, 186 could not be acquired"*; pytorch *"66 of 72, 6
  unextractable"*, and listed all six URLs. django listed 759. This is the
  product's best feature and it is genuinely good.
- **The JSONL log.** 6,695 lines across the run: every phase change for every
  job, one line per request, per trace event, per error. When the first attempt
  found no server on :8000, it said so in plain words rather than failing
  quietly.
- **The suite.** 817 passing, 59 skipped, 64 seconds. Green throughout.

---

## 4. What does not work

### F1 — the MCP surface cannot see the knowledge base 🔴

`ISSUES.md` S1.

`app.py` calls `load_dotenv()`. `mcp_server.py` does not. An MCP client launches
`python mcp_server.py` with no `DOCSFORGE_DB` in its environment, so
`build_store()` finds no DSN and returns a `FileStore`.

At the start of this run, against a Postgres store holding 23 technologies:

```
list_knowledge_base()  ->  "Nothing is stored yet."
```

At the end of it, having just harvested five technologies *through this same
surface*, all five of which are in Postgres:

```
read_knowledge_base("langchain")   -> No stored documentation called 'langchain'.
read_knowledge_base("langgraph")   -> No stored documentation called 'langgraph'.
read_knowledge_base("django")      -> No stored documentation called 'django'.
read_knowledge_base("tensorflow")  -> No stored documentation called 'tensorflow'.
read_knowledge_base("pytorch")     -> No stored documentation called 'pytorch'.
                     Available: (nothing stored yet)
```

DocsForge harvested five corpora over MCP and could not read one of them back
over MCP. Everything downstream of that follows: `learn_technology`'s
already-stored check reads the empty store, so nothing is ever recognised as
already harvested and every technology re-crawls; `search_knowledge_base`
searches nothing; and the panel's storage chip names Postgres while the MCP
surface reads a folder.

**The guard against exactly this exists and cannot fire.** `build_store()`
carries `degraded` and `wanted_dsn` so that a silent fallback is impossible —
its own comment says *"Falling back silently means everything you ever harvested
appears to have vanished, with the interface calmly reporting an empty store."*
Both are set only when a DSN was present and unreachable. Measured in a process
with no `.env`:

```
store().kind = files      degraded = ''      wanted_dsn = ''
```

The defence was built one layer too far in: it catches a database that is down,
and not a database that was never mentioned.

### F2 — one extra strong signal outranks the project's own documentation 🔴

`ISSUES.md` R1. This produced two of the four wrong answers.

`evidence()` ranks on meaning first — forge below documentation, then
`docs-host`, then `own-domain` — and only then falls through to a **count** of
strong signals. The signals are not equally strong, and a count treats them as
if they were. Every candidate, from a cleared cache:

```
tensorflow
  tensorflow.github.io/rust/tensorflow   own-domain, repo-identity, names-it:14   WON
  www.tensorflow.org/guide/              own-domain, names-it:17                  verified
  www.tensorflow.org/                    own-domain, names-it:20                  verified
  github.com/tensorflow/rust             registry-agreement, names-it:6           verified

pytorch
  pytorch.kr/                            own-domain, install:pypi, names-it:37    WON
  pytorch.org/llms.txt/                  own-domain, names-it:129                 verified
  pytorch.org/                           own-domain                               REFUSED
```

`www.tensorflow.org` was fetched, read and **verified**. It lost by a single
signal to a rustdoc page. `pytorch.org/llms.txt/` names PyTorch 129 times and
lost to a community mirror that carries a `pip install` line.

Note the last line too: `pytorch.org/` itself was **refused** — "only
own-domain" — because a client-rendered homepage does not say its own name
three times in the raw HTML. The gate is simultaneously too strict for the real
site and too loose for the mirror.

This is the failure `evidence()` was written to prevent. Its docstring names
the case: `github.com/langchain-ai/langchainjs/tree/main/libs/langchain/`
beating `docs.langchain.com`. The ranking is sound; it is reached through a
door it does not cover, which is F3.

### F3 — `*.github.io` is the project's own domain, and is not a forge 🟠

`ISSUES.md` R2.

`is_forge()` lists `github.com`, `gitlab.com`, `bitbucket.org`,
`sourceforge.net`. `tensorflow.github.io` is not among them, so two things
happen at once: `_owns_the_name()` sees the label `tensorflow` and awards
**`own-domain`**, and `evidence()`'s first key — *a forge never outranks a docs
site* — never engages.

`github.io` is a shared, first-come namespace. The label belongs to whoever
registered the organisation. `_owns_the_name`'s own docstring dismisses that
class of claim for package registries — *"one package in one namespaced,
first-come registry"* — and here it is being treated as the strongest evidence
a URL can offer.

### F4 — a sub-project documented on its parent's domain cannot pass the gate 🔴

`ISSUES.md` R3. This produced the langgraph result.

LangGraph is documented at `docs.langchain.com`. The identity gate asks whether
the *host* carries the asked-for name; that host carries `langchain`. So the
real documentation earns neither `own-domain` nor `docs-host`:

```
reference.langchain.com/python/langgraph/           names-it:11
    refused: "only names-it:11"
docs.langchain.com/oss/python/langgraph/overview    registry-agreement
    refused: "only registry-agreement"
github.com/langchain-ai/langgraph/tree/main/libs/langgraph
    repo-identity, names-it:10                      VERIFIED — chosen
```

Both documentation sites were fetched and refused. The source tree passed, and
the harvest stored its README: one page, 6,350 characters, recorded
**complete**.

The middle line is the one that matters. PyPI *nominates*
`docs.langchain.com/oss/python/langgraph/overview` as langgraph's homepage —
that is `registry-agreement`, a strong signal — and it needed one more. It did
not get `names-it` because the page renders client-side and the raw HTML does
not repeat the word.

This is not a squatter problem and R1's remedies do not reach it. Monorepos and
umbrella projects documenting children on the parent's domain is the normal
arrangement, not an edge case.

### F5 — completeness is true by construction wherever no denominator exists 🔴

`ISSUES.md` C1.

`_note_coverage()` ends:

```python
else:
    stats["whole"] = True
...
    stats.setdefault("expected", len(docs))
stats.setdefault("acquired", len(docs))
```

Any strategy that did not set `whole` itself is declared complete, and
`expected` becomes however many pages came back. `acquired == expected` is then
true by definition:

```
langgraph   pages=1  expected=1  complete=True   strategy=github
tensorflow  pages=1  expected=1  complete=True   strategy=crawl
```

So the two most wrong results in this run are also the two the store presents
with the most confidence. A caller reading `list_knowledge_base` sees
`tensorflow — 1 page, complete`, and the tool told the model *"do NOT
re-harvest to answer questions about it."*

The three-state design is right and the manifest and sitemap paths honour it —
django and pytorch both reported `False` correctly. But `unknown` is the state
that should apply here, and `_note_coverage` can essentially never produce it,
because `True` is the default rather than `None`.

`PRODUCT.md` principle 3 is *"Never report unearned confidence."* This is the
one place the code does exactly that, and it does it silently.

### F6 — the density note measures documents whose bodies were deleted 🔴

`ISSUES.md` C2. This fires on **every** harvest of 20 pages or more.

```python
# forge_tools.py:931
shape_note = llmsfinder.density_note([len(d.markdown) for d in docs])
```

By that line `docs` has been through `_drain()`, which returns
`Doc(doc.url, doc.title, "")` — bodies released on purpose so peak memory stays
proportional to page count. That was the fix for the old S5. So every length is
`0`, `reads_as_stubs()` sees a median of zero, and its threshold is satisfied
unconditionally.

The clearest instance is langchain: **8,956,185 characters across 627 pages —
14,283 per page** — reported as

> NOTE: these 627 pages have a median of 0 characters and 627 of them are under
> 500. That is the shape of an API symbol index or a split dump, not prose
> documentation.

Against pytorch's stored rows the real figures are a median of **2,865**
characters and **5** pages under 500. Run on true sizes, `reads_as_stubs()`
returns `False` and `density_note()` returns `""`.

The note is not merely wrong, it is inverted: the one signal the product has
for *"this corpus is the wrong shape"* now fires on prose documentation, which
is the same as not having the signal at all. It was added deliberately, two
weeks ago, to stop precisely the mistake it is now making in reverse.

The suite missed it because the tests call `density_note(sizes)` with
hand-built lists. The fixture agrees with the assumption that wrote it.

### F7 — the marketing filter is a spelling-matched deny-list 🔴

`ISSUES.md` E1. **This is F12 from the previous audit, returning.**

F12 was recorded fixed by *"prefer `/docs`, `/guide`, `/reference` and friends
where enough exist, and otherwise drop `/blog`, `/careers`, `/pricing` and the
rest."* Django spells it `/weblog/`:

```
_NOT_DOCS matches /blog      -> True
_NOT_DOCS matches /weblog/   -> False
```

Both halves of `_focus_on_docs` fail together here. The allow-list branch needs
five or more `/docs`-shaped URLs and finds none — Django's documentation is on
`docs.djangoproject.com`, a different host that this harvest never visited. The
deny-list branch does not recognise `weblog`. So the entire weblog passed:

```
django 5.2, 232 pages stored
  214  /weblog/     (92%)
    8  /foundation/
    3  /conduct/
    2  /start/
    5  other
```

A caller who asked for Django 5.2 documentation received *"DSF member of the
month"*, *"2026 DSF Board Election Results"* and *"PyCharm & Django Fall
Fundraiser"* — filed as version 5.2, and reported only as *incomplete*, never
as the wrong material.

The fix's shape is the problem, not its content. The next site will spell it
`/journal/` or `/updates/` and a deny-list will lose again.

### F8 — a version label is applied without anything confirming it 🟠

`ISSUES.md` C3.

`django 5.2` is 232 pages of weblog with nothing version-scoped in it.
`pytorch lts` is a Korean community site — and PyTorch's LTS programme ended
after 1.8.2, so the label names a release line that no longer exists. Both
labels came from the caller and neither was checked against what was harvested.

`versions.py` is strict about ordering labels and about whether a found release
answers a requested one. Nothing consults it here: `_version_label` takes
`opts.version` when it is given one. Storing an unverified version label is the
same failure as stamping a corpus complete, in a different field.

### F9 — federation did not discover the documentation subdomain 🟠

`ISSUES.md` E2.

`www.djangoproject.com` links to `docs.djangoproject.com` from its primary
navigation. Federation exists to notice that a technology is several corpora,
admits hosts from link evidence gathered while crawling, and marks unselected
ones `not requested`. It surfaced nothing: no corpus list in the result, no
second corpus stored, nothing marked `not requested`.

Not diagnosed further. It is one of three things — the link evidence was never
gathered, the candidate was refused at the identity gate, or intent deselected
it silently — and only the first is benign. If it was the second, F4 is broader
than langgraph.

### F10 — smaller things 🟢

- The tracker reported `pages=0` for langgraph, which stored a page: the
  `github` strategy does not pass through the counting fetcher. Zero reads as
  failure to anyone watching.
- Every job publishes `phase=starting` twice.
- `pytorch.org/llms.txt/` was probed with a trailing slash appended to a file
  URL.
- The stated test count is stale in three documents, three different ways:
  README says 611, `ARCHITECTURE.md` 740, `PRODUCT.md` 757. Measured: **817**.
  This was recorded once before as F20, for one file; it has since drifted in
  two more.

---

## 5. What the suite did not catch, and why

817 tests passed while all ten findings above were live. That is worth more
attention than any individual defect.

- **F1** cannot be caught by the suite by construction: `conftest.py`
  deliberately strips `DOCSFORGE_DB` so tests never touch a real database. The
  isolation is correct, and it makes the one configuration users actually run —
  `.env` present, MCP server launched fresh — the one configuration nothing
  exercises.
- **F6** is tested, thoroughly, at the wrong altitude. `density_note` has unit
  tests with hand-built size lists, and they pass, because the defect is in what
  the caller hands it. No test harvests through a real sink and reads the note.
- **F5** and **F8** are assertions about *claims*, and the suite checks
  mechanics. Nothing asserts "a corpus of one page from a strategy with no
  manifest must not be `complete`", because until you look at a real harvest it
  does not occur to you that it could be.
- **F2, F3, F4, F7** need the live web. They are exactly the class the
  measurement harness exists for, and `measure.py` has not been run against
  these names.

The pattern is the one this project has hit before and written down: a green
test suite says the code does what its author expected. Every defect here lived
in the gap between that and what a site actually is.

---

## 6. What to do next, in order

1. **F1**, first and alone. It is a two-line fix, it is the difference between
   the MCP surface working and not working, and every other measurement taken
   through that surface is suspect until it lands.
2. **F6**, second. Also small — measure at the sink where the body is still in
   hand — and it currently corrupts the shape claim on every substantial
   harvest, including the good one.
3. **F5**, third. Default `whole` to `None`. Expect corpora that currently read
   `complete` to become `unknown`; that is the fix working, not a regression.
4. **F7**, then. A dated URL path is an article on every site that has ever
   published one; that is a more durable test than a list of nouns.
5. **F2/F3/F4** together, as one deliberate piece of work on the gate and the
   ranking, with a measurement run before and after. These are the ones where
   widening recall has previously multiplied looseness, and none of them should
   ship on reasoning alone.
6. **F8, F9, F10** after.

The five corpora from this run are in the store. `tensorflow`, `langgraph` and
`django` should be deleted rather than kept — they are wrong material under
right-looking names, which is the one thing this product exists not to produce —
but that is the owner's call, and `forget_documentation` is gated behind
`DOCSFORGE_ALLOW_DELETE` for good reason. The four resolutions are also cached
for 30 days; `forget_resolution` clears them.

---

## 7. Reproducing this

The full run, verbatim, including every tool call and its result:

```
Project Development/EVALUATION-2026-09-10.log
```

The two resolution diagnostics at the end of that file re-run `resolve()` for
langgraph, tensorflow and pytorch with the cache cleared, and print every
candidate with its signals and the reason it was accepted or refused. They are
resolution only — no harvest, no store write — and they are the cheapest way to
re-check F2, F3 and F4 after a change.

To repeat the harvests themselves: start `python app.py --port 8000`, then
drive `mcp_server.py` over stdio and call `learn_technology` with the five
argument sets in §1. The harness used here is not committed; it is 200 lines and
its only subtlety is that `mcp_server.py` must be launched with an inherited
environment, because curating one hides F1.
