# Design

The decisions, and the reasons. `Architecture.md` says where things live;
this says why they are shaped as they are, so that the next change can be
judged against the intent rather than against the code that happens to
exist. Each decision names the failure that produced it, because a rule with
no incident behind it is a preference, and preferences get traded away.

The visual design of the public site is `DESIGN.md` at the repository root
(a Stitch design system: near-black ground, one lavender, Inter and
JetBrains Mono). The local web panel's visual design is not documented here.

---

## 1. Verification is the point

**Decision.** A page is accepted as a technology's documentation only when it
*identifies* the project — two strong signals, or one strong signal plus the
name — never because it mentions the name often. An unconfirmed resolution
returns its candidates with the reason each failed, and no `Best:`.

**Why.** Every serious wrong answer this project has produced was a
plausible one: `mojo` → a Java library called procrastination, `tensorflow`
→ the Rust bindings, `pytorch` → the Korean user group, `click` → a
Kubernetes CLI. Each would have been harvested, stamped `verified`, and
answered from with confidence. A refusal costs the user one more question; a
confident wrong answer costs them the check they would otherwise have made.

**Consequences.** `find_docs("@tanstack/react-query")` refuses today, because
tanstack.com never writes the scoped name. That is the design working as
intended and a limitation at once; loosening the gate for scoped names is a
**DECISION** in `Issues.md`, not a bug fix.

## 2. What could not be examined is not evidence against

**Decision.** A candidate whose fetch failed for a reason that says nothing
about it — HTTP 429, a 5xx, a request that never completed — is *unexamined*.
It is neither accepted nor rejected, and nothing weaker may win in its place.
A refusal reached this way is not cached.

**Why.** The identity gate judges pages it has read. When the right page
cannot be read, the laps below it (another registry's same-named project, a
code host, a search hit) still can be, and one of them will pass — because it
genuinely documents *a* project of that name. On 2026-09-20 that stored a
Kubernetes CLI as `click`. The gate was not wrong about the page it read; it
was wrong to read it at all while a stronger one was unanswered.

**Consequences.** Under a rate limit DocsForge says "could not be read,
outranks every candidate that could, try again later" and stops. That is
slower than guessing and is the design.

## 3. Coverage is measured or it is unknown

**Decision.** Four states, never collapsed: `complete` (every page the site
lists is stored), `incomplete` (a stated shortfall: a page cap, unreadable
pages, refused pages), `unknown` (nothing established how much exists — a
crawl that drained its frontier), and the pages that were *refused*. None is
rendered as success. The count a harvest is measured against is what the site
says exists, never the slice a limit left behind.

**Why.** `tensorflow` once stored a one-page corpus as `complete` because the
frontier drained after one page — the strongest claim the product can make,
from the weakest evidence it has. A 600-page manual harvested at
`max_pages=200` gave a third of the docs and said nothing, and answers were
confidently based on a partial copy.

## 4. Store what was published; narrow when reading

**Decision.** Ingestion never reshapes a document. Relevance — sections split
on `h2`/`h3`, ranking, the read cap — is applied at read time, and the cap
says what it dropped.

**Why.** A corpus restructured at ingestion cannot be un-restructured, and
the question the reader asks is not known at harvest time. A read cap is a
context-window fact, not a storage fact; the store is unbounded.

## 5. The site's own words outrank inference

**Decision.** The acquisition ladder tries what the site published about
itself first (`llms-full.txt`, `llms.txt`, its generator's manifest) and
crawls last. An `llms.txt` index beside a fuller dump resolves to the dump.
The request decides the pathway: a named release is not answered by a file
published for the current one.

**Why.** A sitemap is a hint to crawlers; `llms.txt` is a statement to us.
Taking a 2 KB index at face value once stored it as the whole of the AI SDK's
5.7 MB. Broadening a scoped request once answered a question about Mojo with
billing documentation for a different product on the same host.

**Amended 2026-09-24: first is not only.** The published file is still read
first and still supplies every page it has — its copy is the cleanest there
is — but it is then checked against the site's other statements, the
sitemap and the generator's manifest, and what they list in the same section
that it does not is fetched too. Measured on 53 real sites: Angular's
`llms.txt` is 85 curated pages, AWS's lists 2 of a guide's hundreds, GitHub's
7 pages of Actions, FastAPI's `objects.inv` 22 API pages and no guides — each
had been stored as complete, which is the one claim this product exists not
to make falsely. "No redundant discovery" became "no redundant *fetching*":
reading a sitemap costs a request or two; a harvest that agrees with it
fetches nothing more.

## 6. Refuse rather than guess; ask rather than choose

**Decision.** Where a technology documents itself in several places and the
caller's intent does not settle which, the answer is `NEEDS SELECTION` with
the options, remembered once answered. Where a name is ambiguous, the answer
is the candidates. Where a harvest cannot be verified as the release asked
for, it says so.

**Why.** A choice made silently is a choice the caller cannot correct.

## 7. One engine, one tool list

**Decision.** Every tool is defined once in `forge_tools.TOOLS`; the MCP
surface is generated from that list; the CLI and the panel call the same
functions.

**Why.** Two hand-maintained copies of a tool surface drifted: four tools
existed in the panel and not over MCP, and `max_pages` was bounded `1..200`
over MCP while the harvester treated 0 as unlimited, so an MCP client could
not ask for a whole harvest at all. The benchmark compares the live surface
with the checkout on every run.

## 8. Hosted is read-mostly; offline is where the work happens

**Decision.** The hosted server is stateless and ephemeral: it serves reads,
short harvests, and resolution, and says plainly that a harvest past its
deadline is lost. Large harvests, development, and every published benchmark
run offline — a long-lived process on the developer's machine against a
local database, with every variable set to an offline value so the shared
store is never touched.

**Why.** One developer, free tiers, a 300-second request ceiling, and a
store that other people read. A benchmark that wrote to it would be
measuring one thing and damaging another. The offline runner refuses any
database host that is not loopback for the same reason.

## 9. Politeness is not optional

**Decision.** A per-host delay and concurrency cap on every rung of the
ladder; `Retry-After` honoured; three refusals in a row end a harvest; the
refused pages are named.

**Why.** The manifest path once sent 211 back-to-back requests at a small
documentation host. Read the Docs rate-limits an address after three whole
harvests in an hour, and a crawler that answers "slow down" with thirty more
requests is the crawler that gets blocked — and, before this week, filed the
thirty 429s as "nothing on these pages reads like documentation".

## 10. Every request through one door

**Decision.** `Fetcher.get` is the only place an HTTP request leaves the
package. It guards the URL, follows redirects one hop at a time and guards
each, waits out a 429, and reports where the page landed.

**Why.** A guard with two call sites is a guard with one hole. `requests`
following a `302` to `169.254.169.254` on its own defeated the SSRF guard
end to end; the fix is one loop in one function, and the benchmark tries the
redirect on every run.

## 11. Honesty in the answer, not the log

**Decision.** Whatever a tool learned that bears on trust — coverage,
truncation, unreadable and refused pages, which registry the version came
from, that a search fallback matched only some words, that the host cannot
promise a background harvest — is in the text handed to the model, not in a
log a person may read later.

**Why.** The model is the reader. A caveat it does not see is a caveat that
was not made.

## 12. A defect found live becomes a test that names the case

**Decision.** Fixtures are written from what reality showed — the real URL,
the real count, the real status — and quote it in the test's docstring.

**Why.** Every serious defect so far hid behind a green suite, because the
fixture was built from the same assumption as the code. The benchmark suite
exists because a test asks whether the code does what the author expected;
a bench asks what the server actually said.

## 13. The section is the site's, not the URL's

**Decision.** A harvest keeps to the section the start page's own navigation
covers — the deepest directory holding 80% of its sidebar's links — rather
than the one its URL's shape suggests, and derives it from where the page
*lands*, not the address that redirected there. It widens only where the
URL's section holds no real subtree, and never across a release.

**Why.** URL shapes lie in both directions: `angular.dev/overview` is a page,
not a folder, and a harvest kept to `/overview/` stored one page;
`docs.github.com/en/actions` is inside `/en/`, all of GitHub's docs.
`laravel.com/docs` redirects to `/framework/docs`. The sidebar is the manual
stating its own edges. Where one sidebar spans a family of products
(Firebase), it says nothing narrower than the family, and the caller's
`scope=` or `topic=` has to.

## 14. A language is not a package; an edition is not the default

**Decision.** "langgraph for node" is the JavaScript *edition* of LangGraph's
documentation, found beside the edition the name resolves to and taken only
on evidence — the page's own code, or the site filing it under that
language's name — and filed apart as `<name>-<language>`. "go" is a language,
answered from its own manual before any registry is asked.

**Why.** The same project documented per language is the release problem one
axis over: the right name, the wrong documentation, and nothing in a harvest
to show it. And a registry is an authority on packages, not languages — asked
for "go", the only registry that knew the word offered a Rust crate, and the
squatter rule (R10) handed it the win over `go.dev`.

## 15. A topic is scope, and scope is declared

**Decision.** "go for web development" harvests every page of the manual
about the web, whole, plus the pages that introduce the language — judged by
title, address, headings and prose against a vocabulary the answer states —
and lists what it left out by section. It is filed as `<name>-<topic>`.

**Why.** §6 of this file says choosing a corpus is scope and dropping pages
inside one is filtering. A topic is the caller choosing, in their own words,
which part of a corpus they want; the rule it must keep is the one behind §3
and §6 — nothing left out silently, nothing partial presented as whole. The
topic's harvest is complete for the topic, says so, and says what the topic
excluded, so a caller who wanted more can widen instead of guessing.

## 16. Furniture is not documentation

**Decision.** Extraction keeps what the publisher wrote and drops the page's
furniture: heading permalinks, copy buttons, "Edit this page", breadcrumbs,
pagers. Every tab of a tab set is kept under its label; every code line on
its own line; every link made absolute. Published Markdown gets the same:
permalink marks out, relative links resolved, code blocks untouched.

**Why.** §4 forbids reshaping what was published, and none of this is what
was published — it is the site's machinery around it. Kept, it was measured
at thousands of dead `[​](#anchor)` links per corpus, code a model could not
run because its lines had been joined, and the Python tab of every example
stored while the JavaScript tab was deleted as hidden.
