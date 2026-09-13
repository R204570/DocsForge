/* DocsStore — everything DocsForge has harvested.

   Three levels, and the URL names all three, so any view can be linked,
   bookmarked and reloaded:

     #/                        the list
     #/effect                  one technology: every crawled version of it
     #/effect/v3               a version: its index of pages
     #/effect/v3/17            one page, open

   The list grows with every harvest, so it is read a page at a time from
   the server rather than loaded whole. */

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

  tech: null,        // the open technology
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

async function api(path, method = "GET") {
  const r = await fetch(path, { method });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${r.status} ${r.statusText}`);
  return data;
}

/** A destructive button that asks once before doing anything.

    Deleting a harvest cannot be undone and re-harvesting means crawling the
    site again, so it takes two clicks — but a modal for it would be heavier
    than the act deserves. The button becomes the confirmation, and reverts on
    its own if you walk away. */
function arming(button, label, confirmLabel, run) {
  let armed = 0;
  const disarm = () => {
    armed = 0;
    button.classList.remove("armed");
    button.replaceChildren(icon("i-trash", "sm"), el("span", null, label));
    button.title = label;
  };
  disarm();
  button.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!armed) {
      armed = window.setTimeout(disarm, 4000);
      button.classList.add("armed");
      button.replaceChildren(icon("i-trash", "sm"), el("span", null, confirmLabel));
      button.title = confirmLabel;
      return;
    }
    window.clearTimeout(armed);
    armed = 0;
    button.disabled = true;
    button.replaceChildren(el("span", null, "Deleting…"));
    try {
      await run();
    } catch (err) {
      button.disabled = false;
      disarm();
      renderError("Could not delete that", err.message);
    }
  });
  return button;
}

/* ── right-click a technology ─────────────────────────────
   Deleting a whole technology was reachable only from inside it: open the
   technology, scroll past its versions, then the wide button at the foot.
   That is three steps to undo one mistaken harvest, and nothing on the
   listing said the option existed. Right-click is where people already
   look for "do something to this row". */
let menuEl = null;

function closeMenu() {
  if (menuEl) {
    menuEl.remove();
    menuEl = null;
  }
}

function openMenu(x, y, tech) {
  closeMenu();
  const menu = el("div", "row-menu");
  menu.setAttribute("role", "menu");

  const title = el("div", "row-menu-title", tech.name);
  menu.append(title);

  // Not named `open`: that is the module's own refresh function, and
  // shadowing it here is how the delete below would silently stop
  // redrawing the listing.
  const openItem = el("button", "row-menu-item");
  openItem.type = "button";
  openItem.setAttribute("role", "menuitem");
  openItem.append(el("span", null, "Open"));
  openItem.addEventListener("click", () => {
    closeMenu();
    go(tech.name);
  });
  menu.append(openItem);

  menu.append(arming(
    Object.assign(el("button", "row-menu-item danger"), { type: "button" }),
    `Delete all ${plural(tech.versions, "version")}`,
    "Really delete everything?",
    async () => {
      await api(`/api/library/${encodeURIComponent(tech.name)}`, "DELETE");
      closeMenu();
      // Deleting the technology currently open would leave the detail pane
      // showing something that no longer exists.
      if (state.tech === tech.name) return go(null);
      return open();
    }));

  document.body.append(menu);
  menuEl = menu;

  // Placed after insertion so the real size is known: a menu that opens
  // past the bottom of a long listing is a menu you cannot use.
  const box = menu.getBoundingClientRect();
  const left = Math.min(x, window.innerWidth - box.width - 8);
  const top = Math.min(y, window.innerHeight - box.height - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  menu.querySelector("button").focus();
}

document.addEventListener("click", (e) => {
  if (menuEl && !e.target.closest(".row-menu")) closeMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenu();
});
window.addEventListener("resize", closeMenu);
window.addEventListener("scroll", closeMenu, true);

/** How many crawls of this technology the store holds, drawn as stacked
    card edges. The tab matters: a plain rectangle at one version reads as
    an empty checkbox, which invites a click that does nothing. */
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

// ── the paged technology list ────────────────────────────
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
    backendEl.textContent = "store unreachable";
  }
  dividersEl.setAttribute("aria-busy", "false");
  renderBox();
}

function showBackend() {
  const b = state.backend;
  if (!b) return;

  // A configured-but-unreachable database is the one case where an empty
  // store does not mean "nothing harvested", so it says so instead of
  // quietly showing you nothing.
  if (b.degraded) {
    backendEl.textContent = "database unreachable";
    backendEl.classList.remove("files");
    backendEl.classList.add("broken");
    backendEl.title =
      `${b.degraded}\n\nShowing Markdown files in ${b.location} instead. `
      + `Anything harvested into the database is still there — start Postgres `
      + `and reload.`;
    return;
  }

  backendEl.classList.remove("broken");
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
      empty.append(el("p", null, `No technology matches “${state.query}”.`));
      empty.append(el("p", "dim", `${plural(state.total, "technology", "technologies")} stored.`));
    } else {
      const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      art.setAttribute("class", "empty-art");
      art.setAttribute("viewBox", "0 0 24 24");
      art.setAttribute("aria-hidden", "true");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#i-store");
      art.append(use);
      empty.append(art);
      empty.append(el("p", null, "Nothing harvested yet."));
      empty.append(el("p", "dim", "Ask for a library's documentation in the chat and it lands here."));
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
    btn.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      openMenu(e.clientX, e.clientY, t);
    });
    // Right-click is not reachable from a keyboard, and neither is a
    // trackpad without a second button configured. The context-menu key
    // and Shift+F10 are what those users press instead.
    btn.addEventListener("keydown", (e) => {
      if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
        e.preventDefault();
        const box = btn.getBoundingClientRect();
        openMenu(box.left + 24, box.bottom - 8, t);
      }
    });
    li.append(btn);
    dividersEl.append(li);
  });

  // Arriving on a link straight to a technology should show which row is
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

// ── opening a technology ─────────────────────────────────
async function open() {
  const r = route();

  if (!r.tech) {
    state.tech = state.version = state.ordinal = null;
    state.versions = [];
    renderBox();
    renderActs();
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
      return renderError(`Nothing is stored under “${r.tech}”.`, e.message);
    }
  }

  if (!r.version) {
    state.version = state.ordinal = null;
    renderActs();
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
/** Paint a panel onto the stage. `hollow` is for panels with nothing in them:
    they centre what little they have instead of hugging content that isn't
    there. */
function card(title, { hollow = false } = {}, ...rest) {
  const art = el("article", hollow ? "card hollow" : "card");

  const head = el("header", "card-head");
  head.append(Object.assign(el("h1", "card-title"), { textContent: title }));
  art.append(head);
  art.append(...rest);

  stageEl.replaceChildren(art);
  return art;
}

function renderEmptyStage() {
  const field = el("div", "card-field");
  const md = el("div", "md");
  const blank = el("div", "index-empty");

  const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  art.setAttribute("class", "empty-art");
  art.setAttribute("viewBox", "0 0 24 24");
  art.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-store");
  art.append(use);

  blank.append(art);
  blank.append(el("p", null, "Pick a technology."));
  blank.append(el("p", "dim",
    "Each one holds every version of its documentation that has been crawled."));
  md.append(blank);
  field.append(md);
  card("DocsStore", { hollow: true }, field);
}

function renderError(message, detail) {
  const field = el("div", "card-field");
  const md = el("div", "md");
  md.append(el("p", null, message));
  if (detail) md.append(Object.assign(el("p", "dim"), { textContent: detail }));
  const back = el("button", "pill");
  back.type = "button";
  back.append(icon("i-back"), el("span", null, "Back to the list"));
  back.addEventListener("click", () => go(null));
  md.append(back);
  field.append(md);
  card("Not found", { hollow: true }, field);
}

/** One technology opened: every crawl of it, with its date. */
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
    const row = el("div", "version-row");
    const btn = el("button", "version");
    btn.type = "button";
    btn.append(el("span", "tag", v.version));

    const facts = el("div", "facts");
    facts.append(el("span", null, plural(v.pages, "page")));
    facts.append(el("span", null, size(v.characters)));
    facts.append(el("span", null, `harvested ${v.harvested}`));
    facts.append(el("span", null, `via ${v.strategy}`));
    // Three states, not two: `null` means nothing counted how much there was
    // to get, which is a different warning from a copy known to be partial.
    if (v.complete === false) facts.append(Object.assign(el("span", "warn"),
      { textContent: "partial harvest" }));
    else if (v.complete === null || v.complete === undefined) {
      facts.append(Object.assign(el("span", "warn"),
        { textContent: "coverage unknown" }));
    }
    facts.append(Object.assign(el("span", "src"), { textContent: v.source }));
    btn.append(facts);
    btn.append(icon("i-arrow", "go"));

    btn.addEventListener("click", () => go(state.tech, v.version));
    row.append(btn);
    row.append(arming(
      Object.assign(el("button", "forget"), { type: "button" }),
      `Delete ${v.version}`, "Really delete?",
      async () => {
        await api(`/api/library/${encodeURIComponent(state.tech)}`
                  + `/${encodeURIComponent(v.version)}`, "DELETE");
        // Last one gone means the technology is gone; go back to the box.
        if (state.versions.length <= 1) return go(null);
        return open();
      }));
    li.append(row);
    list.append(li);
  });

  field.append(list);

  const foot = el("div", "card-foot");
  foot.append(arming(
    Object.assign(el("button", "forget wide"), { type: "button" }),
    `Delete ${state.tech} and all ${plural(state.versions.length, "version")}`,
    "Really delete everything?",
    async () => {
      await api(`/api/library/${encodeURIComponent(state.tech)}`, "DELETE");
      return go(null);
    }));
  field.append(foot);

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
  const box = el("button", null, "DocsStore");
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
  renderActs();
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
  // An API reference table can be far wider than the reader. Let it scroll
  // inside its own box rather than push the whole page sideways.
  $$("table", md).forEach((table) => {
    const box = el("div", "scroll-x");
    table.replaceWith(box);
    box.append(table);
  });
  view.append(md);
  view.scrollTop = 0;
}

// ── what you can do with what is open ─────────────────
/* Buttons appear in the top bar as they become possible, rather than sitting
   there greyed out: on this screen "nothing is open yet" is the common state,
   and a row of dead controls is not information. */
function renderActs() {
  const acts = $("#page-acts");
  if (!acts) return;
  acts.replaceChildren();

  const add = (label, iconId, act, title) => {
    const b = el("button", "ghost");
    b.type = "button";
    b.dataset.act = act;
    b.title = title || label;
    b.append(icon(iconId, "sm"), el("span", null, label));
    b.addEventListener("click", () => runAction(act));
    acts.append(b);
    return b;
  };

  if (state.doc) {
    add("Copy", "i-copy", "copy-page", "Copy this page as Markdown");
    add("Download", "i-save", "download-page", "Download this page (Ctrl+S)");
    if (state.doc.url) add("Source", "i-open", "source", "Open the original page");
  } else if (state.version) {
    add("Download version", "i-save", "download-version",
        "Every page of this version as one Markdown file");
  }
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
  // 700 pages is a real wait, so the button reports where it has got to
  // instead of looking like nothing happened.
  const btn = $('[data-act="download-version"]');
  const label = btn && $("span", btn);
  const say = (t) => { if (label) label.textContent = t; };
  if (btn) btn.disabled = true;

  const parts = [
    `# ${state.tech} ${state.version} documentation`, "",
    `<!-- ${state.pageList.length} pages | from: ${(state.meta || {}).source || ""} ` +
    `| harvested: ${(state.meta || {}).harvested || ""} -->`, "", "## Contents", "",
  ];
  state.pageList.forEach((p, i) => parts.push(`${i + 1}. ${p.title}`));

  let done = 0;
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
    done += 1;
    if (done % 10 === 0) say(`${done} / ${state.pageList.length}`);
  }
  download(`${state.tech}-${state.version}.md`, parts.join("\n") + "\n");
  say("Download version");
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

// A background harvest finishing is the one thing that changes this listing
// without the user doing anything, and it used to appear only on a manual
// reload -- the documentation simply materialising some minutes later.
window.addEventListener("docsforge:harvest-finished", () => { open(); });

document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (e.key === "Escape" && typing) { document.activeElement.blur(); return; }
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = e.key.toLowerCase();
  if (k === "f") {
    e.preventDefault();
    runAction(state.version ? "find-text" : "find-tech");
  } else if (k === "s" && state.doc) {
    e.preventDefault();
    runAction("download-page");
  } else if (k === "l") {
    e.preventDefault();
    location.href = "/";
  }
});

loadBox().then(open);
