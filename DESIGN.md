# Design

<!-- impeccable:design-schema 1 -->

Recorded from the built panel (`static/`), not from intention. Where the build
and this file disagree, the build is right and this file is stale.

## World

**HyperCard shoebox stack.** Every answer is a card in a stack you can paint,
not a message in a chat log. The user pinned this direction over the roll's
assignment (seed `f7c15ffe`); the direction contract is the HTML comment that
opens `static/index.html`.

The panel is embedded inside FlowIT, so it behaves as one window on a desktop:
a bordered stack window inset 13px, with the classic 50% checkerboard showing
around it. Below 560px the window goes full-bleed and the desktop is dropped.

## Colour

One bit. `--ink` `#000000` on `--paper` `#ffffff`, inverted to `#ffffff` on
`#101010` under `prefers-color-scheme: dark`. There is no third colour and no
value between the two — every grey is a dither pattern.

## Dither

Four levels, `--d12` `--d25` `--d50` `--d75`, as 4×4 `shape-rendering=crispEdges`
SVGs. They exist in two forms and the distinction is load-bearing:

| Token | Mechanism | Use on |
|---|---|---|
| `--d12/25/50/75` | CSS **mask** over `--ink` | Elements with no text: the desktop, the window shadow, progress bars, the dissolve. Theme-independent. |
| `--stipple`, `--stipple-light` | plain **background-image**, flipped per theme | Anything containing text: table headers. |

A positioned pseudo-element paints *after* inline content, so an overlay
dither prints on top of the words. That bug shipped three times in this build
(menu dropdown, selected rail card, table header) before being caught. Use
`--stipple` on anything with text in it.

Selection in the card rail **inverts** rather than stipples: any dither under
12.5px type muddies it, and "pressed inverts to solid black" is the same
world's rule.

## Type

Two self-hosted bitmap faces, in `static/fonts/`, each used at the size it was
drawn for. Both ship **one weight only**, so `font-synthesis: none` is set on
`body` — synthesised bold smears a pixel grid.

| Role | Face | Weight | Used for |
|---|---|---|---|
| Display | DotGothic16 | 400 | Wordmark, menu titles, card titles, `.md` headings, buttons, rail card titles |
| Label | Silkscreen | 700 | Uppercase chips only (`CARDS`, `ASK`, `STOPPED`) |
| Body | system stack | — | Running prose in the document |
| Mono | Monaco / Consolas | — | Code, counts, URLs, metadata |

Body and mono are deliberately system stacks: this is an Operate surface whose
deliverable is long Markdown, and a bitmap face at reading size damages the
thing the product exists to produce.

Pixelify Sans was tried first and rejected: its `C` is a closed ring, so the
menu read "Oard" and the chip read "OARDS".

Prose is capped at 72ch; tables and code blocks are not, because an endpoint
table squeezed into a prose measure is worse than a long line.

## Borders and depth

- **Painted objects** (`.painted`) wear a hand border: a `2px` outline on a
  pseudo-element run through an `feTurbulence` + `feDisplacementMap` filter, so
  the edge wobbles and the text above it stays crisp. `#rough` for cards,
  `#rough-soft` for buttons and the Ask field.
- **System controls** stay crisp — that split is the world's, not an accident.
- Depth is offset outlined rectangles, never blur. The card's `box-shadow`
  draws two paper-and-ink rectangles behind it: the rest of the shoebox.
- The window's shadow is a **sibling element**, not `.stack::after`, because
  `.stack` carries a `z-index` and a stacking context would paint its own
  negative-z pseudo over its background.

## Components

- **Menu bar** — File / Edit / Card / Go / Tools. Active title inverts. Items
  are enabled against real state; disabled labels are stencilled with dither.
  Shortcuts print `Ctrl+` or `⌘` from the actual platform, and only where a
  binding exists.
- **Card** — head (title + three outlined window boxes), meta row, source list,
  scrolling field, foot. It hugs its content and caps at the stage, so a
  four-line answer is a four-line card. Max width 780px.
- **Source list** — one row per tool call: a running barber-pole bar, then a
  tick or a bang, the target, and the character count.
- **Rail** — the shoebox index. Empty state carries a drawn, stippled shoebox.
- **Ask field** — dashed border on focus, the way an active Mac field marched.
- **Buttons** — crisp `2px` outline, `9px` radius, invert on hover and press.
  Primary is filled and inverts to outline.

## Icons

Authored 1-bit SVG `<symbol>`s in the sprite at the top of `index.html`, all at
`1.5` stroke on a 16px grid. No emoji, no icon font. `#stip` is a `<pattern>`
for stippled fills inside artwork.

## Motion

One authored moment: the card **dissolve**, stepped through the world's own
four dither levels as a mask (`steps(1, end)`, 260ms) rather than a fade.
Everything else that moves is state — the painting caret and the barber-pole.
All of it is disabled under `prefers-reduced-motion`.

## Topology

Cards are addressable. Each gets `#c<id>`, `history.pushState` on navigation,
`popstate` to go back, and Back retraces the trail rather than stepping an
index. HyperCard's rule that browse and author are two modes of one object is
the signature interaction: **Edit This Card** turns the rendered document back
into a text field, and the download takes the edited version.

## Responsive

| Width | Layout |
|---|---|
| > 860px | Two columns: rail (210–260px) then stage. |
| ≤ 860px | One column. Rail becomes a horizontal card strip; explicit `grid-template-rows: auto minmax(0,1fr)` — without it, grid's default `align-content: stretch` inflates the auto row and leaves a dead gap. |
| ≤ 560px | Window full-bleed, no desktop, no border, model chip hidden. |

## Source frame

The board's framed picture region, carrying information rather than decoration:
every card shows **what kind of source it was forged from**. A 34px stippled
frame in the card head, 26px in the rail, plus the kind in words in the meta row.

No second detector was written for this. Every document DocsForge produces
already opens with `<!-- source: … | type: KIND | scraped: … -->`, so `app.py`
reads the kind straight out of the tool result and sends it on the `tool` end
event. Six glyphs (`#k-openapi`, `#k-github`, `#k-sitemap`, `#k-html`,
`#k-llms`, `#k-raw`), authored in the same grammar as the rest.

On the inverted selected rail card the frame's border and stroke flip to paper.

## Type scale

9px (Silkscreen chip) → 25px (intro lede), with the card title at 21px as the
focal element. An embedded panel rules out a display hero, but not a hierarchy.

## DocsStore — the second surface

`/library` (`static/library.html`, `library.js`, `store.css`). Same world, same
`style.css`; `store.css` adds only what the box needs that the stack did not
have. Reached from **Go > DocsStore**, `Ctrl+L`.

**The card box, not a data table.** Technologies are dividers, versions are the
cards behind a divider, pages are the lines on a card. The rail keeps its
position and grammar from the stack, so the two surfaces read as one window.

| Level | Where | Carries |
|---|---|---|
| technology | the rail, 12 per page | spine, version count, page count, latest label, size |
| version | the stage, one row each | label, pages, size, harvest date, strategy, source URL |
| page | the reader's index | ordinal, title, and the matched snippet while searching |

- **Divider spine** — an authored 1-bit glyph: a card with a tab, plus one
  edge behind it per extra version, capped at three. The tab is load-bearing.
  Drawn without it, a single-version divider is a bare rectangle beside a
  label, which reads as an empty checkbox and invites a click that does nothing.
- **Pager** — `◀ page 1 of 2 · 18 ▶` under the rail. It stays put at one page
  rather than hiding: its disabled state is the answer to "is there more?".
  `.btn:disabled` stencils a `<span>`, so `.btn.pg:disabled .icon` repeats the
  dither for the arrows.
- **Version tabs** — once inside a version the others stay one click away: the
  same list, folded up. Selected inverts, as everything selected does here.
- **Reader** — index beside document at ≥1000px, index as a strip above it
  below that. Search inside a version marks matches with `<mark>`, which
  inverts, and inverts back inside a selected or hovered row.
- **Hollow cards** — the empty and error states stretch to the stage and centre
  their content instead of hugging. A squat bar across the top of an empty
  stage reads as a failed load; a card with a document in it still hugs.
- **Backend chip** — the menu bar's right-hand corner names where the box is
  stored, dashed square for files, solid for Postgres. Not decoration: Postgres
  ranks search and the file store cannot, and reading unranked results while
  believing they are ranked is worse than knowing.

Snippets arrive marked with `«` `»` rather than markup, and the client escapes
first and promotes the guillemets second, so a page full of angle brackets
cannot smuggle HTML into the index.

Every view is addressable: `#/effect/v3/41`, `hashchange` drives the render.

## Verified

- Both themes probed by computed style, not by eye: ink/paper, card, chip,
  status and the filled primary button all invert. Contrast is 21:1 each way.
- Desktop 1280 and mobile 420 captured in one round; no horizontal overflow at
  either, no console errors.
- `detect.mjs` clean over all static files.
- DocsStore captured at 1280 / 900 / 420 in light and dark
  (`tests/shoot_store.py`), over the real store: 18 technologies across two
  pages, pydantic's two versions, and Effect v3's 703-page index under search.
  No overflow, no console errors at any width.
- Contrast measured on the built page, both themes: body 21:1 / 19:1,
  search snippets the same, the one placeholder in the design 6.2:1 / 7.6:1.
