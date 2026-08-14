/* DocsStore — the box of harvested documentation.

   Three levels, and the URL names all three, so any view can be linked,
   bookmarked and reloaded:

     #/                        the box
     #/effect                  a divider: every crawled version of it
     #/effect/v3               a version: its index of pages
     #/effect/v3/17            one page, open

   The box grows with every harvest, so the divider list is read a page at a
   time from the server rather than loaded whole. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const stageEl = $("#stage");
const dividersEl = $("#dividers");
const pagerEl = $("#pager");
const pgCountEl = $("#pg-count");
const backendEl = $("#backend");
const findTechEl = $("#find-tech");

const state = {
  page: 1,
  pages: 1,
  total: 0,
  query: "",
  technologies: [],
  backend: null,

  tech: null,        // the open divider
  versions: [],
  version: null,     // the open version
  meta: null,
  pageList: [],      // its index of pages
  ordinal: null,     // the open page
  doc: null,         // …once fetched

  find: "",          // search inside the open version
  hits: null,        // null = not searching; [] = searched, nothing found
};

// ── small helpers ────────────────────────────────────────
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function icon(id, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls ? `icon ${cls}` : "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

/** Bytes as something a human reads at a glance. */
function size(chars) {
  if (!chars) return "0 B";
  if (chars < 1024) return `${chars} B`;
  if (chars < 1024 * 1024) return `${(chars / 1024).toFixed(0)} KB`;
  return `${(chars / 1048576).toFixed(1)} MB`;
}

function plural(n, one, many) {
  return `${n.toLocaleString()} ${n === 1 ? one : many || one + "s"}`;
}

async function api(path) {
  const r = await fetch(path);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${r.status} ${r.statusText}`);
  return data;
}

/** A divider standing in the box, with one card edge behind it per extra
    version. The tab matters: a plain rectangle at one version reads as an
    empty checkbox, which invites a click that does nothing. */
function spine(count) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "spine");
  svg.setAttribute("viewBox", "0 0 22 26");
  svg.setAttribute("aria-hidden", "true");

  const add = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    svg.append(n);
  };

  for (let i = 1; i <= Math.min(Math.max((count || 1) - 1, 0), 3); i++) {
    add("path", { d: `M${14.5 + i * 2} 8.5V22.5` });
  }
  add("rect", { x: 1.5, y: 6.5, width: 12, height: 17 });
  add("path", { d: "M4 6.5V3.5h5v3" });
  return svg;
}

/* The store marks matches with « » rather than markup, so a page full of
   angle brackets cannot smuggle HTML into the index. Escape first, then
   promote the guillemets. */
function highlight(text) {
  const span = el("span", "snip");
  const parts = String(text || "").split(/«|»/);
  parts.forEach((part, i) => {
    if (i % 2) span.append(Object.assign(el("mark"), { textContent: part }));
    else span.append(document.createTextNode(part));
  });
  return span;
}

// ── routing ──────────────────────────────────────────────
function route() {
  const raw = location.hash.replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  return {
    tech: parts[0] || null,
    version: parts[1] || null,
    ordinal: parts[2] ? parseInt(parts[2], 10) : null,
  };
}

function go(tech, version, ordinal) {
  const parts = [tech, version, ordinal].filter((p) => p !== null && p !== undefined);
  const hash = "#/" + parts.map(encodeURIComponent).join("/");
  if (location.hash === hash) open();
  else location.hash = hash;
}

// ── the box: the paged divider list ──────────────────────
async function loadBox() {
  dividersEl.setAttribute("aria-busy", "true");
  try {
    const data = await api(
      `/api/library?page=${state.page}&q=${encodeURIComponent(state.query)}`);
    state.technologies = data.technologies;
    state.page = data.page;
    state.pages = data.pages;
    state.total = data.total;
    state.backend = data.backend;
    showBackend();
  } catch (e) {
    state.technologies = [];
    state.pages = 1;
    state.total = 0;
    backendEl.textContent = "the box is unreachable";
  }
  dividersEl.setAttribute("aria-busy", "false");
  renderBox();
}

function showBackend() {
  const b = state.backend;
  if (!b) return;
  backendEl.textContent = b.kind === "postgres" ? b.location : "files · " + b.location;
  backendEl.classList.toggle("files", b.kind !== "postgres");
  backendEl.title = b.kind === "postgres"
    ? `Stored in Postgres at ${b.location} — search is ranked across every page.`
    : `Stored as Markdown files in ${b.location} — set DOCSFORGE_DB for ranked search.`;
}

function renderBox() {
  dividersEl.replaceChildren();

  if (!state.technologies.length) {
    const empty = el("li", "index-empty");
    if (state.query) {
      empty.append(el("p", null, `Nothing in the box matches “${state.query}”.`));
      empty.append(el("p", "dim", `${plural(state.total, "technology", "technologies")} stored.`));
    } else {
      const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      art.setAttribute("class", "shoebox-art");
      art.setAttribute("viewBox", "0 0 120 92");
      art.setAttribute("aria-hidden", "true");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#i-shoebox");
      art.append(use);
      empty.append(art);
      empty.append(el("p", null, "The box is empty."));
      empty.append(el("p", "dim", "Harvest a technology in the chat and it gets filed here."));
    }
    dividersEl.append(empty);
    renderPager();
    return;
  }

  state.technologies.forEach((t) => {
    const li = el("li");
    const btn = el("button", "index-card");
    btn.type = "button";
    if (t.name === state.tech) btn.setAttribute("aria-current", "true");

    btn.append(spine(t.versions));

    const text = el("div", "index-text");
    text.append(el("span", "t", t.name));

    const m = el("span", "m");
    m.append(el("span", null, plural(t.versions, "version")));
    m.append(Object.assign(el("span", "num"), { textContent: plural(t.pages, "page") }));
    text.append(m);

    text.append(el("span", "v", `${t.latest || "—"} · ${size(t.characters)}`));
    if (!t.complete) text.append(Object.assign(el("span", "warn"), { textContent: "partial" }));

    btn.append(text);
    btn.addEventListener("click", () => go(t.name));
    li.append(btn);
    dividersEl.append(li);
  });

  // Arriving on a link straight to a technology should show which divider is
  // open, even when it sits below the fold of a full page of them.
  const current = $('#dividers [aria-current="true"]');
  if (current) current.scrollIntoView({ block: "nearest" });

  renderPager();
}

function renderPager() {
  const prev = $('[data-page="prev"]', pagerEl);
  const next = $('[data-page="next"]', pagerEl);
  prev.disabled = state.page <= 1;
  next.disabled = state.page >= state.pages;
  pgCountEl.textContent = state.total
    ? `page ${state.page} of ${state.pages} · ${state.total}`
    : "empty";
}

// ── opening a divider ────────────────────────────────────
async function open() {
  const r = route();

  if (!r.tech) {
    state.tech = state.version = state.ordinal = null;
    state.versions = [];
    renderBox();
    return renderEmptyStage();
  }

  if (r.tech !== state.tech) {
    state.tech = r.tech;
    state.versions = [];
    state.version = null;
    renderBox();
    try {
      state.versions = (await api(`/api/library/${encodeURIComponent(r.tech)}`)).versions;
    } catch (e) {
      return renderError(`Nothing in the box is filed under “${r.tech}”.`, e.message);
    }
  }

  if (!r.version) {
    state.version = state.ordinal = null;
    return renderVersions();
  }

  if (r.version !== state.version) {
    state.version = r.version;
    state.ordinal = null;
    state.find = "";
    state.hits = null;
    try {
      const data = await api(
        `/api/library/${encodeURIComponent(r.tech)}/${encodeURIComponent(r.version)}`);
      state.pageList = data.pages;
      state.meta = data.meta;
    } catch (e) {
      return renderError(`${r.tech} has no version “${r.version}”.`, e.message);
    }
    renderReader();
  }

  state.ordinal = r.ordinal || null;
  if (state.ordinal) {
    await loadPage(state.ordinal);
  } else {
    state.doc = null;
    renderPageView();
    markToc();
  }
}

// ── the stage: three states ──────────────────────────────
/** Paint a card onto the stage. `hollow` is for cards with nothing in them:
    they stretch to the stage instead of hugging content that isn't there. */
function card(title, { hollow = false } = {}, ...rest) {
  const art = el("article", hollow ? "card open painted hollow" : "card open painted");

  const head = el("header", "card-head");
  head.append(Object.assign(el("h1", "card-title"), { textContent: title }));
  const boxes = el("span", "boxes");
  boxes.setAttribute("aria-hidden", "true");
  boxes.append(el("i"), el("i"), el("i"));
  head.append(boxes);
  art.append(head);
  art.append(...rest);

  stageEl.classList.remove("dissolving");
  void stageEl.offsetWidth;          // restart the dissolve
  stageEl.replaceChildren(art);
  stageEl.classList.add("dissolving");
  return art;
}

function renderEmptyStage() {
  const field = el("div", "card-field");
  const md = el("div", "md");
  const blank = el("div", "index-empty");

  const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  art.setAttribute("class", "shoebox-art");
  art.setAttribute("viewBox", "0 0 120 92");
  art.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-shoebox");
  art.append(use);

  blank.append(art);
  blank.append(el("p", null, "Pick a technology from the box."));
  blank.append(el("p", "dim",
    "Each divider holds every version of that documentation that has been crawled."));
  md.append(blank);
  field.append(md);
  card("The box", { hollow: true }, field);
}

function renderError(message, detail) {
  const field = el("div", "card-field");
  const md = el("div", "md");
  md.append(el("p", null, message));
  if (detail) md.append(Object.assign(el("p", "dim"), { textContent: detail }));
  const back = el("button", "btn painted");
  back.type = "button";
  back.append(icon("i-back"), el("span", null, "Back to the box"));
  back.addEventListener("click", () => go(null));
  md.append(back);
  field.append(md);
  card("Not in the box", { hollow: true }, field);
}

/** The divider opened: every crawl of this technology, with its date. */
function renderVersions() {
  const meta = el("div", "card-meta");
  meta.append(el("span", null, plural(state.versions.length, "version")));
  meta.append(el("span", null, plural(
    state.versions.reduce((n, v) => n + v.pages, 0), "page")));
  meta.append(el("span", null, size(
    state.versions.reduce((n, v) => n + v.characters, 0))));

  const field = el("div", "card-field");
  const list = el("ol", "versions");

  state.versions.forEach((v) => {
    const li = el("li");
    const btn = el("button", "version");
    btn.type = "button";
    btn.append(el("span", "tag", v.version));

    const facts = el("div", "facts");
    facts.append(el("span", null, plural(v.pages, "page")));
    facts.append(el("span", null, size(v.characters)));
    facts.append(el("span", null, `harvested ${v.harvested}`));
    facts.append(el("span", null, `via ${v.strategy}`));
    if (!v.complete) facts.append(Object.assign(el("span", "warn"),
      { textContent: "partial harvest" }));
    facts.append(Object.assign(el("span", "src"), { textContent: v.source }));
    btn.append(facts);
    btn.append(icon("i-arrow", "go"));

    btn.addEventListener("click", () => go(state.tech, v.version));
    li.append(btn);
    list.append(li);
  });

  field.append(list);
  card(state.tech, {}, meta, field);
}

/** Inside a version: the page index beside the page. */
function renderReader() {
  const tabs = el("div", "tabs");
  tabs.append(el("span", "chip", "Versions"));
  state.versions.forEach((v) => {
    const t = el("button", "tab");
    t.type = "button";
    t.textContent = v.version;
    t.title = `${plural(v.pages, "page")}, harvested ${v.harvested}`;
    if (v.version === state.version) t.setAttribute("aria-current", "true");
    t.addEventListener("click", () => go(state.tech, v.version));
    tabs.append(t);
  });

  const meta = el("div", "card-meta");
  const crumb = el("nav", "crumb");
  crumb.setAttribute("aria-label", "Breadcrumb");
  const box = el("button", null, "the box");
  box.type = "button";
  box.addEventListener("click", () => go(null));
  crumb.append(box, el("span", "sep", "›"));
  const back = el("button", null, state.tech);
  back.type = "button";
  back.addEventListener("click", () => go(state.tech));
  crumb.append(back, el("span", "sep", "›"));
  crumb.append(el("span", "here", state.version));
  meta.append(crumb);

  const m = state.meta || {};
  meta.append(el("span", null, plural(state.pageList.length, "page")));
  meta.append(el("span", null, size(m.characters || 0)));
  meta.append(el("span", null, m.harvested || ""));
  if (m.complete === false) {
    meta.append(Object.assign(el("span", "warn"), { textContent: "partial harvest" }));
  }

  const reader = el("div", "reader");

  const contents = el("div", "contents");
  const form = el("form", "find");
  form.setAttribute("role", "search");
  const label = el("label", "sr", "Search inside this version");
  label.htmlFor = "find-text";
  const wrap = el("div", "find-field");
  wrap.append(icon("i-find"));
  const input = el("input");
  input.id = "find-text";
  input.type = "search";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.placeholder = "search these pages…";
  input.value = state.find;
  wrap.append(input);
  form.append(label, wrap);
  form.addEventListener("submit", (e) => e.preventDefault());
  input.addEventListener("input", () => queueFind(input.value));
  contents.append(form);

  const toc = el("ol", "toc");
  toc.id = "toc";
  contents.append(toc);

  const view = el("div", "page-view");
  view.id = "page-view";

  reader.append(contents, view);
  card(`${state.tech} ${state.version}`, {}, tabs, meta, reader);

  renderToc();
  renderPageView();
}

// ── the index of pages, and searching it ─────────────────
let findTimer = null;

function queueFind(value) {
  state.find = value;
  clearTimeout(findTimer);
  findTimer = setTimeout(runFind, 220);
}

async function runFind() {
  const q = state.find.trim();
  if (!q) {
    state.hits = null;
    return renderToc();
  }
  try {
    const data = await api(
      `/api/library-search?q=${encodeURIComponent(q)}` +
      `&tech=${encodeURIComponent(state.tech)}` +
      `&version=${encodeURIComponent(state.version)}&limit=60`);
    state.hits = data.hits;
  } catch (e) {
    state.hits = [];
  }
  renderToc();
}

function renderToc() {
  const toc = $("#toc");
  if (!toc) return;
  toc.replaceChildren();

  const rows = state.hits === null
    ? state.pageList.map((p) => ({ ...p, snippet: null }))
    : state.hits;

  if (!rows.length) {
    const li = el("li");
    li.append(el("p", "none", state.hits === null
      ? "This version has no pages stored."
      : `No page in ${state.tech} ${state.version} mentions “${state.find.trim()}”.`));
    toc.append(li);
    return;
  }

  if (state.hits !== null) {
    const li = el("li");
    li.append(el("p", "none",
      `${plural(rows.length, "page")} matching “${state.find.trim()}”`));
    toc.append(li);
  }

  rows.forEach((p) => {
    const li = el("li");
    const btn = el("button");
    btn.type = "button";
    btn.dataset.ordinal = String(p.ordinal);
    if (p.ordinal === state.ordinal) btn.setAttribute("aria-current", "true");
    btn.append(el("span", "n", String(p.ordinal)));
    const nm = el("span", "nm");
    nm.append(document.createTextNode(p.title));
    if (p.snippet) nm.append(highlight(p.snippet));
    btn.append(nm);
    btn.addEventListener("click", () => go(state.tech, state.version, p.ordinal));
    li.append(btn);
    toc.append(li);
  });
}

function markToc() {
  $$("#toc button").forEach((b) => {
    if (Number(b.dataset.ordinal) === state.ordinal) {
      b.setAttribute("aria-current", "true");
      b.scrollIntoView({ block: "nearest" });
    } else {
      b.removeAttribute("aria-current");
    }
  });
}

// ── the page itself ──────────────────────────────────────
async function loadPage(ordinal) {
  const view = $("#page-view");
  if (view) view.replaceChildren(el("p", "blank", "Reading…"));
  try {
    state.doc = await api(
      `/api/library/${encodeURIComponent(state.tech)}/` +
      `${encodeURIComponent(state.version)}/page/${ordinal}`);
  } catch (e) {
    state.doc = null;
    if (view) view.replaceChildren(el("p", "blank", e.message));
    return;
  }
  renderPageView();
  markToc();
}

function renderPageView() {
  const view = $("#page-view");
  if (!view) return;
  view.replaceChildren();

  if (!state.doc) {
    const blank = el("div", "blank");
    blank.append(el("p", null, `${plural(state.pageList.length, "page")} in `
      + `${state.tech} ${state.version}.`));
    blank.append(el("p", "dim", "Pick one from the index to read it."));
    view.append(blank);
    return;
  }

  const p = state.doc;
  const head = el("div", "page-head");
  head.append(Object.assign(el("h2"), { textContent: p.title }));
  if (p.url) {
    const src = el("p", "src");
    const a = el("a", null, p.url);
    a.href = p.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    src.append(a);
    head.append(src);
  }
  view.append(head);

  // Sanitised server-side by the same nh3 pass the chat cards use.
  const md = el("div", "md");
  md.innerHTML = p.html;
  view.append(md);
  view.scrollTop = 0;
}

// ── menus ────────────────────────────────────────────────
function closeMenus() {
  $$(".menu").forEach((m) => {
    m.dataset.open = "false";
    $(".menu-title", m).setAttribute("aria-expanded", "false");
  });
}

function refreshMenus() {
  const hasPage = !!state.doc;
  const hasVersion = !!state.version;
  const set = (act, on) => {
    const b = $(`[data-act="${act}"]`);
    if (b) b.disabled = !on;
  };
  set("download-page", hasPage);
  set("copy-page", hasPage);
  set("download-version", hasVersion);
  set("source", hasPage && !!state.doc.url);
  set("box", !!state.tech);
  set("find-text", hasVersion);
  set("clear-find", !!state.find || !!state.query);
}

function wireMenus() {
  $$(".menu").forEach((menu) => {
    const title = $(".menu-title", menu);
    title.addEventListener("click", (e) => {
      e.stopPropagation();
      const open_ = menu.dataset.open === "true";
      closeMenus();
      if (!open_) {
        menu.dataset.open = "true";
        title.setAttribute("aria-expanded", "true");
        refreshMenus();
      }
    });
    title.addEventListener("mouseenter", () => {
      if ($$('.menu[data-open="true"]').length) {
        closeMenus();
        menu.dataset.open = "true";
        title.setAttribute("aria-expanded", "true");
        refreshMenus();
      }
    });
  });

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".menu-drop button");
    if (btn && !btn.disabled) {
      closeMenus();
      runAction(btn.dataset.act);
      return;
    }
    if (!e.target.closest(".menu")) closeMenus();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeMenus(); return; }
    if (!(e.ctrlKey || e.metaKey)) return;
    const k = e.key.toLowerCase();
    if (k === "f" && state.version) { e.preventDefault(); runAction("find-text"); }
    if (k === "s" && state.doc) { e.preventDefault(); runAction("download-page"); }
  });
}

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function pageMarkdown(p) {
  return `# ${p.title}\n\nSource: <${p.url}>\n\n${p.content}\n`;
}

async function runAction(act) {
  const p = state.doc;
  switch (act) {
    case "chat":
      location.href = "/";
      return;
    case "box":
      return go(null);
    case "source":
      if (p && p.url) window.open(p.url, "_blank", "noopener");
      return;
    case "copy-page":
      if (p) await navigator.clipboard.writeText(pageMarkdown(p));
      return;
    case "download-page":
      if (p) download(`${state.tech}-${state.version}-${p.ordinal}.md`, pageMarkdown(p));
      return;
    case "download-version":
      return downloadVersion();
    case "find-tech":
      findTechEl.focus();
      findTechEl.select();
      return;
    case "find-text": {
      const f = $("#find-text");
      if (f) { f.focus(); f.select(); }
      return;
    }
    case "clear-find": {
      state.find = "";
      state.hits = null;
      const f = $("#find-text");
      if (f) f.value = "";
      renderToc();
      if (state.query) {
        state.query = "";
        findTechEl.value = "";
        state.page = 1;
        loadBox();
      }
      return;
    }
  }
}

/** Every page of the open version, as the one Markdown file the harvest
    produced. Fetched page by page so the server never has to hold it all. */
async function downloadVersion() {
  const btn = $('[data-act="download-version"]');
  if (btn) btn.disabled = true;
  const parts = [
    `# ${state.tech} ${state.version} documentation`, "",
    `<!-- ${state.pageList.length} pages | from: ${(state.meta || {}).source || ""} ` +
    `| harvested: ${(state.meta || {}).harvested || ""} -->`, "", "## Contents", "",
  ];
  state.pageList.forEach((p, i) => parts.push(`${i + 1}. ${p.title}`));

  for (const p of state.pageList) {
    try {
      const full = await api(
        `/api/library/${encodeURIComponent(state.tech)}/` +
        `${encodeURIComponent(state.version)}/page/${p.ordinal}`);
      parts.push("", "---", "", `## ${full.title}`, "", `Source: <${full.url}>`, "",
        full.content);
    } catch (e) {
      parts.push("", "---", "", `## ${p.title}`, "", `<!-- could not be read: ${e.message} -->`);
    }
  }
  download(`${state.tech}-${state.version}.md`, parts.join("\n") + "\n");
  if (btn) btn.disabled = false;
}

// ── boot ─────────────────────────────────────────────────
let techTimer = null;
findTechEl.addEventListener("input", () => {
  clearTimeout(techTimer);
  techTimer = setTimeout(() => {
    state.query = findTechEl.value.trim();
    state.page = 1;
    loadBox();
  }, 220);
});
$("#find-form").addEventListener("submit", (e) => e.preventDefault());

pagerEl.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-page]");
  if (!btn || btn.disabled) return;
  state.page += btn.dataset.page === "next" ? 1 : -1;
  state.page = Math.min(Math.max(1, state.page), state.pages);
  loadBox();
});

window.addEventListener("hashchange", open);

(function labelShortcuts() {
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  $$(".menu-drop .key[data-key]").forEach((k) => {
    k.textContent = (mac ? "⌘" : "Ctrl+") + k.dataset.key;
  });
})();

wireMenus();
loadBox().then(open);
