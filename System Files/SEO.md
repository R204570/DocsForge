# SEO

How the public site at **https://docsforge.vercel.app/** presents itself to
search engines and social platforms, how to verify it and submit it, and how
to check that it still works. Everything here is free and needs no custom
domain and no DNS record.

None of this promises a ranking or a date of indexing. It removes the reasons
a search engine would *not* index the site, and says plainly what the site is.
Whether it ranks is measured afterwards, in Search Console.

---

## 1. What the site serves

Every file below is a real file in `docsforge/server/site/`, written by
`python scripts/build_site.py` and served unchanged (`FILES` in
`docsforge/server/mcp_server.py`). What is in the repository is exactly what
a crawler gets.

| Path | What | Source |
|---|---|---|
| `/`, `/tools`, `/connect` | the three public pages | `scripts/site_src/*.html` |
| `/robots.txt` | allow everything public; keep `/mcp` and the OAuth endpoints out; name the sitemap | `ROBOTS` in `build_site.py` |
| `/sitemap.xml` | the three pages at their canonical URLs, each with a `<lastmod>` that moves only when that page's built HTML changes | `sitemap()` in `build_site.py` |
| `/llms.txt` | the site described for language models ([llmstxt.org](https://llmstxt.org/)): what DocsForge is, its tools, where each part is documented | `llms_txt()` in `build_site.py` |
| `/og.png` | the 1200 × 630 social-sharing card | `Logo/docsforge-og.png` |
| `/apple-touch-icon.png`, `/icon-192.png`, `/icon-512.png`, `/site.webmanifest` | home-screen icons and the manifest naming them | `Logo/docsforge-icon-*.png`, `manifest()` |
| `/logo.svg`, `/favicon.ico` | the fox | `Logo/` |
| any other address | the site's 404 page — a real `404`, `noindex` (meta tag and `X-Robots-Tag`), links back to the three pages | `scripts/site_src/404.html` |
| `/<key>.txt` | the IndexNow key file, only when `DOCSFORGE_INDEXNOW_KEY` is set | `indexnow_key_file()` in `mcp_server.py` |

The site's responses are gzip-compressed (the home page goes from 137 KB to
21 KB); `/mcp` is left exactly as the SDK sends it.

**Why production shows none of this before the merge:** Vercel builds
`docsforge.vercel.app` from `main`. Until this work is merged, `/robots.txt`
and `/sitemap.xml` there answer 404. Pull-request previews sit behind Vercel's
login (Deployment Protection) and are `noindex` by Vercel itself, so search
engines only ever see production.

Every page carries, stamped in by `scripts/build_site.py`:

- a **`<title>`** and **meta description** of its own (the build refuses a
  title over 65 characters or a description outside 70–170);
- a **canonical URL** on `https://docsforge.vercel.app` — the one address in
  `docsforge.SITE_URL`. Preview deployments and self-hosted copies name the
  production page as canonical, so they never compete with it;
- `robots: index, follow` — nothing on the site is `noindex`;
- **Open Graph** and **Twitter card** tags (title, description, URL, the
  `og.png` card), copied from the page's own title and description so the
  two never disagree;
- **Schema.org JSON-LD**: `WebSite`, `Person` (the developer, with
  `sameAs` → portfolio, LinkedIn, GitHub), `SoftwareApplication` (free, MIT,
  the repository), and a `WebPage` for the page itself, with breadcrumbs on
  `/tools` and `/connect`. A `Person` rather than an `Organization`:
  DocsForge is one developer's project, and the markup says only what the
  page says.

The home page has a **Frequently asked questions** section (`#faq`): what
DocsForge is, who it is for, what an MCP server is and which tools it
provides, which clients it works with, how it avoids wrong documentation,
API keys, and licence. The questions are written once, in `FAQ` in
`build_site.py`, and rendered both as the visible section and as `FAQPage`
structured data, so the two cannot disagree. (Google shows FAQ rich results
only for a few authoritative sites now; the section is there for readers,
for on-page relevance, and for AI answer engines that quote it.)

The home page also has a **Built by Raj Patel** section (`#developer`) with
links to the portfolio, LinkedIn and GitHub (`rel="me"`), and every page's
footer links to it. The links are defined once, in `DEVELOPER` in
`scripts/build_site.py`.

After editing anything under `scripts/site_src/`, or `Logo/`, run:

```
python scripts/build_site.py
```

and commit the rebuilt files under `docsforge/server/site/`.

---

## 2. Google Search Console

Use a **URL-prefix property** — a Domain property needs a DNS record on
`vercel.app`, which the project does not control.

1. Open <https://search.google.com/search-console>, *Add property* →
   **URL prefix** → `https://docsforge.vercel.app/`.
2. Choose **HTML tag**. Google shows
   `<meta name="google-site-verification" content="XXXX">`. Copy only the
   `XXXX`.
3. In Vercel → the project → *Settings* → *Environment Variables*, add
   `DOCSFORGE_GOOGLE_SITE_VERIFICATION = XXXX` for **Production**, and
   redeploy. The server writes the tag into every page's `<head>`
   (`site_verification()` in `mcp_server.py`); unset, it writes nothing.
4. Check: `curl -s https://docsforge.vercel.app/ | grep google-site-verification`.
   Then press **Verify** in Search Console. Keep the variable set — Google
   re-checks periodically and drops the property if the tag disappears.
5. *Sitemaps* → submit `https://docsforge.vercel.app/sitemap.xml`.
6. *URL inspection* → enter `https://docsforge.vercel.app/` → **Request
   indexing**. Repeat for `/tools` and `/connect`.

Google Analytics (the `G-KTBKMGZM75` tag already on every page) can verify
the property too, if the same Google account owns it.

## 3. Bing Webmaster Tools

1. Open <https://www.bing.com/webmasters>. The quickest route is **Import
   from Google Search Console**, once step 2 is done — it carries the site
   and the sitemap over with no further verification.
2. Otherwise add `https://docsforge.vercel.app/` and choose the **meta tag**
   option. Copy the `content` of `<meta name="msvalidate.01" content="…">`
   into `DOCSFORGE_BING_SITE_VERIFICATION` in Vercel, redeploy, verify.
3. *Sitemaps* → submit `https://docsforge.vercel.app/sitemap.xml`.

Bing also feeds DuckDuckGo, Yahoo and several AI search products.

## 3a. IndexNow (instant Bing indexing)

IndexNow lets the site tell Bing (and Yandex, Seznam, Naver) that a page
changed, instead of waiting for the next crawl.

1. Pick a key: 8–128 letters, digits or dashes — e.g. the output of
   `python -c "import uuid; print(uuid.uuid4().hex)"`.
2. Set `DOCSFORGE_INDEXNOW_KEY` to it in Vercel (Production) and redeploy.
   The site now answers `https://docsforge.vercel.app/<key>.txt` with the
   key, which is how IndexNow checks the submission is the owner's.
3. After any deploy that changes a page:

   ```
   DOCSFORGE_INDEXNOW_KEY=<key> python scripts/indexnow.py
   ```

   It checks the key file is live, then submits every URL in the live
   sitemap. `--dry-run` lists them without submitting.

## 4. Checking indexing and fixing crawl errors

- **Search Console → Pages** lists what is indexed and why anything is not.
  "Discovered – currently not indexed" on a new site is normal for days to
  weeks; it is not an error.
- **URL inspection → Test live URL** shows the page as Googlebot renders it.
  Every page is server-rendered HTML, so the content is there without
  JavaScript.
- `site:docsforge.vercel.app` on Google or Bing gives a rough count.
- If a page drops out: check it answers `200` (`curl -I`), that
  `/robots.txt` still allows it, that its canonical points at itself, and
  that no `noindex` crept in. The tests below check all four.

---

## 5. Validation

Automated, on every push (`tests/test_http_surface.py`):

- each page has exactly one canonical, pointing at its own production URL;
- Open Graph and Twitter tags are present and match the title and description;
- titles and descriptions are unique across pages; no page says `noindex`;
- the JSON-LD parses and names the developer and the repository;
- `/robots.txt` allows `/`, keeps `/mcp` out and names the sitemap;
- `/sitemap.xml` is valid and lists exactly the canonical pages, each serving
  `200` with a valid `<lastmod>`; `robots.txt` and `sitemap.xml` are served
  byte-for-byte as committed;
- the FAQ section and its `FAQPage` markup say the same thing;
- `/llms.txt` lists exactly the tools the tools page documents, and its links resolve;
- the manifest's icons are served; an unknown page is a `noindex` 404 while
  `/mcp` keeps its own answers; the IndexNow key file is served only for the
  configured key;
- the site is gzip-compressed and `/mcp` is not;
- the committed site is exactly what `build_site.py` builds — editing
  `scripts/site_src/` without rebuilding fails CI;
- verification tags appear only when configured, and are escaped.

The build itself refuses a page with no canonical, more or less than one
`<h1>`, a title over 65 characters or a description outside 70–170.

**Lighthouse** (v12, mobile emulation, throttled), run against a local
server before this was merged:

| Page | Performance | Accessibility | Best practices | SEO |
|---|---|---|---|---|
| `/` | 96 | 100 | 96* | 100 |
| `/tools` | 100 | 100 | 96* | 100 |
| `/connect` | 100 | 100 | 96* | 100 |

\* The one deduction was a console error from the Google tag failing to load
in the sandbox the audit ran in, which had no route to googletagmanager.com.
It does not occur on the live site. Two changes earned the performance
figures: gzip (above), and loading the Google Fonts stylesheet without
blocking render (`rel="preload"` + `onload`, with a `<noscript>` fallback) —
first paint on throttled mobile went from 3.2 s to 1.0 s, with no layout
shift.

By hand, after a deploy:

| Check | Tool |
|---|---|
| Lighthouse SEO, performance, accessibility | Chrome DevTools → Lighthouse, or <https://pagespeed.web.dev/> |
| Structured data | <https://search.google.com/test/rich-results> and <https://validator.schema.org/> |
| Social card (LinkedIn) | <https://www.linkedin.com/post-inspector/> — also refreshes LinkedIn's cache |
| Social card (others) | <https://www.opengraph.xyz/> |
| robots.txt | Search Console → *Settings* → *robots.txt* |
| Mobile rendering | Search Console → URL inspection → *View tested page* → screenshot |

`SoftwareApplication` markup is valid but will not produce a rich result on
its own — Google shows that rich result only with ratings or reviews. It is
there to tell search engines what the page is about.

---

## 6. GitHub

The repository description and topics are set on GitHub itself (*About* →
the gear icon), not in a file. Suggested, consistent with the site:

**Description**

> Open-source MCP server that finds a library's official documentation from its name, verifies it, harvests it, and serves it to AI agents and coding assistants.

**Website**: `https://docsforge.vercel.app`

**Topics**

`mcp` `mcp-server` `model-context-protocol` `ai-agents` `documentation`
`llm` `rag` `claude-code` `cursor` `developer-tools` `web-scraping` `python`

The README links the live site, the tools and connect pages, and the
developer; the site links back to the repository from the nav, the hero and
the footer.

## 7. LinkedIn

The same one-line positioning everywhere: *an open-source MCP server that
gives AI agents documentation for technologies they were not trained on.*

**Project entry / About section**

> **DocsForge** — an open-source MCP server that finds a library's official documentation from its name alone, verifies the page really documents it, harvests the whole set, and serves it to AI coding assistants like Claude Code, Cursor and Codex — with a coverage figure that says when a harvest is incomplete.
> Website: https://docsforge.vercel.app · Code: https://github.com/R204570/DocsForge

**Post ideas**, each linking to the site (LinkedIn renders the `og.png` card):

1. *Why your AI coding assistant hallucinates new libraries* — the
   training-cutoff problem, and resolving docs from a package name.
2. *Verify before you trust* — why DocsForge refuses to guess a docs URL,
   with the resolve → verify → harvest diagram from the home page.
3. *Honest coverage* — what an `INCOMPLETE` harvest looks like and why a
   partial manual is worse than none.
4. *Connecting DocsForge to Claude Code / Cursor in one command* — a short
   walkthrough of `/connect`.
5. *What I learned building an MCP server* — the thirteen tools and the
   design choices behind them.

Always link `https://docsforge.vercel.app/` (the canonical address), not a
preview deployment URL.

---

## 8. Next steps

- Merge, then confirm `https://docsforge.vercel.app/robots.txt`,
  `/sitemap.xml` and `/llms.txt` answer 200.
- Set the verification variables (and the IndexNow key) in Vercel and submit
  the sitemap (sections 2–3a). This is the one step code cannot do.
- Set the GitHub description, website and topics (section 6).
- Re-run Lighthouse after each design change; keep the SEO score at 100.
- Watch Search Console → *Performance* for the queries the site actually
  appears for, and write for those rather than guessed keywords.
