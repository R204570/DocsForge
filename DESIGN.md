---
name: DocsForge
description: The public site of an MCP server that hands a model verified, measured documentation — near-black ground, one lavender, Inter and JetBrains Mono, the system drawn in its own theme.
colors:
  bg: "#0b0b0d"
  deep: "#0e0e11"
  soft: "#121215"
  surface: "#17171b"
  line: "#2a2a31"
  line-soft: "#202027"
  wire: "#55555f"
  text: "#ececee"
  text-2: "#a9a9b3"
  text-3: "#858591"
  accent: "#cf9fff"
  accent-bright: "#dcb8ff"
  accent-ink: "#170b24"
  accent-dim: "rgba(207, 159, 255, 0.13)"
  accent-line: "rgba(207, 159, 255, 0.34)"
  danger: "#ff9a9a"
typography:
  display:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "clamp(38px, 5.4vw, 62px)"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.03em"
  display-doc:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "clamp(32px, 4.4vw, 46px)"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "clamp(26px, 3.4vw, 36px)"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.01em"
  title-sm:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.01em"
  lead:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "clamp(17px, 1.7vw, 20px)"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
  ui:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "15px"
    fontWeight: 500
    lineHeight: 1
    letterSpacing: "normal"
  code:
    fontFamily: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.7
    letterSpacing: "normal"
  label:
    fontFamily: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
rounded:
  tag: "6px"
  sm: "8px"
  md: "10px"
  lg: "14px"
  pill: "999px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "22px"
  xl: "28px"
  2xl: "44px"
  3xl: "56px"
  section: "64px"
  band: "clamp(72px, 9vw, 120px)"
  foot: "96px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    typography: "{typography.ui}"
    rounded: "{rounded.lg}"
    padding: "0 22px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.accent-bright}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    typography: "{typography.ui}"
    rounded: "{rounded.lg}"
    padding: "0 22px"
    height: "44px"
  button-sm:
    typography: "{typography.code}"
    rounded: "{rounded.md}"
    padding: "0 12px"
    height: "32px"
  chip-version:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-2}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "3px 10px"
  tag:
    backgroundColor: "transparent"
    textColor: "{colors.text-2}"
    typography: "{typography.label}"
    rounded: "{rounded.tag}"
    padding: "2px 7px"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.text-2}"
    typography: "{typography.code}"
    rounded: "{rounded.sm}"
    padding: "7px 13px"
  tab-active:
    backgroundColor: "{colors.accent-dim}"
    textColor: "{colors.text}"
  input-token:
    backgroundColor: "{colors.deep}"
    textColor: "{colors.text}"
    typography: "{typography.code}"
    rounded: "{rounded.sm}"
    padding: "9px 12px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-2}"
    rounded: "{rounded.lg}"
    padding: "22px 24px"
  figure:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.lg}"
    padding: "18px"
  figure-deep:
    backgroundColor: "{colors.deep}"
  well:
    backgroundColor: "{colors.deep}"
    textColor: "{colors.text}"
    typography: "{typography.code}"
    rounded: "{rounded.lg}"
    padding: "16px 18px"
  callout:
    backgroundColor: "{colors.accent-dim}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "20px 22px"
  result:
    backgroundColor: "{colors.deep}"
    textColor: "{colors.danger}"
    rounded: "{rounded.lg}"
    padding: "20px 22px"
  nav-top:
    backgroundColor: "{colors.soft}"
    textColor: "{colors.text-2}"
    height: "56px"
  nav-link-active:
    textColor: "{colors.text}"
---

# Design System: DocsForge

## Overview

**Creative North Star: "The Recorded Run"**

The site does not describe DocsForge; it plays one run of it back. A name goes into a prompt at the top of the page and, in the product's own theme, three registries answer, one page verifies, 85 pages land and a bar completes. Every figure on the site is a run that actually happened — pydantic's 214 mentions, Effect's 703 pages, the 36/813 progress line — and every drawing is authored inline SVG built from the same custom properties the page chrome uses. Each later section is one part of that machine shown at work: resolve, verify, harvest, detect, coverage, architecture. The page reads as the product's own console, opened to the public.

The world is near-black and quiet. Four greys make the ground (page, well, soft, surface); hairlines, not gaps, separate things; a single lavender is spent only on what lights up. Words are Inter; anything the software would print — a tool name, a path, a count, a caption, a version — is JetBrains Mono. Density is editorial: one 1100px column, rows that split five-to-seven between words and the figure, wide bands between sections. Depth is tonal, not cast; the one diffuse shadow exists to seat the two showcase surfaces. Nothing on the site is uppercase, letter-spaced open, or iconised.

Motion is playback, never decoration, and it is strictly an enhancement: with script off or reduced motion on, every reveal and every diagram is simply shown at rest, complete. Confirmed rejections: no light mode; no raster imagery or illustration; no icon-card feature grid; no metric tiles; no emoji; no external scripts.

**Key Characteristics:**
- One chromatic colour, lavender, on a near-black four-step grey ladder; red appears only where a harvest fell short
- Inter for what a person wrote, JetBrains Mono for what the software prints
- Authored inline SVG diagrams from a node / wire / cell / bar / tick vocabulary, coloured by the theme's own custom properties, with wide and narrow drawings per figure
- 1px hairline separation everywhere; 14px radii on things you rest content in, 8px on things you tap or type in
- Tonal depth with a single diffuse shadow; a blurred sticky nav; one faint lavender glow behind the hero figure
- Content complete at rest; motion added only by `.js` + `.in` when reduced motion is off

## Colors

A near-black achromatic ladder (every grey sits at hue 286 with almost no chroma) carrying exactly one chromatic voice, lavender, and one status colour, red.

### Primary
- **Lavender** (#cf9fff): the single accent. It goes on what the product prints or lights: tool names, step numbers, the figures in the figures strip, the `$` prompt sigil, the diagram node that answered, each stored page cell, the completed bar, the verified tick's dot, config keys, the active tab's border, the current nav item's underline, the wordmark square, the focus ring, text selection, and the fill of the one primary button per view.
- **Lavender Bright** (#dcb8ff): the primary button's hover fill — the only place the accent brightens.
- **Lavender Ink** (#170b24): text on any lavender fill (9.06:1 on the accent, 11.1:1 on hover) and the stroke of the tick drawn inside a lavender dot.
- **Lavender Tint** (rgba(207, 159, 255, 0.13)): the lit node's fill, the active tab's background, the callout's background, the verify figure's highlighted mentions. At 16% it is the radial glow behind the hero figure.
- **Lavender Line** (rgba(207, 159, 255, 0.34)): the wire that fired, the call node's stroke, the callout's border, the highlighted mentions' stroke.

### Status
- **Incomplete Red** (#ff9a9a): the INCOMPLETE result line, the "400+ still queued" figure and the short coverage bar. It marks a harvest that fell short, and nothing else: not form errors, not hover, not emphasis.

### Neutral
- **Ground** (#0b0b0d): the page.
- **Well** (#0e0e11, `deep`): anything recessed — code blocks, the connect snippet, the result box, the token input, and the hero figure so the drawing sits in a darker well than the cards around it.
- **Soft** (#121215): the sticky nav (at 86% with a 12px blur) and the footer.
- **Surface** (#17171b): cards, figures, the closing card, the version chip, diagram nodes at rest.
- **Hairline** (#2a2a31): the border around every surface, the nav's bottom and footer's top edges, ghost-button and chip borders, diagram node strokes, the grey text lines in the verify figure.
- **Hairline Soft** (#202027): dividers between siblings — feature rows, tool rows, config rows, code-block title bars, the figures strip, the bar track under a harvest.
- **Wire** (#55555f): diagram wires at rest, before a route lights.
- **Text** (#ececee): headlines, body on wells, tool descriptions (16.7:1 on ground).
- **Text Secondary** (#a9a9b3): leads, prose, nav links, card body, returns (8.4:1).
- **Text Tertiary** (#858591): captions, notes, diagram sub-labels, code-bar labels, placeholders (5.4:1 on ground, 4.9:1 on surface). This is the build's value; it supersedes the #7c7c88 listed in PRODUCT.md, which falls under 4.5:1 on a surface.

### Named Rules
**The One Lavender Rule.** Lavender is the only chromatic colour on the site and it marks what the product prints or lights — a name, a number, the route that fired, the page that landed, the action. It is never a decoration, a background wash, or a second-tier emphasis.

**The Tint Does the Area Rule.** Solid lavender never fills an area larger than a button. Where an area must read as lit — a node, an active tab, a callout — it takes the 13% tint bounded by the 34% line.

**The Red Means Incomplete Rule.** Red appears only where coverage fell short of the whole. A UI that has nothing incomplete to report has no red on it.

## Typography

**Display Font:** Inter (700), with `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif` fallback
**Body Font:** Inter (400/500/600), same stack
**Label/Mono Font:** JetBrains Mono (400/500/600), with `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` fallback

Both faces load from Google Fonts with `display=swap`; the page is readable on the fallback stack before either arrives.

**Character:** A tight, heavy Inter for what a person wrote, a small JetBrains Mono for what the machine wrote. The two never share a role: every string on the page is either prose or output, and the face declares which.

### Hierarchy
- **Display** (700, clamp(38px, 5.4vw, 62px), 1.1, -0.03em): the home headline. Beside the hero figure at ≥1024px it tightens to clamp(40px, 3.6vw, 52px) so the figure keeps its seven columns.
- **Display-doc** (700, clamp(32px, 4.4vw, 46px), 1.1, -0.03em): the reading pages' h1 (tools, connect).
- **Headline** (700, clamp(26px, 3.4vw, 36px), 1.1, -0.02em): band and closing-card h2. On reading pages, section h2s drop to 20px (700) and group heads to 20px (600).
- **Title** (600, 24px, 1.1, -0.01em): feature-row h3, preceded by its mono step number. **Title-sm** (600, 18px): card h3.
- **Lead** (400, clamp(17px, 1.7vw, 20px), 1.55, secondary text, ≤62ch): the sentence under any headline; 18px beside the hero figure.
- **Body** (400, 16px, 1.6): row paragraphs at ≤48ch; prose at 1.65 and ≤66ch; card body at 14.5px; notes at 13px.
- **UI** (500, 15px Inter): buttons and the wordmark (600); nav links at 14px.
- **Code** (JetBrains Mono, 400, 13px, 1.7): code blocks, config keys, figures strip, tab labels at 12.5px; the result line at 13.5px/600; tool names at 15px/600 in lavender.
- **Label** (JetBrains Mono, 11–12.5px, sentence case): version chip and argument tags (11px), code-bar titles (11px), step numbers and token label (12px), figure captions and tabs (12.5px).

### Named Rules
**The Mono Prints Rule.** Anything the software would print — a tool name, an argument, an environment key, a path, a count, a version, a caption on a recorded run, a transcript — is set in JetBrains Mono. Anything a person wrote is Inter. The face is the class of the string, never its emphasis.

**The Tighter-as-it-Grows Rule.** Headings track tighter as they grow: -0.01em at 18–24px, -0.02em at headline, -0.03em at display; line-height 1.1 throughout; `text-wrap: balance` on every heading and `pretty` on every lead and paragraph.

**The Sentence-case Rule.** No text on the site is uppercase or letter-spaced open. Labels are small mono in sentence case; hierarchy comes from size, weight and face, never from capitals.

## Layout

One centred column, `max-width: 1100px`, with 24px side padding (16px at ≤640px). The sticky nav is 56px tall (`--nav`), blurred over the content, with the wordmark left, links centred-right, and the version chip at the far right (chip shown from 640px; the "How it works" and "Storage" links from 768px, when link gaps open from 16px to 28px).

**Home.** The hero is a two-column grid at ≥1024px — `minmax(0, 5fr) minmax(0, 7fr)`, 48px gap, vertically centred — words left, the running figure right. Below that width it stacks and centres: copy capped at 820px, figure at 900px, 44px between them. Beneath the hero, a figures strip runs the full column between two soft hairlines (56px above, 22px padding). Each feature row repeats the 5fr/7fr split at ≥900px with a 56px gap and 28px vertical padding; alternate rows carry `.flip` (7fr/5fr, figure first). Rows are separated by a soft hairline. Bands are padded `clamp(72px, 9vw, 120px)` vertically; a band's lead sits 44px above its content. The closing card is a 1.2fr/1fr grid at ≥900px inside `clamp(48px, 6vw, 72px)` by `clamp(24px, 5vw, 56px)` padding.

**Reading pages (tools, connect).** The column opens 64px below the nav with a 768px lede; sections are 64px apart; group heads sit 44px above their list. A tool row is `300px 1fr` at ≥820px (name and tags left, description and return right), 20px vertical padding, soft hairline above; the list closes with a soft hairline. Storage cards are a two-column grid at ≥820px with 16px gap. Configuration rows are `minmax(200px, 320px) 1fr` at 12px padding, single-column at ≤640px.

**Breakpoints observed:** 640px (mobile padding, narrow diagrams, version chip), 768px (full nav), 820px (tool grid, two-column cards), 900px (feature rows, coverage, close), 1024px (hero side by side).

**Diagrams.** A figure's SVG is `width: 100%; height: auto` with a max width of 860px (520px inside a feature row, 620px in the hero). Three figures ship a wide and a narrow drawing; at ≤640px the narrow one shows (max 360px) and the wide one is hidden and taken out of flow. Script keeps `aria-hidden` on whichever is not showing.

**Footer.** 96px above, soft ground, hairline top, 28px vertical padding, 13px secondary text; project name in body weight, then licence, credit and a tertiary note; three project links right.

### Named Rules
**The Five-Seven Rule.** Every two-column composition splits 5fr/7fr: the words take five, the figure takes seven, and consecutive rows flip which side the figure sits on. Below 900px (1024px for the hero) it is one column, figure below words.

**The Hairline Rhythm Rule.** Sibling rows in a list — features, tools, config keys — are separated by a 1px soft hairline and their own padding, never by whitespace alone or by boxing each row.

## Elevation & Depth

Depth is tonal. Four greys form a ladder — ground (#0b0b0d) → well (#0e0e11) → soft (#121215) → surface (#17171b) — and a thing's height on the page is which rung it sits on: code and figures are dug into the well, nav and footer float on soft, cards and figures rest on surface. Every surface is edged with the 1px hairline, so nothing depends on a shadow to find its boundary.

One shadow exists and it is soft, not cast: a deep diffuse drop with a 2px inset highlight along the top edge, and it appears only under the figure and the closing card — the two surfaces that hold the showcase. The sticky nav is soft at 86% opacity with a 12px backdrop blur. The hero figure alone carries a glow: a radial 16% lavender behind and above it, fading out at 72% of its radius.

### Shadow Vocabulary
- **Seat** (`box-shadow: 0 30px 60px -28px rgba(0, 0, 0, 0.85), 0 2px 0 0 rgba(255, 255, 255, 0.02) inset`): under `.figure` and `.close` only. A long, low, negative-spread shadow that seats a surface on the ground without a visible edge.
- **Nav veil** (`background: rgba(18, 18, 21, 0.86); backdrop-filter: blur(12px)`): the sticky header over scrolling content.
- **Hero glow** (`radial-gradient(closest-side, rgba(207, 159, 255, 0.16), transparent 72%)`, `inset: -60px 0 auto; height: 60%`): behind the hero figure only, on a `z-index: -1` pseudo-element in an isolated stacking context.

### Named Rules
**The Ladder, Not Shadow Rule.** Height is a rung on the grey ladder plus a hairline. The seat shadow is reserved for the figure and the closing card; a new surface takes a rung and a border, never a shadow.

## Shapes

Corners are soft and consistent: 14px (`--r`) on anything you rest content in — cards, figures, buttons, code wells, callouts, the result box, the closing card; 8px (`--r-s`) on anything you tap or type in inline — tabs, the token input, the show/hide button; 10px on the small mono button and on diagram nodes (8px on the small nodes in the sources and architecture figures); 6px on the tiniest tags (arguments, client names); a 999px pill only for the version chip. Micro-shapes follow: 3px on the wordmark's 12px lavender square, 2px on diagram page cells and bar tracks, 4px on the focus ring.

Every border is 1px. Surfaces take the hairline; sibling dividers take the soft hairline; the lit things take the lavender line. Chips at rest sit on a 1.5% white wash inside a hairline. Ghost buttons are a hairline that brightens to tertiary text on hover; the current nav item is a 1px lavender underline with 1px of padding beneath the text, not a pill or a fill.

Diagram geometry: nodes are rounded rectangles stroked at 1px; wires are 1.5px round-capped paths; the verified tick is a 9px-radius lavender dot with a 1.8px ink stroke inside; page cells are 11–20px squares at 3–5px gaps; bars are 4px tall. The database is drawn as a cylinder (ellipse plus a swept path) in the same node stroke.

## Components

### Buttons
Confident and quiet: a flat lavender slab for the one action that matters, a hairline outline for the second.
- **Shape:** soft (14px); small variant 10px.
- **Primary:** lavender fill, ink text (#170b24), 44px tall, 0 22px padding, Inter 15px/500, 8px gap for content. Hover brightens the fill to #dcb8ff over 150ms; active presses 1px down.
- **Ghost:** transparent, hairline border, primary text; hover brightens the border to tertiary text.
- **Small:** 32px tall, 0 12px, JetBrains Mono 13px, 10px radius — the Copy control in a snippet's title bar, in the primary colour.
- **Focus:** the global ring — 2px solid lavender, 3px offset, 4px radius.
- **Pairing:** a primary and a ghost side by side with a 12px gap, wrapping; the home page shows exactly two pairs (hero and close).

### Chips / Tags
- **Version chip:** JetBrains Mono 11px, 3px 10px, hairline border, pill, surface fill, secondary text; hidden under 640px.
- **Argument tag:** JetBrains Mono 11px, 2px 7px, hairline, 6px radius, secondary text; 6px gap in a wrapping row under a tool name.
- **Client tag:** JetBrains Mono 11.5px, 4px 10px, hairline, 6px radius, secondary text; the closing card's list of supported clients.
- **Facts line:** 13px tertiary Inter, items separated by a middle dot — "MCP server · open source · MIT" beneath the hero actions.

### Tabs
- **Style:** JetBrains Mono 12.5px, 7px 13px, hairline border, 8px radius, transparent, secondary text; hover to primary text.
- **Active (`aria-pressed="true"` / `.is-active`):** lavender border, 13% lavender tint, primary text. Buttons wrap in an 8px-gap row; the client picker is ten of them.

### Inputs / Fields
- **Token input:** well fill, hairline, 8px radius, 9px 12px, JetBrains Mono 12.5px, primary text; tertiary placeholder. Type `password` with a mono "show/hide" button of the same height beside it.
- **Focus:** the border turns lavender; the outline is suppressed in favour of it.
- **Label:** JetBrains Mono 12px secondary, inline before the field; row wraps at 10px gap.

### Cards / Containers
- **Card:** surface fill, hairline, 14px radius, 22px 24px; h3 at 18px/600 with 8px beneath; 14.5px secondary body; inline code at 13px primary.
- **Figure:** surface (or well, `.figure.deep`, for the hero), hairline, 14px, 18px padding, seat shadow; an optional caption row above the drawing — 12.5px tertiary, left label in mono 11.5px secondary, right note — with 12px beneath. Holds one authored SVG (or a wide/narrow pair).
- **Closing card:** surface, hairline, 14px, seat shadow, `clamp(48px, 6vw, 72px)` by `clamp(24px, 5vw, 56px)` padding, headline plus lead plus client tags on the left, the button pair on the right.
- **Callout:** 13% lavender tint, 34% lavender line, 14px, 20px 22px; 15px primary text; inline code in lavender at 13px. One per page at most.

### Code wells
- **Code block:** well fill, hairline, 14px, clipped; an optional title bar (9px 14px, soft hairline beneath, mono 11px tertiary); `pre` at 16px 18px, mono 13px/1.7, primary text; `.wrap` variant pre-wraps; comment/prompt spans in tertiary.
- **Snippet (connect):** the same well with a title bar holding the client name (mono 12px secondary), the file or transport kind (mono 10.5px tertiary), and the small primary Copy button; `pre` at 18px 20px, min-height 96px.
- **Transcript:** well, hairline, 14px, 18px 20px, mono 13px/1.9; the speaker in tertiary at a fixed 5.5ch; the tool call in lavender.
- **Result box:** well, hairline, 14px, 20px 22px; the first line mono 13.5px/600 in incomplete red, wrapped in tertiary `**` markers drawn by CSS; the paragraph beneath at 14.5px secondary, 14px above.

### Navigation
- **Top bar:** sticky, 56px, soft at 86% with 12px blur, hairline beneath. Wordmark: a 12px lavender square (3px radius) and "DocsForge" at Inter 15px/600, -0.01em, 10px gap. Links: Inter 14px/500 secondary, hover to primary over 150ms; the current page in primary with a 1px lavender underline. The GitHub link carries a 10px stroked arrow (1.6px, 70% opacity) drawn inline, `aria-hidden`. Version chip at the right.
- **Mobile:** links keep their row; the two anchor links and the chip hide below their breakpoints; no drawer.
- **Footer:** soft ground, hairline top, one flex row wrapping at 12px 24px gaps; project links in a 22px-gap nav; hover to primary.

### Tool row
`300px 1fr` at ≥820px. Left: the tool name in lavender mono 15px/600 and its argument tags. Right: the description at 15px primary, then "Returns:" (tertiary, 500) and the return at 13.5px secondary, 6px beneath. 20px vertical padding, soft hairline above.

### Steps (connect)
A numbered vertical list at 18px gap: the step number in lavender mono 12px, its title in Inter 15px/600 primary 10px to the right, and a code block beneath at 10px gap. Step numbers are content numbering, set inline with the title.

### Diagrams (signature)
Every figure on the site is an authored inline SVG in the theme's custom properties, generated by `scripts/site_src/diagrams.py` and inlined at build. The vocabulary:
- **Node** (`.nd`): surface fill, hairline stroke, rx 10 (8 when small), label in Inter or mono at 13px primary, optional mono sub-label at 11px tertiary. **Call node** (`.nd-call`): well fill, lavender-line stroke, mono 12.5px, a lavender `$` sigil. **Lit node** (`.d-hi .nd`): lavender stroke, 13% tint.
- **Wire** (`.wire`): 1.5px round-capped wire-grey path, `pathLength="1"`. **Lit wire** (`.wire-lit`): lavender line — the route that answered. **Flowing wire** (`.d-flow`): a 0.06/0.05 dash that runs for three 1.6s cycles once drawn.
- **Cell** (`.cell`): a lavender square, rx 2 — one stored page; **empty cell** (`.cell-empty`): no fill, 1px #3a3a44 stroke — a page still queued.
- **Bar** (`.bar-track` / `.bar-fill`): 4px, rx 2; soft-hairline track, lavender fill scaled from the left to `--to`; **danger bar** in incomplete red.
- **Tick**: 9px lavender dot with a 1.8px ink check. **Mark** (`.d-hi .mark`): a 10px-tall tinted, lined rectangle over a grey text line — a verified mention. **Text line** (`.tl`): a 6px hairline-coloured bar standing for a line of prose.
- **Text**: primary (`.nt`), tertiary (`.ns`), lavender (`.hi`), red (`.danger`), or dimmed to 45%.
- Every SVG is `role="img"` with a full-sentence `aria-label`; a narrow twin is `aria-hidden` until it is the one showing.

### Motion
Playback of the recorded run, driven by classes and one delay property. The single ease is `cubic-bezier(0.16, 1, 0.3, 1)` for everything but the flowing dash (linear).
- **Reveals** (`.rv`): a block enters at 14px below, 6px blurred, and settles over 700ms once 12% of it is in view (observer root margin -12%). **Lists** (`.rv-list`): children rise 10px over 600ms with a 70ms stagger (capped at 340ms from the sixth).
- **Diagram parts**, each delayed by its own `--d`: `.d-in` fades and drifts 6px over 700ms; `.d-draw` draws a path over 800ms (`--dur`); `.d-fill` pops a cell from 40% over 420ms; `.d-bar` grows a bar over 900ms; `.d-hi` lights a node over 500ms and its marks over 400ms. The verify figure's "214×" counts up over 900ms on a cubic ease-out, after its `--d`.
- **Hover:** 150ms on colour, border and transform; the primary button presses 1px on active.
- **Rest:** all of the above is gated on `.js` (set by script) and `prefers-reduced-motion: no-preference`. Under reduced motion every target is marked `.in` at once and `scroll-behavior` returns to `auto`; without script, nothing is ever hidden.

### Named Rules
**The Complete at Rest Rule.** A figure or block is drawn finished, then motion is subtracted from it only when script has run and motion is allowed. No content, figure or number exists only as the end state of an animation.

**The One Ease Rule.** Every transition and keyframe uses `cubic-bezier(0.16, 1, 0.3, 1)`; the only linear motion is the dash that flows along an active wire.

## Do's and Don'ts

### Do:
- **Do** draw every figure as authored inline SVG using the theme's custom properties (`var(--surface)`, `var(--line)`, `var(--accent)`), from the node / wire / cell / bar / tick vocabulary, and give each a full-sentence `aria-label`.
- **Do** ship a narrow twin for any diagram that cannot be read at 360px, swapped at 640px.
- **Do** put words in the 5fr column and the figure in the 7fr column, and flip consecutive rows.
- **Do** separate list rows with a 1px soft hairline (#202027) and box surfaces with a 1px hairline (#2a2a31).
- **Do** set anything the software prints — tool names, arguments, keys, paths, counts, versions, captions — in JetBrains Mono at 11–13.5px, sentence case.
- **Do** keep the focus ring at 2px lavender, 3px offset, and let the token input's border turn lavender on focus.
- **Do** keep text at or above 4.5:1 on surface (#17171b); tertiary text stays at #858591 for that reason.
- **Do** gate every reveal and diagram animation behind `.js` and `prefers-reduced-motion: no-preference`, and use the one ease.
- **Do** use the seat shadow only on the figure and the closing card.

### Don't:
- **Don't** add a light mode; `color-scheme: dark` and the pinned tokens are the whole palette.
- **Don't** fill an area larger than a button with solid lavender; use the 13% tint bounded by the 34% line.
- **Don't** use red for anything but incomplete coverage.
- **Don't** introduce an icon-card feature grid or metric tiles; the figures strip is a hairline band of mono numbers, and features are rows with a drawing.
- **Don't** use raster imagery, illustration, icon fonts or emoji; the only pictographs are the wordmark square, the inline external-link arrow, and the diagram tick.
- **Don't** set any text in uppercase or with open letter-spacing.
- **Don't** hide content behind motion or load an external script; every page must read complete with script off and at 400px.
- **Don't** give a surface a shadow to lift it; move it a rung on the grey ladder and edge it with a hairline.
