"""
The site's diagrams: the system drawn in its own theme, as inline SVG.

Each function returns one SVG fragment — or, for the three figures that
carry a lot at once, a wide SVG and a narrow one side by side, and the
stylesheet shows whichever the viewport can read; build_site.py drops the
result in at `<!--SVG:name-->`. Everything animates through CSS classes
that site.css defines and that only run once the figure's `.rv` container
is marked `.in` by site.js — so without script, or under reduced motion,
every diagram is simply shown at rest, complete.

Classes and the custom property `--d` (delay) are the whole contract:

    .d-in     fades (and drifts) in           .d-draw   draws a path (pathLength=1)
    .d-fill   a cell filling in               .d-bar    a bar growing from the left
    .d-hi     a node lighting up in lavender  .d-flow   a dashed line that flows a while
    .wire-lit the route that fired, in lavender once drawn

Every figure the diagrams show is a recorded run: the pydantic resolution
and its 85 pages, effect's 703 pages and 0.11 s, the 36/813 progress line —
all from the README and the evaluation log, none invented.
"""

from __future__ import annotations

MONO = 'font-family="var(--mono)"'
SANS = 'font-family="var(--sans)"'


def _node(x, y, w, h, label, sub="", d=0, cls="", mono=False, r=10, size=13, sub_size=11):
    face = MONO if mono else SANS
    out = [f'<g class="d-in {cls}" style="--d:{d}ms">',
           f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" class="nd"/>',
           f'<text x="{x + 14}" y="{y + (h / 2 + 5 if not sub else h / 2 - 3)}" {face} font-size="{size}" class="nt">{label}</text>']
    if sub:
        out.append(f'<text x="{x + 14}" y="{y + h / 2 + 15}" {MONO} font-size="{sub_size}" class="ns">{sub}</text>')
    out.append("</g>")
    return "".join(out)


def _wire(path, d=0, lit=False, flow=False):
    cls = "wire d-draw" + (" wire-lit" if lit else "") + (" d-flow" if flow else "")
    return f'<path d="{path}" pathLength="1" class="{cls}" style="--d:{d}ms"/>'


def _grid(x0, y0, cols, rows, cell, gap, d0, step, filled=None, count=None):
    out, n = [], 0
    for r in range(rows):
        for c in range(cols):
            if count is not None and n >= count:
                break
            x = x0 + c * (cell + gap); y = y0 + r * (cell + gap)
            if filled is None or n < filled:
                out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" class="cell d-fill" style="--d:{d0 + n * step}ms"/>')
            else:
                out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" class="cell cell-empty"/>')
            n += 1
    return "".join(out)


def _tick(cx, cy, d):
    return (f'<g class="d-in" style="--d:{d}ms"><circle cx="{cx}" cy="{cy}" r="9" class="ok-dot"/>'
            f'<path d="M{cx - 4.5} {cy + 0.2} l3.2 3.2 l6 -6.4" class="ok-tick" pathLength="1"/></g>')


def _svg(w, h, cls, label, body):
    hidden = ' aria-hidden="true"' if cls == "narrow" else ""
    return (f'<svg viewBox="0 0 {w} {h}" class="diagram {cls}" role="img" aria-label="{label}"{hidden}>'
            + "".join(body) + "</svg>")


# ── the hero: a name goes in; verified, measured documentation comes out ──
HERO_LABEL = ("learn_technology resolves pydantic through PyPI, verifies docs.pydantic.dev "
              "names it 214 times, harvests 85 pages and stores them complete as pydantic 1.10")


def hero() -> str:
    # wide: the call, the registries as a tree beneath it, the verified site beside
    # the one that answered, and the 85 pages spanning the whole stage
    s = []
    s.append('<g class="d-in" style="--d:0ms"><rect x="8" y="14" width="282" height="40" rx="10" class="nd nd-call"/>'
             f'<text x="22" y="39" {MONO} font-size="12.5" class="nt"><tspan class="hi">$</tspan> learn_technology(name="pydantic")</text></g>')
    # crates.io first, PyPI last: the lit route is drawn on top of the shared trunk
    for name, cy, dd in (("crates.io", 186, 420), ("npm", 98, 300), ("PyPI", 142, 360)):
        s.append(_wire(f"M28 54 L 28 {cy - 10} Q 28 {cy} 38 {cy} L 46 {cy}", d=dd, lit=(name == "PyPI")))
    for name, y, dd in (("npm", 80, 500), ("PyPI", 124, 560), ("crates.io", 168, 620)):
        s.append(_node(46, y, 100, 36, name, d=dd, cls="d-hi" if name == "PyPI" else "", mono=True))
    s.append(_wire("M146 142 L 200 142", d=1100, lit=True))
    s.append(_node(200, 106, 353, 72, "docs.pydantic.dev", sub="names 'pydantic' 214 times", d=1300, mono=True, sub_size=12))
    s.append(_tick(531, 126, 1900))
    s.append(_wire("M376 178 L 376 232", d=2000, lit=True))
    s.append(f'<text x="8" y="244" {MONO} font-size="11.5" class="ns d-in" style="--d:2050ms">harvesting</text>')
    s.append(_grid(8, 254, 22, 4, 20, 5, d0=2100, step=12, count=85))
    s.append('<rect x="8" y="364" width="545" height="4" rx="2" class="bar-track d-in" style="--d:3000ms"/>')
    s.append('<rect x="8" y="364" width="545" height="4" rx="2" class="bar-fill d-bar" style="--d:3100ms;--to:1"/>')
    s.append(f'<text x="8" y="392" {MONO} font-size="12" class="nt d-in" style="--d:3500ms"><tspan class="hi">complete</tspan> — every page the sitemap lists</text>')
    s.append(f'<text x="553" y="392" {MONO} font-size="12" text-anchor="end" class="ns d-in" style="--d:3600ms">stored as <tspan class="nt">pydantic 1.10</tspan></text>')
    wide = _svg(560, 404, "wide", HERO_LABEL, s)

    # narrow: the same run, top to bottom, drawn at a size a phone can read
    n = []
    n.append('<g class="d-in" style="--d:0ms"><rect x="10" y="10" width="320" height="40" rx="10" class="nd nd-call"/>'
             f'<text x="24" y="35" {MONO} font-size="12.5" class="nt"><tspan class="hi">$</tspan> learn_technology(name="pydantic")</text></g>')
    for i, (name, x, w, d) in enumerate((("npm", 10, 78, 500), ("PyPI", 98, 84, 560), ("crates.io", 192, 138, 620))):
        n.append(_wire(f"M170 50 C 170 70, {x + w / 2} 70, {x + w / 2} 92", d=300 + i * 60, lit=(name == "PyPI")))
        n.append(_node(x, 92, w, 36, name, d=d, cls="d-hi" if name == "PyPI" else "", mono=True))
    n.append(_wire("M140 128 L 140 166", d=1100, lit=True))
    n.append(_node(10, 166, 320, 60, "docs.pydantic.dev", sub="names 'pydantic' 214 times", d=1300, mono=True))
    n.append(_tick(312, 184, 1900))
    n.append(_wire("M170 226 L 170 258", d=2000, lit=True))
    n.append(f'<text x="10" y="278" {MONO} font-size="11" class="ns d-in" style="--d:2050ms">harvesting</text>')
    n.append(f'<text x="330" y="278" {MONO} font-size="11" text-anchor="end" class="ns d-in" style="--d:3500ms">stored as pydantic 1.10</text>')
    n.append(_grid(10, 286, 17, 5, 16, 3, d0=2100, step=12))
    n.append('<rect x="10" y="392" width="320" height="4" rx="2" class="bar-track d-in" style="--d:3000ms"/>')
    n.append('<rect x="10" y="392" width="320" height="4" rx="2" class="bar-fill d-bar" style="--d:3100ms;--to:1"/>')
    n.append(f'<text x="10" y="418" {MONO} font-size="11.5" class="nt d-in" style="--d:3500ms"><tspan class="hi">complete</tspan> — every page the sitemap lists</text>')
    narrow = _svg(340, 430, "narrow", HERO_LABEL, n)
    return wide + narrow


def resolve() -> str:
    s = []
    s.append(f'<g class="d-in" style="--d:0ms"><rect x="14" y="97" width="110" height="36" rx="10" class="nd nd-call"/><text x="28" y="120" {MONO} font-size="12.5" class="nt">"effect"</text></g>')
    for i, (name, y, ans) in enumerate((("npm", 26, "effect.website"), ("PyPI", 97, "—"), ("crates.io", 168, "docs.rs/effect"))):
        answered = ans != "—"
        s.append(_wire(f"M124 115 C 150 115, 150 {y + 18}, 178 {y + 18}", d=250 + i * 70, lit=answered))
        s.append(_node(178, y, 92, 36, name, d=450 + i * 70, mono=True))
        s.append(_wire(f"M270 {y + 18} L 292 {y + 18}", d=900 + i * 70, lit=answered))
        s.append(f'<text x="298" y="{y + 22}" {MONO} font-size="11.5" class="{"nt" if answered else "ns dim"} d-in" style="--d:{1050 + i * 70}ms">{ans}</text>')
    s.append(f'<text x="14" y="212" {MONO} font-size="10.5" class="ns d-in" style="--d:1400ms">keyless · the registry says where the library documents itself</text>')
    return _svg(420, 230, "", "A name is looked up in npm, PyPI and crates.io; each answers with where the library documents itself", s)


def verify() -> str:
    s = []
    s.append('<g class="d-in" style="--d:0ms"><rect x="18" y="16" width="230" height="198" rx="12" class="nd"/>')
    s.append(f'<text x="34" y="42" {MONO} font-size="11" class="ns">docs.pydantic.dev</text></g>')
    lines = [(62, 150, [(70, 26)]), (78, 190, [(110, 26), (160, 26)]), (94, 170, []), (110, 200, [(52, 26)]),
             (126, 140, []), (142, 196, [(140, 26)]), (158, 176, [(34, 26)]), (174, 120, []), (190, 188, [(96, 26)])]
    for k, (y, wl, marks) in enumerate(lines):
        s.append(f'<rect x="34" y="{y}" width="{wl}" height="6" rx="3" class="tl d-in" style="--d:{200 + k * 40}ms"/>')
        for mx, mw in marks:
            # the mark is its own lit node: the selector is `.d-hi .mark`
            s.append(f'<g class="d-hi" style="--d:{700 + k * 90}ms"><rect x="{mx}" y="{y - 2}" width="{mw}" height="10" rx="3" class="mark"/></g>')
    s.append(f'<text x="270" y="86" {MONO} font-size="11" class="ns d-in" style="--d:1500ms">names it</text>')
    s.append(f'<text x="270" y="122" {MONO} font-size="30" font-weight="600" class="nt hi d-in" style="--d:1600ms"><tspan class="count" data-to="214">214</tspan>×</text>')
    s.append(_tick(282, 156, 2000))
    s.append(f'<text x="298" y="160" {MONO} font-size="11.5" class="nt d-in" style="--d:2000ms">verified</text>')
    s.append(f'<text x="270" y="196" {MONO} font-size="10.5" class="ns d-in" style="--d:2100ms">one mention is noise</text>')
    return _svg(420, 230, "", "A page is trusted only when it names the package repeatedly: docs.pydantic.dev names pydantic 214 times", s)


def harvest() -> str:
    s = []
    for i, (name, y, hit) in enumerate((("llms-full.txt", 22, True), ("sitemap.xml", 82, False), ("scoped crawl", 142, False))):
        s.append(_node(16, y, 150, 40, name, d=i * 180, mono=True, cls="d-hi" if hit else ""))
        if i < 2:
            s.append(_wire(f"M91 {y + 40} L 91 {y + 60}", d=150 + i * 180))
    s.append(f'<text x="16" y="212" {MONO} font-size="10.5" class="ns d-in" style="--d:600ms">first strategy that works wins</text>')
    s.append(_wire("M166 42 C 200 42, 200 66, 226 66", d=700, lit=True))
    s.append(_grid(226, 36, 8, 6, 12, 4, d0=900, step=18))
    s.append(_wire("M290 130 L 290 150", d=2000, lit=True))
    s.append(_node(226, 150, 150, 44, "pydantic/1.10.md", sub="one file per version", d=2100, mono=True))
    return _svg(420, 230, "", "Best strategy first: llms-full.txt, then a docs-scoped sitemap, then a scoped crawl; every page lands as Markdown", s)


KINDS = ["llms.txt / llms-full.txt", "OpenAPI / Swagger", "sitemap.xml", "GitHub repositories", "HTML docs &amp; raw Markdown"]
SOURCES_LABEL = "Five kinds of source converge into one Markdown output; the URL decides which strategy runs"


def sources() -> str:
    s = []
    for i, name in enumerate(KINDS):
        y = 14 + i * 38
        s.append(_node(14, y, 214, 30, name, d=i * 90, mono=True, r=8))
        s.append(_wire(f"M228 {y + 15} C 300 {y + 15}, 300 105, 372 105", d=450 + i * 90))
    s.append(_node(372, 80, 118, 50, "detect", sub="the URL decides", d=1000, mono=True, cls="d-hi"))
    s.append(_wire("M490 105 L 520 105", d=1300, lit=True))
    s.append(_node(520, 80, 66, 50, ".md", d=1450, mono=True))
    wide = _svg(600, 210, "wide", SOURCES_LABEL, s)
    n = []
    for i, name in enumerate(KINDS):
        y = 10 + i * 38
        n.append(_node(10, y, 250, 30, name, d=i * 90, mono=True, r=8))
        n.append(_wire(f"M260 {y + 15} C 300 {y + 15}, 300 262, 170 262", d=450 + i * 90))
    n.append(_node(95, 240, 150, 50, "detect", sub="the URL decides", d=1000, mono=True, cls="d-hi"))
    n.append(_wire("M170 290 L 170 318", d=1300, lit=True))
    n.append(_node(137, 318, 66, 44, ".md", d=1450, mono=True))
    narrow = _svg(340, 380, "narrow", SOURCES_LABEL, n)
    return wide + narrow


def coverage() -> str:
    s = []
    s.append(_grid(14, 14, 40, 15, 11, 3, d0=0, step=4, filled=200))
    s.append('<rect x="14" y="228" width="592" height="4" rx="2" class="bar-track d-in" style="--d:800ms"/>')
    s.append('<rect x="14" y="228" width="592" height="4" rx="2" class="bar-fill bar-danger d-bar" style="--d:900ms;--to:0.3333"/>')
    s.append(f'<text x="14" y="252" {MONO} font-size="12.5" class="ns d-in" style="--d:1300ms"><tspan class="nt">200</tspan> stored</text>')
    s.append(f'<text x="606" y="252" {MONO} font-size="12.5" text-anchor="end" class="ns d-in" style="--d:1300ms"><tspan class="danger">400+</tspan> still queued</text>')
    return _svg(620, 258, "", "A grid of 600 pages: 200 stored, 400 or more still queued; coverage a third", s)


CLIENTS = ["Claude Code", "Cursor", "ChatGPT", "Codex", "Windsurf", "Gemini CLI"]
ARCH_LABEL = ("MCP clients call one endpoint, /mcp, which reads and writes one Postgres store; "
              "a long-lived DocsForge on your machine writes the same store")


def architecture() -> str:
    s = []
    for i, c in enumerate(CLIENTS):
        y = 14 + i * 40
        s.append(_node(14, y, 130, 30, c, d=i * 60, mono=True, r=8))
        s.append(_wire(f"M144 {y + 15} C 200 {y + 15}, 200 106, 258 106", d=400 + i * 60, flow=True))
    s.append(_node(258, 66, 196, 80, "DocsForge  /mcp", sub="12 tools · streamable HTTP", d=900, cls="d-hi", mono=True))
    s.append(f'<text x="258" y="166" {MONO} font-size="10.5" class="ns d-in" style="--d:1000ms">one engine, every surface</text>')
    s.append(_wire("M454 106 L 500 106", d=1200, lit=True, flow=True))
    s.append('<g class="d-in" style="--d:1350ms"><ellipse cx="560" cy="66" rx="60" ry="12" class="nd"/><path d="M500 66 v80 a60 12 0 0 0 120 0 v-80" class="nd"/>'
             f'<text x="560" y="112" {MONO} font-size="12.5" text-anchor="middle" class="nt">PostgreSQL</text>'
             f'<text x="560" y="130" {MONO} font-size="10.5" text-anchor="middle" class="ns">a row per page</text></g>')
    s.append(_node(258, 214, 196, 56, "python main.py", sub="your machine, long-lived", d=1600, mono=True))
    s.append(_wire("M454 242 C 490 242, 505 220, 522 158", d=1800, lit=True, flow=True))
    s.append(f'<text x="258" y="290" {MONO} font-size="10.5" class="ns d-in" style="--d:1900ms">large harvests happen here · readable everywhere at once</text>')
    wide = _svg(680, 300, "wide", ARCH_LABEL, s)
    n = []
    for i, c in enumerate(CLIENTS):
        col, row = i % 2, i // 2
        x, y = 10 + col * 168, 10 + row * 38
        n.append(_node(x, y, 152, 30, c, d=i * 60, mono=True, r=8, size=12))
        n.append(_wire(f"M{x + 76} {y + 30} C {x + 76} {y + 60}, 170 150, 170 170", d=400 + i * 60, flow=True))
    n.append(_node(60, 170, 220, 70, "DocsForge  /mcp", sub="12 tools · streamable HTTP", d=900, cls="d-hi", mono=True))
    n.append(_wire("M170 240 L 170 274", d=1200, lit=True, flow=True))
    n.append('<g class="d-in" style="--d:1350ms"><ellipse cx="170" cy="286" rx="60" ry="12" class="nd"/><path d="M110 286 v66 a60 12 0 0 0 120 0 v-66" class="nd"/>'
             f'<text x="170" y="324" {MONO} font-size="12.5" text-anchor="middle" class="nt">PostgreSQL</text>'
             f'<text x="170" y="342" {MONO} font-size="10.5" text-anchor="middle" class="ns">a row per page</text></g>')
    n.append(_wire("M170 396 L 170 366", d=1800, lit=True, flow=True))
    n.append(_node(60, 396, 220, 56, "python main.py", sub="your machine, long-lived", d=1600, mono=True))
    narrow = _svg(340, 470, "narrow", ARCH_LABEL, n)
    return wide + narrow


DIAGRAMS = {"hero": hero, "resolve": resolve, "verify": verify, "harvest": harvest,
            "sources": sources, "coverage": coverage, "architecture": architecture}
