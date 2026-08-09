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

## Known open

From the finish review, not yet closed:

- Ornament density is below the quality-bar board; the card's framed picture
  region has no counterpart.
- Focal scale is shallow: the whole type system runs 9–19px.
- `#input::placeholder` uses `opacity: 0.55`, the one grey that is not a dither.
