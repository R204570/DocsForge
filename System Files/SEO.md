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

| Path | What | Where it comes from |
|---|---|---|
| `/`, `/tools`, `/connect` | the three public pages | `scripts/site_src/*.html` → `scripts/build_site.py` → `docsforge/server/site/` |
| `/robots.txt` | allow everything public; keep `/mcp` and the OAuth endpoints out; name the sitemap | `robots_txt()` in `docsforge/server/mcp_server.py` |
| `/sitemap.xml` | the three pages at their canonical URLs | `sitemap_xml()` in `docsforge/server/mcp_server.py` |
| `/og.png` | the 1200 × 630 social-sharing card | `Logo/docsforge-og.png`, copied by the build |
| `/logo.svg`, `/favicon.ico` | the fox | `Logo/` |

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
- `/sitemap.xml` is valid and lists exactly the canonical pages, each serving `200`;
- verification tags appear only when configured, and are escaped.

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

- Set the two verification variables in Vercel and submit the sitemap
  (sections 2–3). This is the one step code cannot do.
- Set the GitHub description, website and topics (section 6).
- Re-run Lighthouse after each design change; keep the SEO score at 100.
- Watch Search Console → *Performance* for the queries the site actually
  appears for, and write for those rather than guessed keywords.
