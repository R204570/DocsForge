# Contract v1: what a stored harvest says about itself

Agreed in [Discussion #35](https://github.com/R204570/DocsForge/discussions/35) and tracked in [#42](https://github.com/R204570/DocsForge/issues/42). This is the specification. The code is `docsforge/store/contract.py` (renderer, parser, validator), and `docsforge/store/contract_v1.schema.json` is the JSON Schema for the frontmatter.

A **revision** is one stored harvest of one corpus at one version. Under contract v1 it is a Markdown document that carries, in its frontmatter, everything needed to judge it without asking anyone.

## Rules

1. **A revision describes itself.** Identity, scope, counts, completeness and the reason for it are all in the frontmatter. Nothing about a revision lives only in a tool response.
2. **Decisions stay outside it.** `usable_for_planning` judges an intent, several corpora and one moment. It is a sidecar that points at revisions by `harvest_id` (see [The planning decision](#the-planning-decision)).
3. **`complete: true` is earned.** It requires an independent count, and at least that many pages stored (see [`complete`](#complete)). The writer enforces this at publish time, because only the writer knows what was stored.
4. **Both stores give the same bytes.** The file store writes its files through the renderer. Postgres keeps the same values and renders its export through the same functions.
5. **Additive.** A file with no frontmatter is contract 0 and reads as it always did.

## Identity

| Id | Example | Unique | Names |
|---|---|---|---|
| `harvest_id` | `hv-20261118T101500Z-3f9a2c1d` | yes, for good | one stored revision: one file, or one `doc_version` row |
| `run_id` | `run-20261118T101458Z-77e0b4a2` | yes, for good | one tool call. It groups an entry corpus with its federated corpora, and `harvest_status` accepts it |
| `job` | `effect-1` | **no** | the short handle people quote. It is handed out again once its status record is swept |

Revisions written before contract v1 export with `harvest_id: "legacy-…"`.

## The document

````markdown
---
docsforge_contract: 1
harvest_id: "hv-20261118T101500Z-3f9a2c1d"
… one line per field, in the order below, every value written as JSON …
---

# acme-sdk 2.4 documentation

## Contents

1. [Getting Started](https://docs.acme.example/2.4/start)
2. [Client API](https://docs.acme.example/2.4/api/client)

---

## Getting Started

Source: <https://docs.acme.example/2.4/start>
<!-- docsforge:page n=1 captured_at="2026-11-18T10:15:12Z" sha256="cb2c406543db031ca8b9bb8ee355235f536b347793721dd07f0f4262a7b903e0" bytes=141 -->

{body: exactly 141 bytes}

---

## Client API
…
````

The file is UTF-8 with LF line endings.

### Frontmatter

| Field | Type | Meaning |
|---|---|---|
| `docsforge_contract` | `1` | The contract version. A file without frontmatter is contract 0. |
| `harvest_id` | string | See [Identity](#identity). |
| `run_id` | string \| null | The run that published it. Null outside a tracked run. |
| `job` | string \| null | The run's handle. Not unique. |
| `technology` | string | e.g. `acme-sdk` |
| `corpus` | string | The store key: the technology, or `technology--corpus` for a federated corpus. |
| `corpus_role` | `entry` \| `federated` | |
| `version` | string | The label it is filed under. |
| `version_basis` | enum | How the label was established: `site-path`, `site-declared`, `registry-current`, `requested-confirmed`, `requested-unconfirmed` (asked for, and nothing confirmed it), `harvest-date`, `undated`, `legacy`, `unstated`. |
| `source` | string | Where the harvest started. |
| `strategy` | string | How it was acquired. |
| `store` | `files` \| `postgres` | Where the exported revision lives. |
| `scope` | object \| null | What `complete` is measured against: `{mode, start, widened_to, topic, max_pages}`. |
| `started_at` | UTC string | When the writer opened. |
| `harvested_at` | UTC string | When the revision was published. |
| `complete` | `true` \| `false` \| `null` | See [`complete`](#complete). |
| `complete_reason` | `{code, text}` \| null | Present whenever `complete` is not `true`. |
| `count_basis` | enum | How `expected_pages` was established (see below). |
| `expected_pages` | int \| null | What `count_basis` says exists. |
| `stored_pages` | int | The page blocks in this document. |
| `not_stored` | object | Pages reached or known of and not stored, per kind: `unextractable`, `refused_429`, `rejected_by_store`, `off_topic`, `dead_404`. Each kind is `{count, urls}`, and the URL list is capped at 100. `off_topic` and `dead_404` are not gaps. |
| `generator` | string | e.g. `docsforge 1.1.0` |

`count_basis` values:

| Value | What counted the pages |
|---|---|
| `manifest` | the site's own manifest, such as `llms.txt` |
| `manifest+links` | a manifest, plus the pages its pages link to |
| `sitemap` | a sitemap |
| `sitemap+links` | a sitemap, plus the pages its pages link to |
| `dump-file` | one published file that arrived whole. This is a weaker claim than a manifest. |
| `repository` | a repository, enumerated through its API |
| `api-spec` | an API specification |
| `spec-sections` | the sections of one specification document |
| `stated` | the caller, which gave a count and no basis |
| `crawl-frontier` | a crawl that ran until nothing in scope was left. This counts what it found, not what exists. |
| `none` | nothing |
| `legacy` | unknown: stored before contract v1 |

### Page block

- **Title.** One line. Runs of whitespace, newlines included, collapse to one space.
- **URL.** Whitespace and `>` are percent-encoded.
- **Body.** Line endings are normalised to LF and outer whitespace is stripped, *before* hashing and storing.
- **`bytes`.** The UTF-8 length of the body. A parser reads exactly that many bytes after the blank line that follows the page line. That way a body containing something shaped like a page boundary cannot split the page (Issues B6).
- **`sha256`.** The lowercase hex SHA-256 of exactly those bytes. A stored hash is exported as stored, never recomputed, so a body changed since it was stored fails validation.
- **`captured_at`.** When DocsForge received the page, in UTC. It is `null` for pages stored before contract v1.

## `complete`

| Value | Only when | `complete_reason.code` |
|---|---|---|
| `true` | `count_basis` states a number independently (not `crawl-frontier` or `none`), **and** `expected_pages` is set, **and** `stored_pages ≥ expected_pages`, **and** `not_stored` holds nothing except `off_topic` and `dead_404` | none |
| `false` | a shortfall was measured, or a page limit or rate limiting cut the harvest short | `page-limit`, `rate-limited`, `unextractable`, `rejected-by-store`, `listed-not-acquired`, `index-without-dump`, `shortfall`, `related-corpora` |
| `null` | nothing independent said how much exists | `crawl-uncounted`, `no-enumeration`, `legacy-unverified`, `related-corpora-unknown` |

The writer applies this table (`contract.judge`). A caller that passes `true` when the conditions fail gets `false` or `null`, with the reason. Only `true` is ever lowered.

## Export

```bash
python -m docsforge --export acme-sdk                    # the default version, to stdout
python -m docsforge --export acme-sdk@2.4 -o acme.md     # one version, to a file
python -m docsforge --export "acme-sdk@2.4#hv-…"         # fails unless that is the stored revision
```

- **Which store.** It reads whichever store is configured. To export a hosted deployment's harvests, run it locally against the same `DOCSFORGE_DB`.
- **Local web app.** `GET /api/library/{tech}/{version}/export` serves the same bytes as a download. There is no hosted route.
- **Pre-v1 revisions.** A revision stored before contract v1 exports with `legacy` provenance:
  - `harvest_id: "legacy-…"`
  - `count_basis` and `version_basis`: `legacy`
  - `captured_at: null` on every page
  - a stored `complete: true` with nothing counted behind it is exported as `null`, with reason `legacy-unverified` (decision D3, Issues M7)
- **`--upgrade-store`** rewrites every contract-0 file in the local knowledge base this way.
  - Each file becomes its own export.
  - It is idempotent.
  - It keeps `index.json.bak`.

## Postgres

Migration v8 adds these columns:

- `doc_version.meta` (jsonb): the frontmatter exactly as written.
- `page.captured_at` (text): the ISO string, so it renders back byte for byte.
- `page.sha256` (text).

Migration v7 added `doc_version.harvest_id` (unique) and `doc_version.run_id`.

## Tool responses

Every harvest and read response, and learn's "already stored" answer, starts with one line naming the revision it is about:

```
<!-- docsforge: corpus="acme-sdk" version="2.4" harvest_id="hv-…" complete=false expected=120 stored=40 -->
```

It comes first, so truncation (which cuts from the end) never removes it.

- Full reads carry the coverage note too.
- A missing `complete` is reported as `null`, never as `true`.

## The planning decision

With `strict=True`, a harvest also ends with the decision as a JSON block:

```json
{
  "contract": 1,
  "decided_at": "2026-11-18T10:16:02Z",
  "run_id": "run-20261118T101458Z-77e0b4a2",
  "intent": "resolve-import",
  "usable_for_planning": false,
  "reason": "the 'sdk' corpus is INCOMPLETE (2 of 120 pages)",
  "judged": [
    {"corpus": "acme-sdk", "version": "2.4", "harvest_id": "hv-…", "kind": "api", "complete": true},
    {"corpus": "acme-sdk--sdk-acme-example", "version": "undated", "harvest_id": "hv-…", "kind": "sdk", "complete": false}
  ]
}
```

Every `complete` in `judged` is copied from the stored revision, never recomputed.

## Attempts that published nothing

A run that stores nothing leaves no revision. Its status record is the trace:

- **What the record holds.** `run_id`, `state`, an `outcome`, and a `published` list of what it stored, with `harvest_id`s. The `outcome` is one of `published`, `finished`, `partial`, `failed` or `stopped`.
- **How long it is kept.** With a database, the record stays for `DOCSFORGE_HARVEST_KEEP_DAYS` (default 7). `harvest_status(harvest="run-…")` finds it in that time.
- **A run that stopped reporting.** Before saying "nothing stored", the status check asks the store for revisions carrying that run's id.

## History

A re-harvest gets a new `harvest_id` and **does not overwrite** the revision it replaces. That revision becomes *superseded*: it is no longer read, listed or searched, but it can still be exported by its id.

```bash
python -m docsforge --export "acme-sdk@2.4#hv-20261001T091200Z-1a2b3c4d"
```

- **How many are kept.** The newest `DOCSFORGE_KEEP_HARVESTS` superseded revisions of each version are kept (default 3). The current revision is never pruned.
- **Where they live.**
  - In Postgres, a superseded row stays in `doc_version` with `state = 'superseded'`, and its pages stay too.
  - In the file store, superseded revisions are kept beside the current file in `<version>.history/<harvest_id>.md`. That is a hard link where the filesystem supports one, and a copy otherwise.
- **Listing them.** The local web app lists a version's kept revisions at `GET /api/library/{tech}/{version}` (`history`, newest first) and exports any of them with `…/export?harvest_id=`.
- **Forgetting.** Forgetting a version forgets its history.

## Fixtures

[`tests/fixtures/contract-v1/`](../tests/fixtures/contract-v1/) holds one synthetic run against `acme.example`:

| File | What it is |
|---|---|
| `complete.md` | An entry corpus, harvested whole and counted by a sitemap. One listed page answered 404, which is not a gap. |
| `partial.md` | A federated corpus from the same run, cut by its page limit: 2 of the 120 pages its manifest lists. |
| `decision.json` | The planning decision for that run. It is not usable, because the SDK corpus is incomplete, and it points at both revisions by `harvest_id`. |
| `attempt-stopped.json` | Another run that stopped reporting with nothing stored. There is no revision, so its status record is the observation. |

The fixtures are generated through the real renderer and status record by `scripts/make_contract_fixtures.py`, with `complete` decided by the real rule. `tests/test_contract_fixtures.py` regenerates them and fails on any difference, so they are always what DocsForge actually writes. `.gitattributes` keeps them byte for byte, with LF line endings on every platform.

## Not in v1

- **Deduplication.** Pages are not deduplicated across federated corpora (Issues T2), so counts are per corpus.
- **Signing.** `sha256` detects accidental change, not tampering.
