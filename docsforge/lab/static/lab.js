/* ─────────────────────────────────────────────────────────
   DocsForge lab — one page, drawn from /api/lab.

   Two dashboards behind one sign-in: a tester lands on the bench (run a
   test, watch the tools it calls, give a verdict); an admin lands on the
   overview (what is stored, what testers found, what the tools did).

   Everything that came from a site, a model or a tester is inserted as
   text. The only HTML set directly is what the server rendered and
   sanitised (nh3) from Markdown.
   ───────────────────────────────────────────────────────── */
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const FINISHED = new Set(["done", "failed", "cancelled"]);
  const LANGUAGES = ["javascript", "python", "go", "rust", "java", "kotlin", "csharp",
    "ruby", "php", "swift", "dart", "elixir", "cpp"];
  const S = { user: null, setup: false, recording: true, counts: {}, tools: null };
  let VIEW = { timers: [], alive: true };

  // ── building DOM ──────────────────────────────────────────
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    setAttrs(el, attrs);
    add(el, kids);
    return el;
  }
  function svg(tag, attrs, ...kids) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
    add(el, kids);
    return el;
  }
  function setAttrs(el, attrs) {
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "html") el.innerHTML = v;            // server-sanitised only
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "style") Object.assign(el.style, v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    }
  }
  /** replaceChildren that skips null and flattens arrays, as h() does. */
  function put(el, ...kids) {
    el.replaceChildren();
    add(el, kids);
    return el;
  }
  function add(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      el.append(k instanceof Node ? k : String(k));
    }
  }
  function icon(name, cls = "icon") {
    const el = svg("svg", { class: cls, "aria-hidden": "true" });
    const use = document.createElementNS(NS, "use");
    use.setAttribute("href", "#i-" + name);
    el.append(use);
    return el;
  }
  // Addresses come from scraped pages and registries: only http(s) becomes a link.
  const ext = (href, label, cls) => /^https?:\/\//i.test(href || "")
    ? h("a", { href, target: "_blank", rel: "noopener noreferrer", class: cls }, label || href, " ", icon("out", "icon sm"))
    : h("span", { class: cls, text: label || href || "" });

  // ── formatting ────────────────────────────────────────────
  const n = (v) => (v == null ? "—" : Number(v).toLocaleString("en-US"));
  function compact(v) {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
    return n(v);
  }
  function bytes(v) {
    if (v == null) return "—";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i ? v.toFixed(1) : v) + " " + u[i];
  }
  function ago(ts) {
    if (!ts) return "—";
    const s = Date.now() / 1000 - ts;
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    if (s < 86400 * 7) return Math.round(s / 86400) + " d ago";
    return when(ts);
  }
  function when(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit" });
  }
  function secs(v) {
    if (v == null) return "—";
    if (v < 1) return Math.round(v * 1000) + " ms";
    if (v < 90) return v.toFixed(1) + " s";
    if (v < 5400) return Math.round(v / 60) + " min";
    return (v / 3600).toFixed(1) + " h";
  }
  const ms = (v) => (v == null ? "—" : secs(v / 1000));
  const pct = (good, total) => (total ? Math.round((100 * good) / total) + "%" : "—");

  // ── talking to the server ─────────────────────────────────
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }
  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (init.method !== "GET") init.headers["x-docsforge-lab"] = "1";
    if (opts.body !== undefined) {
      init.headers["content-type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    const type = res.headers.get("content-type") || "";
    const data = type.includes("json") ? await res.json() : await res.text();
    if (res.status === 401 && !opts.anon) {
      S.user = null;
      boot();
      throw new ApiError("Signed out.", 401);
    }
    if (!res.ok) throw new ApiError((data && data.detail) || res.statusText || "Request failed", res.status);
    return data;
  }
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v != null && v !== false)
    .map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v === true ? 1 : v)).join("&");

  // ── toast and tooltip ─────────────────────────────────────
  let toastTimer = 0;
  function toast(message, { bad = false, link = null } = {}) {
    const el = document.getElementById("toast");
    el.className = "toast" + (bad ? " bad" : "");
    put(el, message, link ? h("a", { href: link[1], text: link[0] }) : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, link ? 9000 : 4200);
  }
  const fail = (e) => { if (!(e instanceof ApiError && e.status === 401)) toast(e.message || String(e), { bad: true }); };

  function showTip(target, title, rows) {
    const tip = document.getElementById("tip");
    put(tip, h("b", { text: title }),
      ...rows.map(([k, v]) => h("div", { class: "row" }, h("span", { text: k }), h("span", { text: v }))));
    tip.hidden = false;
    const r = target.getBoundingClientRect();
    const w = tip.offsetWidth, ht = tip.offsetHeight;
    let x = r.left + r.width / 2 - w / 2;
    x = Math.max(8, Math.min(x, window.innerWidth - w - 8));
    let y = r.top - ht - 8;
    if (y < 8) y = r.bottom + 8;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  const hideTip = () => { document.getElementById("tip").hidden = true; };

  // ── view lifecycle ────────────────────────────────────────
  function every(msInterval, fn) {
    const view = VIEW;
    const id = setInterval(() => {
      if (!view.alive || document.hidden) return;
      Promise.resolve(fn()).catch(() => {});
    }, msInterval);
    view.timers.push(id);
    return () => clearInterval(id);
  }
  function leaveView() {
    VIEW.alive = false;
    VIEW.timers.forEach(clearInterval);
    VIEW = { timers: [], alive: true };
    hideTip();
    closeDrawer();
  }

  // ── the gate ──────────────────────────────────────────────
  async function boot() {
    leaveView();
    let state;
    try {
      state = await api("/api/lab/state", { anon: true });
    } catch (e) {
      put(document.getElementById("app"), gateCard("The lab could not start",
        e.message, []));
      return;
    }
    S.setup = state.setup;
    S.user = state.user;
    S.recording = state.recording;
    if (S.setup) return drawSetup();
    if (!S.user) return drawLogin();
    drawShell();
  }

  function gateCard(title, lead, body, wide) {
    return h("div", { class: "gate" }, h("div", { class: "gate-card" + (wide ? " wide" : "") },
      h("div", { class: "gate-mark" }, icon("flask"), h("b", { text: "DocsForge lab" })),
      h("h1", { text: title }), lead ? h("p", { text: lead }) : null, body));
  }

  function field(label, input, hint) {
    return h("label", { class: "field" }, h("span", null, label), input, hint ? h("small", { text: hint }) : null);
  }

  function drawLogin() {
    const name = h("input", { type: "text", name: "username", autocomplete: "username", required: true });
    const pass = h("input", { type: "password", name: "password", autocomplete: "current-password", required: true });
    const err = h("p", { class: "error", hidden: true });
    const btn = h("button", { class: "btn primary wide", type: "submit", text: "Sign in" });
    const form = h("form", {
      onsubmit: async (ev) => {
        ev.preventDefault();
        btn.disabled = true;
        err.hidden = true;
        try {
          const r = await api("/api/lab/login", { method: "POST", anon: true,
            body: { username: name.value, password: pass.value } });
          S.user = r.user;
          location.hash = "";
          drawShell();
        } catch (e) {
          err.textContent = e.message;
          err.hidden = false;
          pass.select();
        } finally {
          btn.disabled = false;
        }
      },
    }, field("Name", name), field("Password", pass), btn, err,
    h("p", { class: "gate-note", text: "Only this machine can reach this page. Accounts are kept in lab_data/ beside the code." }));
    put(document.getElementById("app"), gateCard("Sign in", "Testing and verdicts for DocsForge.", form));
    name.focus();
  }

  function drawSetup() {
    const aName = h("input", { type: "text", value: "admin", autocomplete: "off", required: true });
    const aPass = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: "8" });
    const tName = h("input", { type: "text", value: "tester", autocomplete: "off", required: true });
    const tPass = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: "8" });
    const err = h("p", { class: "error", hidden: true });
    const btn = h("button", { class: "btn primary wide", type: "submit", text: "Create both accounts" });
    const form = h("form", {
      onsubmit: async (ev) => {
        ev.preventDefault();
        btn.disabled = true;
        err.hidden = true;
        try {
          const r = await api("/api/lab/setup", { method: "POST", anon: true, body: {
            admin: { username: aName.value.trim(), password: aPass.value },
            tester: { username: tName.value.trim(), password: tPass.value } } });
          S.user = r.user;
          S.setup = false;
          location.hash = "#/overview";
          drawShell();
          toast("Both accounts made. You are signed in as the admin.");
        } catch (e) {
          err.textContent = e.message;
          err.hidden = false;
        } finally {
          btn.disabled = false;
        }
      },
    },
    h("div", { class: "gate-pair" },
      h("p", { class: "gate-group", text: "admin — the numbers, the setup, the verdicts inbox" }),
      field("Name", aName), field("Password", aPass, "8 characters or more"),
      h("p", { class: "gate-group", text: "tester — the bench: run tests, give verdicts" }),
      field("Name", tName), field("Password", tPass)),
    btn, err,
    h("p", { class: "gate-note", text: "Only this machine can reach this page. More accounts can be added later under Accounts." }));
    put(document.getElementById("app"), gateCard("Set up the test lab",
      "The first run on this machine. Make the admin account, and the tester account the bench is for.", form, true));
    aPass.focus();
  }

  // ── the shell ─────────────────────────────────────────────
  const NAV = {
    tester: [
      ["Testing", [["bench", "Test bench", "bench"], ["review", "Review queue", "inbox", "review"],
        ["tests", "All tests", "list"], ["batches", "Batches", "stack"]]],
      ["What the AI ran", [["activity", "AI activity", "pulse"]]],
      ["You", [["feedback", "My verdicts", "flag"]]],
    ],
    admin: [
      ["Dashboard", [["overview", "Overview", "chart"], ["feedback", "Verdicts inbox", "flag", "open"]]],
      ["Testing", [["bench", "Test bench", "bench"], ["review", "Review queue", "inbox", "review"],
        ["tests", "All tests", "list"], ["batches", "Batches", "stack"]]],
      ["What the AI ran", [["activity", "AI activity", "pulse"], ["logs", "Logs", "log"]]],
      ["Setup", [["setup", "Setup", "gear"], ["users", "Accounts", "users"]]],
    ],
  };
  const ADMIN_ONLY = new Set(["overview", "setup", "users", "logs"]);
  let shellMain = null, shellNav = null, onHash = null, countTimer = 0;

  function drawShell() {
    const role = S.user.role;
    const nav = h("nav", { class: "nav", "aria-label": "Lab" });
    const main = h("main", { class: "main", id: "main" });
    const top = h("header", { class: "top" },
      h("button", { class: "btn sm ghost menu-btn", type: "button", "aria-label": "Menu",
        onclick: () => nav.classList.toggle("open") }, icon("list")),
      h("a", { class: "brand", href: "#/" + (role === "admin" ? "overview" : "bench") }, icon("flask"), "DocsForge", h("span", { class: "chip brand-chip", text: "lab" })),
      h("span", { class: "chip" + (role === "admin" ? " lit" : ""), text: role }),
      h("span", { class: "spacer" }),
      h("a", { class: "btn sm ghost", href: "/", title: "The chat this lab watches" }, icon("chat", "icon sm"), h("span", { class: "lbl", text: "Chat" })),
      h("a", { class: "who", href: "#/account", text: S.user.username }),
      h("button", { class: "btn sm", type: "button", onclick: signOut, "aria-label": "Sign out" }, icon("exit", "icon sm"), h("span", { class: "lbl", text: "Sign out" })));
    shellNav = nav;
    shellMain = main;
    put(document.getElementById("app"), h("div", { class: "shell" }, top, nav, main));
    drawNav();
    refreshCounts();
    clearInterval(countTimer);
    countTimer = setInterval(refreshCounts, 20000);
    if (onHash) window.removeEventListener("hashchange", onHash);
    onHash = () => route();
    window.addEventListener("hashchange", onHash);
    route();
  }

  function drawNav() {
    const current = (location.hash.slice(2).split(/[/?]/)[0]) || "";
    const groups = NAV[S.user.role] || NAV.tester;
    put(shellNav, ...groups.map(([head, items]) => [
      h("p", { class: "nav-head", text: head }),
      items.map(([id, label, ic, count]) => {
        const c = count ? S.counts[count] : 0;
        const on = current === id;
        return h("a", { href: "#/" + id, "aria-current": on ? "page" : null,
          onclick: () => shellNav.classList.remove("open") }, icon(ic), label,
        c ? h("span", { class: "count" + (count === "review" ? " lit" : ""), text: c }) : null);
      }),
    ]).flat(2));
  }

  async function refreshCounts() {
    if (!S.user) return;
    try {
      const me = await api("/api/lab/me");
      S.user = me.user;
      S.recording = me.recording;
      S.counts = { review: me.review, open: me.open, active: me.active, mine: me.tests };
      S.tools = me.tools || S.tools;
      drawNav();
    } catch (e) { /* signed out: boot() handles it */ }
  }

  async function signOut() {
    try { await api("/api/lab/logout", { method: "POST", anon: true }); } catch (e) { /* gone anyway */ }
    S.user = null;
    location.hash = "";
    boot();
  }

  // ── routing ───────────────────────────────────────────────
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    const [path, query] = raw.split("?");
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    const params = Object.fromEntries(new URLSearchParams(query || ""));
    return { parts, params };
  }

  async function route() {
    if (!S.user) return boot();
    leaveView();
    const { parts, params } = parseHash();
    let [view, a, b] = parts;
    const admin = S.user.role === "admin";
    if (!view) view = admin ? "overview" : "bench";
    if (ADMIN_ONLY.has(view) && !admin) view = "bench";
    drawNav();
    const main = shellMain;
    put(main, h("div", { class: "page" }, h("p", { class: "muted", text: "Loading…" })));
    window.scrollTo(0, 0);
    try {
      if (view === "bench") await viewBench(main, params);
      else if (view === "review") await viewReview(main);
      else if (view === "tests" && a) await viewTest(main, a, params);
      else if (view === "tests") await viewTests(main, params);
      else if (view === "batches" && a) await viewBatch(main, a);
      else if (view === "batches") await viewBatches(main);
      else if (view === "activity" && a === "turn" && b) await viewTurn(main, b);
      else if (view === "activity" && a === "call" && b) await viewCall(main, b);
      else if (view === "activity") await viewActivity(main, a || "turns", params);
      else if (view === "feedback") await viewFeedback(main, params);
      else if (view === "overview") await viewOverview(main);
      else if (view === "setup") await viewSetup(main);
      else if (view === "users") await viewUsers(main);
      else if (view === "logs") await viewLogs(main, a || "server", params);
      else if (view === "account") await viewAccount(main);
      else put(main, page("Not here", "Nothing lives at this address.", []));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      put(main, page("Something went wrong", "", [h("div", { class: "banner bad", text: e.message })]));
    }
  }

  function page(title, lead, body, acts, crumbs) {
    return h("div", { class: "page" },
      crumbs ? h("p", { class: "crumbs" }, crumbs) : null,
      h("div", { class: "page-head" },
        h("div", null, h("h1", { text: title }), lead ? h("p", { class: "lead", text: lead }) : null),
        acts && acts.length ? h("div", { class: "acts" }, acts) : null),
      body);
  }

  // ── small marks ───────────────────────────────────────────
  const stateMark = (s) => h("span", { class: "state " + (s || ""), text: s || "—" });
  const VERDICT_WORD = { correct: "correct", partial: "partly", wrong: "wrong", unsure: "unsure" };
  const verdictMark = (v) => v ? h("span", { class: "verdict " + v, text: VERDICT_WORD[v] || v })
    : h("span", { class: "verdict none", text: "no verdict" });
  function autoMark(auto) {
    if (!auto) return h("span", { class: "muted", text: "—" });
    return h("span", { class: "auto " + (auto.pass ? "pass" : "fail"), title: auto.reason || "",
      text: auto.pass ? "pass" : "fail" });
  }
  const KIND_WORD = { resolve: "resolve", fetch: "content", harvest: "harvest" };
  function subjectOf(t) {
    const i = t.input || {};
    return (i.name || i.url || "?") + (i.language ? ` · ${i.language}` : "") + (i.topic ? ` · ${i.topic}` : "");
  }
  function metric(label, value, bad) {
    return h("span", { class: "metric" + (bad ? " bad" : "") }, h("b", { text: value }), " ", label);
  }
  function metricsRow(m, extra = []) {
    if (!m) return null;
    return h("div", { class: "metrics" },
      metric("characters", n(m.chars)), metric("headings", n(m.headings)), metric("code blocks", n(m.fences)),
      metric("collapsed code", n(m.collapsed_fences), m.collapsed_fences > 0),
      metric("chrome lines", n(m.chrome_lines), m.chrome_lines > 0),
      metric("relative links", n(m.relative_links), m.relative_links > 0),
      metric("permalink marks", n(m.permalink_marks), m.permalink_marks > 0), extra);
  }
  const flagsOf = (flags) => (flags || []).length
    ? (flags || []).map((f) => h("span", { class: "flag", text: f }))
    : h("span", { class: "muted", text: "clean" });

  // ── the bench ─────────────────────────────────────────────
  async function viewBench(main, params) {
    const tab = params.tab || "resolve";
    const me = await api("/api/lab/me");
    S.counts = { review: me.review, open: me.open, active: me.active, mine: me.tests };
    drawNav();
    const mine = me.tests;
    const k = mine.kinds;
    const line = h("p", { class: "lead" },
      `You have run ${n(mine.total)} test${mine.total === 1 ? "" : "s"}`,
      mine.active ? ` · ${mine.active} running now` : "",
      " · ", h("a", { href: "#/review", text: `${n(me.review)} await a verdict` }), ".");
    const tabs = h("div", { class: "tabs", role: "tablist" });
    const panel = h("div", { class: "card" });
    const recent = h("div");
    const TABS = [["resolve", "Resolve a name"], ["fetch", "Check a page"], ["harvest", "Harvest"], ["batch", "Batch"]];
    function show(which) {
      put(tabs, ...TABS.map(([id, label]) => h("button", { class: "tab", role: "tab", type: "button",
        "aria-selected": String(id === which), text: label,
        onclick: () => { history.replaceState(null, "", "#/bench?tab=" + id); show(id); } })));
      put(panel, ...benchForm(which));
    }
    show(TABS.some(([id]) => id === tab) ? tab : "resolve");
    put(main, h("div", { class: "page" },
      h("div", { class: "page-head" }, h("div", null, h("h1", { text: "Test bench" }), line)),
      judgedLine(k),
      tabs, panel,
      h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", { text: "Your recent tests" }),
        h("div", { class: "acts" }, h("a", { class: "btn sm", href: "#/tests?mine=1", text: "All of yours" }))), recent)));
    let lastRecent = "";
    async function loadRecent() {
      const r = await api("/api/lab/tests?" + qs({ mine: 1, limit: 8 }));
      const sig = JSON.stringify(r.tests);
      if (sig === lastRecent) return;
      lastRecent = sig;
      put(recent, r.tests.length ? testsTable(r.tests)
        : h("div", { class: "empty" }, h("b", { text: "Nothing run yet" }), "Start with a name above — zod, langgraph for javascript, tokio."));
      return r.tests.some((t) => !FINISHED.has(t.state));
    }
    await loadRecent();
    every(3000, loadRecent);
  }

  /** "Your tests, as you judged them: resolution 4 of 5 correct · …" — only
   *  the kinds with something judged, and nothing at all before the first. */
  function judgedLine(k) {
    const parts = [["resolution", k.resolve], ["content", k.fetch], ["harvest", k.harvest]]
      .filter(([, x]) => x.accuracy.total)
      .map(([name, x]) => `${name} ${x.accuracy.good} of ${x.accuracy.total} correct`);
    return parts.length ? h("p", { class: "muted", style: { margin: "-10px 0 18px", fontSize: "12.5px" },
      text: "Your tests, as judged: " + parts.join(" · ") }) : null;
  }

  function select(options, value) {
    return h("select", null, options.map(([v, label]) => h("option", { value: v, text: label, selected: v === value ? true : null })));
  }
  const langSelect = () => select([["", "any language"], ...LANGUAGES.map((l) => [l, l])], "");

  async function startTest(kind, input, btn, err) {
    btn.disabled = true;
    err.hidden = true;
    try {
      const r = await api("/api/lab/tests", { method: "POST", body: { kind, input } });
      location.hash = `#/tests/${r.id}`;
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      btn.disabled = false;
    }
  }

  function benchForm(which) {
    const err = h("p", { class: "error", hidden: true });
    if (which === "resolve") {
      const name = h("input", { type: "text", placeholder: "zod, tokio, langgraph, apache airflow…", required: true });
      const lang = langSelect();
      const eco = select([["", "any registry"], ["npm", "npm"], ["pypi", "PyPI"], ["crates", "crates.io"]], "");
      const expected = h("input", { type: "text", placeholder: "zod.dev  (optional)" });
      const fetchPage = h("input", { type: "checkbox", checked: true });
      const memory = h("input", { type: "checkbox" });
      const btn = h("button", { class: "btn primary", type: "submit", text: "Resolve" });
      return [h("h2", { text: "Name → documentation URL" }),
        h("p", { class: "sub", text: "Resolves the name with the cache off, exactly as the held-out measure does, and fetches the page it lands on — so you can check the address and what is there." }),
        h("form", { onsubmit: (e) => { e.preventDefault(); startTest("resolve", { name: name.value, language: lang.value,
          ecosystem: eco.value, expected: expected.value, fetch_page: fetchPage.checked, use_memory: memory.checked }, btn, err); } },
        h("div", { class: "grid3" }, field("Name", name), field("Language edition", lang, "“langgraph for node” → javascript"),
          field("Registry", eco)),
        field(h("span", null, "Where it should land ", h("i", { text: "— optional; turns on the automatic check" })), expected,
          "A host or host/path, several separated by commas: docs.astral.sh/uv"),
        h("label", { class: "check" }, fetchPage, h("span", { text: "Fetch the resolved page, so its content can be checked" })),
        h("label", { class: "check" }, memory, h("span", { text: "Use remembered answers, as the chat does (off tests resolution itself)" })),
        h("div", { class: "form-foot" }, btn, h("span", { class: "note", text: "Nothing is harvested or stored." })), err)];
    }
    if (which === "fetch") {
      const url = h("input", { type: "url", placeholder: "https://docs.pydantic.dev/latest/concepts/models/", required: true });
      const name = h("input", { type: "text", placeholder: "pydantic  (optional)" });
      const js = h("input", { type: "checkbox" });
      const btn = h("button", { class: "btn primary", type: "submit", text: "Fetch and extract" });
      return [h("h2", { text: "URL → extracted Markdown" }),
        h("p", { class: "sub", text: "Runs detect_source_type and fetch_docs on one page. Put the Markdown beside the live page and say whether anything was lost or kept that should not have been." }),
        h("form", { onsubmit: (e) => { e.preventDefault(); startTest("fetch", { url: url.value, name: name.value, js: js.checked }, btn, err); } },
        field("URL", url), field("Technology it should document", name, "Optional: the automatic check counts how often the page names it."),
        h("label", { class: "check" }, js, h("span", { text: "Render JavaScript first (Playwright)" })),
        h("div", { class: "form-foot" }, btn), err)];
    }
    if (which === "harvest") {
      const target = h("input", { type: "text", placeholder: "a name (htmx) or a URL (https://htmx.org/docs/)", required: true });
      const lang = langSelect();
      const topic = h("input", { type: "text", placeholder: "web development  (optional)" });
      const version = h("input", { type: "text", placeholder: "1.10  (optional)" });
      const pages = h("input", { type: "number", min: "0", max: "5000", value: "40" });
      const timeout = h("input", { type: "number", min: "1", max: "240", value: "30" });
      const expected = h("input", { type: "text", placeholder: "htmx.org  (optional)" });
      const js = h("input", { type: "checkbox" });
      const btn = h("button", { class: "btn primary", type: "submit", text: "Harvest" });
      return [h("h2", { text: "Name or URL → a whole harvest" }),
        h("p", { class: "sub", text: "learn_technology for a name, harvest_docs for a URL — in a process and store of their own under lab_data/runs/, never in DocsStore. Every stored page is measured and listed." }),
        h("form", { onsubmit: (e) => {
          e.preventDefault();
          const v = target.value.trim();
          const isUrl = /^https?:\/\//i.test(v);
          startTest("harvest", { name: isUrl ? "" : v, url: isUrl ? v : "", language: lang.value, topic: topic.value,
            version: version.value, max_pages: pages.value, timeout_min: timeout.value, expected: expected.value, js: js.checked }, btn, err);
        } },
        field("Name or URL", target),
        h("div", { class: "grid3" }, field("Language edition", lang), field("Topic", topic), field("Version", version)),
        h("div", { class: "grid3" }, field("Page limit", pages, "0 harvests the whole section"),
          field("Give up after (minutes)", timeout), field("Where it should come from", expected)),
        h("label", { class: "check" }, js, h("span", { text: "Render JavaScript (slower; for sites that draw everything client-side)" })),
        h("div", { class: "form-foot" }, btn, h("span", { class: "note", text: "Harvests run one at a time; others wait in the queue." })), err)];
    }
    // batch
    const kind = select([["resolve", "resolve — name | language | expected"], ["fetch", "content — url | name"],
      ["harvest", "harvest — name or url | language | page limit"]], "resolve");
    const preset = h("select", null, h("option", { value: "", text: "Load a ready-made list…" }));
    const text = h("textarea", { class: "code", placeholder: "zod | | zod.dev\nlanggraph | javascript | docs.langchain.com/oss/javascript\ntokio | | tokio.rs, docs.rs/tokio" });
    const title = h("input", { type: "text", placeholder: "Round 4, fresh names" });
    const fetchPage = h("input", { type: "checkbox", checked: true });
    const pages = h("input", { type: "number", min: "0", max: "5000", value: "40" });
    const btn = h("button", { class: "btn primary", type: "submit", text: "Run 0 tests" });
    const count = () => text.value.split("\n").filter((l) => l.replace(/(^|\s)#.*$/, "").trim()).length;
    const recount = () => { const c = count(); btn.textContent = `Run ${c} test${c === 1 ? "" : "s"}`; btn.disabled = !c; };
    text.addEventListener("input", recount);
    recount();
    const kindOptions = h("div");
    function drawKindOptions() {
      put(kindOptions, kind.value === "resolve"
        ? h("label", { class: "check" }, fetchPage, h("span", { text: "Fetch each resolved page too" }))
        : kind.value === "harvest" ? h("div", { class: "grid3" }, field("Page limit for lines that give none", pages)) : "");
    }
    kind.addEventListener("change", drawKindOptions);
    drawKindOptions();
    let presets = [];
    api("/api/lab/presets").then((r) => {
      presets = r.presets;
      preset.append(...presets.map((p) => h("option", { value: p.id, text: p.title })));
    }).catch(() => {});
    preset.addEventListener("change", () => {
      const p = presets.find((x) => x.id === preset.value);
      if (!p) return;
      kind.value = p.kind;
      text.value = p.text;
      title.value = p.title;
      drawKindOptions();
      recount();
    });
    return [h("h2", { text: "A list, run automatically, reviewed by hand" }),
      h("p", { class: "sub", text: "One test per line. They queue and run by themselves; each finished one lands in the review queue with its automatic check, for you to confirm or overrule." }),
      h("form", { onsubmit: async (e) => {
        e.preventDefault();
        btn.disabled = true;
        err.hidden = true;
        const defaults = kind.value === "resolve" ? { fetch_page: fetchPage.checked }
          : kind.value === "harvest" ? { max_pages: pages.value } : {};
        try {
          const r = await api("/api/lab/batches", { method: "POST", body: { kind: kind.value, text: text.value, title: title.value, defaults } });
          location.hash = `#/batches/${r.id}`;
        } catch (ex) {
          err.textContent = ex.message;
          err.hidden = false;
          btn.disabled = false;
        }
      } },
      h("div", { class: "grid2" }, field("Kind", kind), field("Start from", preset)),
      field("Tests", text, "“#” after a space starts a comment. Separate columns with |."),
      h("div", { class: "grid2" }, field("Title", title), h("div", null, kindOptions)),
      h("div", { class: "form-foot" }, btn), err)];
  }

  // ── lists of tests ────────────────────────────────────────
  function testsTable(tests, { from } = {}) {
    const go = (t) => { location.hash = `#/tests/${t.id}` + (from ? `?from=${from}` : ""); };
    return h("div", { class: "table-wrap" }, h("table", { class: "t" },
      h("thead", null, h("tr", null, ["#", "kind", "subject", "result", "auto", "verdict", "by", "when", "took"].map((c, i) =>
        h("th", { class: i === 0 || i === 8 ? "r" : null, text: c })))),
      h("tbody", null, tests.map((t) => h("tr", { class: "go", tabindex: "0", onclick: () => go(t),
        onkeydown: (e) => { if (e.key === "Enter") go(t); } },
      h("td", { class: "r mono muted", text: t.id }),
      h("td", null, h("span", { class: "kind", text: KIND_WORD[t.kind] || t.kind })),
      h("td", { class: "subject" }, subjectOf(t)),
      h("td", null, FINISHED.has(t.state) && t.state !== "done" ? h("div", null, stateMark(t.state)) : null,
        t.state === "done" ? h("div", { class: "clip soft wrap", text: t.headline || "—" }) :
          (!FINISHED.has(t.state) ? stateMark(t.state) : h("div", { class: "clip muted", text: t.error }))),
      h("td", null, autoMark(t.auto)),
      h("td", null, FINISHED.has(t.state) ? verdictMark(t.verdict) : h("span", { class: "muted", text: "—" })),
      h("td", { class: "muted", text: t.by }),
      h("td", { class: "muted", text: ago(t.created), title: when(t.created) }),
      h("td", { class: "r muted", text: t.seconds != null ? secs(t.seconds) : "" }))))));
  }

  async function viewTests(main, params) {
    const f = { kind: params.kind || "", state: params.state || "", mine: params.mine || "", review: params.review || "", q: params.q || "", offset: +params.offset || 0 };
    const LIMIT = 50;
    const kind = select([["", "every kind"], ["resolve", "resolve"], ["fetch", "content"], ["harvest", "harvest"]], f.kind);
    const state = select([["", "any state"], ["queued", "queued"], ["running", "running"], ["done", "done"], ["failed", "failed"], ["cancelled", "cancelled"]], f.state);
    const mine = select([["", "everyone's"], ["1", "mine"]], f.mine);
    const review = select([["", "judged or not"], ["1", "no verdict yet"]], f.review);
    const q = h("input", { type: "search", placeholder: "Search names and URLs", value: f.q });
    const apply = (extra = {}) => { location.hash = "#/tests?" + qs({ kind: kind.value, state: state.value, mine: mine.value, review: review.value, q: q.value.trim(), ...extra }); };
    [kind, state, mine, review].forEach((el) => el.addEventListener("change", () => apply()));
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
    const holder = h("div");
    const pager = h("div", { class: "pager" });
    put(main, page("All tests", "Every test anyone has run here, newest first.", [
      h("div", { class: "filters" }, kind, state, mine, review, q), holder, pager],
    [h("a", { class: "btn primary", href: "#/bench", text: "New test" })]));
    let last = "";
    async function load() {
      const r = await api("/api/lab/tests?" + qs({ ...f, limit: LIMIT }));
      const sig = JSON.stringify(r);
      if (sig === last) return;
      last = sig;
      put(holder, r.tests.length ? testsTable(r.tests) : h("div", { class: "empty" }, h("b", { text: "No tests match" }), "Loosen the filters, or run one from the bench."));
      put(pager, `${n(r.total)} test${r.total === 1 ? "" : "s"}`,
        f.offset > 0 ? h("button", { class: "btn sm", text: "Newer", onclick: () => apply({ offset: Math.max(0, f.offset - LIMIT) }) }) : null,
        f.offset + LIMIT < r.total ? h("button", { class: "btn sm", text: "Older", onclick: () => apply({ offset: f.offset + LIMIT }) }) : null);
      return r.tests.some((t) => !FINISHED.has(t.state));
    }
    await load();
    every(3000, load);
  }

  async function viewReview(main) {
    const r = await api("/api/lab/tests?" + qs({ review: 1, limit: 200 }));
    const first = r.tests[0];
    put(main, page("Review queue",
      "Finished tests nobody has judged yet, newest first. Each asks the same few questions; your answers are what accuracy is measured from.",
      [r.tests.length ? testsTable(r.tests, { from: "review" })
        : h("div", { class: "empty" }, h("b", { text: "Nothing waiting" }), "Every finished test has a verdict. Run more from the bench, or a batch.")],
      first ? [h("a", { class: "btn primary", href: `#/tests/${first.id}?from=review`, text: `Start reviewing (${r.total})` })] : []));
  }

  // ── one test ──────────────────────────────────────────────
  async function viewTest(main, id, params) {
    let t = await api(`/api/lab/tests/${encodeURIComponent(id)}`);
    const open = new Set();
    const head = h("div");
    const left = h("div");
    const aside = h("aside");
    put(main, h("div", { class: "page" }, head, h("div", { class: "detail" }, left, aside)));
    let sig = "";
    function draw(test) {
      put(head, testHeader(test, params));
      put(left, ...testBody(test, open));
    }
    draw(t);
    const panel = verdictPanel({ targetKind: "test", targetId: String(t.id), form: t.kind, test: t,
      finished: FINISHED.has(t.state), feedback: t.feedback, from: params.from });
    put(aside, panel.el);
    if (!FINISHED.has(t.state)) {
      const stop = every(1500, async () => {
        const next = await api(`/api/lab/tests/${t.id}`);
        const s = JSON.stringify([next.state, next.steps.map((x) => [x.state, x.duration_ms, (x.events || []).length, x.output.length])]);
        if (s !== sig) { sig = s; draw(next); }
        if (FINISHED.has(next.state)) {
          stop();
          t = next;
          panel.finished(next);
          refreshCounts();
          toast(`Test #${next.id} ${next.state === "done" ? "finished" : next.state} — how did it do?`);
        }
      });
    }
  }

  function testHeader(t, params) {
    const i = t.input || {};
    const acts = [];
    if (!FINISHED.has(t.state)) {
      acts.push(h("button", { class: "btn sm warn", type: "button", onclick: async () => {
        try { const r = await api(`/api/lab/tests/${t.id}/cancel`, { method: "POST" }); toast(r.outcome === "stopping" ? "Stopping the harvest…" : "Cancelled."); } catch (e) { fail(e); }
      } }, icon("stop", "icon sm"), "Cancel"));
    }
    acts.push(h("button", { class: "btn sm", type: "button", onclick: async () => {
      try { const r = await api(`/api/lab/tests/${t.id}/rerun`, { method: "POST" }); location.hash = `#/tests/${r.id}`; } catch (e) { fail(e); }
    } }, icon("redo", "icon sm"), "Run again"));
    const chain = (kind, input, label) => h("button", { class: "btn sm", type: "button", onclick: async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        const r = await api("/api/lab/tests", { method: "POST", body: { kind, input } });
        location.hash = `#/tests/${r.id}`;
      } catch (ex) { fail(ex); btn.disabled = false; }
    } }, label);
    const best = t.result && t.result.best_url;
    if (t.kind === "resolve" && best && FINISHED.has(t.state)) {
      acts.push(chain("fetch", { url: best, name: i.name }, "Check its extraction"),
        chain("harvest", { url: best, name: i.name, max_pages: 40 }, "Harvest it (40 pages)"));
    }
    if (t.kind === "fetch" && FINISHED.has(t.state)) {
      acts.push(chain("harvest", { url: i.url, name: i.name, max_pages: 40 }, "Harvest from here (40 pages)"));
    }
    const from = params.from || "";
    const back = from === "review" ? h("a", { href: "#/review", text: "Review queue" })
      : from.startsWith("batch-") ? h("a", { href: `#/batches/${from.slice(6)}`, text: "Batch " + from.slice(6) })
        : t.batch_id ? h("a", { href: `#/batches/${t.batch_id}`, text: "Batch " + t.batch_id })
          : h("a", { href: "#/tests", text: "Tests" });
    return page(`${KIND_WORD[t.kind] || t.kind}: ${i.name || i.url || "?"}`, "", [
      h("div", { class: "summary-line" }, stateMark(t.state),
        t.state === "running" && t.started ? h("span", { text: `running ${secs(Date.now() / 1000 - t.started)}` }) : null,
        t.seconds != null ? h("span", { text: `took ${secs(t.seconds)}` }) : null,
        i.language ? h("span", { class: "chip", text: i.language }) : null,
        i.topic ? h("span", { class: "chip", text: "topic: " + i.topic }) : null,
        i.version ? h("span", { class: "chip", text: "version " + i.version }) : null,
        h("span", { class: "muted", text: `by ${t.by || "?"} · ${when(t.created)}` }),
        t.parent_id ? h("a", { href: `#/tests/${t.parent_id}`, text: `re-run of #${t.parent_id}` }) : null,
        t.reruns && t.reruns.length ? h("span", null, "re-run as ", t.reruns.map((r, k) => [k ? ", " : "", h("a", { href: `#/tests/${r}`, text: "#" + r })])) : null),
      autoLine(t),
      t.error && t.state !== "done" ? h("div", { class: "banner bad", style: { marginTop: "12px" }, text: t.error }) : null,
    ], acts, [back, " / #", String(t.id)]);
  }

  function autoLine(t) {
    if (!FINISHED.has(t.state)) return null;
    const a = t.auto;
    if (!a) return h("p", { class: "muted", style: { margin: "10px 0 0", fontSize: "13px" } },
      t.kind === "resolve" ? "No automatic check: no expected location was given. Your verdict is the only judgement." : "No automatic check.");
    return h("p", { style: { margin: "10px 0 0", fontSize: "13px" } }, "Automatic check: ", autoMark(a), " — ", h("span", { class: "soft", text: a.reason || "" }),
      h("span", { class: "muted", text: "  (a machine's reading; your verdict decides)" }));
  }

  function testBody(t, open) {
    const out = [];
    const r = t.result || {};
    if (t.kind === "resolve" && FINISHED.has(t.state)) out.push(resolveResult(t, r));
    if (t.kind === "fetch" && FINISHED.has(t.state) && r.url) out.push(fetchResult(t, r));
    if (t.kind === "harvest" && (FINISHED.has(t.state) || r.pages)) out.push(harvestResult(t, r));
    if (!FINISHED.has(t.state) && !t.steps.length) {
      out.push(h("div", { class: "empty" }, h("b", { text: t.state === "queued" ? "Waiting its turn" : "Starting" }),
        t.kind === "harvest" ? "Harvests run one at a time." : "A few seconds."));
    }
    out.push(h("div", { class: "section" },
      h("div", { class: "section-head" }, h("h2", { text: "Tools this test ran" }),
        h("span", { class: "muted", style: { fontSize: "12.5px" }, text: "What each was given, what it returned, and the stages underneath — the same trace the chat shows." })),
      t.steps.length ? t.steps.map((s) => stepCard(s, open, !FINISHED.has(t.state) && s.state === "running" || s.state === "failed"))
        : h("p", { class: "muted", text: "None yet." })));
    return out;
  }

  function stepOutput(t, tool) {
    const s = [...t.steps].reverse().find((x) => x.tool === tool);
    return s ? s : null;
  }

  function resolveResult(t, r) {
    const res = r.resolution || {};
    const best = r.best_url;
    const landed = r.page;
    const fetched = stepOutput(t, "fetch_docs");
    const i = t.input || {};
    return h("div", null,
      h("div", { class: "card" },
        h("h2", { text: best ? "Resolved to" : "Refused" }),
        best ? h("div", null, ext(best, best, "resolved")) : h("p", { class: "soft", text: "Nothing verified, so nothing would be harvested." }),
        h("div", { class: "summary-line" },
          best ? h("span", { text: `via ${r.via || "—"}` }) : null,
          h("span", { text: `${n((res.candidates || []).length)} candidate${(res.candidates || []).length === 1 ? "" : "s"} weighed` }),
          h("span", { text: `in ${secs(r.seconds)}` }),
          h("span", { text: r.cached ? "remembered answers allowed" : "cache off" }),
          res.ecosystem ? h("span", { class: "chip", text: res.ecosystem }) : null,
          res.release ? h("span", { class: "chip", text: "release " + res.release }) : null),
        res.note ? h("p", { class: "soft", style: { margin: "12px 0 0", whiteSpace: "pre-wrap" }, text: res.note }) : null,
        (res.candidates || []).length ? h("div", { class: "table-wrap", style: { marginTop: "14px" } }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ["", "candidate", "from", "confidence", "authority", "why"].map((c) => h("th", { text: c })))),
          h("tbody", null, res.candidates.map((c) => h("tr", { class: best && c.url === best ? "best" : null },
            h("td", null, h("span", { class: "candidate-mark " + (c.verified === true ? "v" : c.verified === false ? "u" : ""),
              text: c.verified === true ? "verified" : c.verified === false ? "refused" : "unchecked" })),
            h("td", { class: "wrap" }, ext(c.url, c.url)),
            h("td", { class: "mono muted", text: c.source }),
            h("td", { class: "r mono", text: c.confidence != null ? c.confidence.toFixed(2) : "" }),
            h("td", { class: "r mono", text: c.authority != null ? c.authority : "" }),
            h("td", { class: "soft wrap" }, c.reason || c.evidence || "",
              (c.signals || []).length ? h("div", { class: "muted mono", style: { fontSize: "11px", marginTop: "3px" }, text: c.signals.join(" · ") }) : null))))))
          : null),
      landed ? h("div", { class: "card" },
        h("h2", { text: "The page it lands on, as fetch_docs returned it" }),
        h("p", { class: "sub" }, "Compare with the live page: ", ext(landed.url, "open " + landed.url)),
        metricsRow(landed.metrics, [i.name ? metric(`mentions of “${i.name}”`, n(landed.mentions), landed.mentions === 0) : null]),
        fetched ? outputBlock(fetched.output, fetched.omitted, { rendered: true }) : null) : null);
  }

  function fetchResult(t, r) {
    const fetched = stepOutput(t, "fetch_docs");
    const i = t.input || {};
    return h("div", { class: "card" },
      h("h2", { text: "What extraction kept" }),
      h("p", { class: "sub" }, "Compare with the live page: ", ext(r.url, r.url)),
      h("div", { class: "summary-line" },
        r.source_type ? h("span", { class: "chip", text: "detected: " + r.source_type }) : null,
        r.kind ? h("span", { class: "chip", text: "kind: " + r.kind }) : null,
        h("span", null, flagsOf(r.flags))),
      metricsRow(r.metrics, [i.name ? metric(`mentions of “${i.name}”`, n(r.mentions), r.mentions === 0) : null]),
      fetched ? outputBlock(fetched.output, fetched.omitted, { rendered: true }) : null);
  }

  function harvestResult(t, r) {
    const pages = r.pages || [];
    const entries = r.entries || [];
    const tot = r.totals || {};
    const flagged = pages.filter((p) => (p.flags || []).length);
    const sets = new Set(pages.map((p) => p.technology)).size > 1;
    const thin = pages.filter((p) => (p.flags || []).includes("thin"));
    const list = h("div");
    const tabs = h("div", { class: "tabs", style: { marginBottom: "10px" } });
    let limit = 200;
    function show(which) {
      const rows = which === "flagged" ? flagged : which === "thin" ? thin : pages;
      put(tabs, ...[["all", `every page (${pages.length})`], ["flagged", `with residue (${flagged.length})`], ["thin", `thin (${thin.length})`]]
        .map(([id, label]) => h("button", { class: "tab", type: "button", "aria-selected": String(id === which), text: label, onclick: () => { limit = 200; show(id); } })));
      put(list, rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", null, h("tr", null, [sets ? "set" : null, "#", "title", "address", "characters", "residue"].filter((c) => c !== null)
          .map((c) => h("th", { class: c === "#" || c === "characters" ? "r" : null, text: c })))),
        h("tbody", null, rows.slice(0, limit).map((p) => h("tr", { class: "go",
          onclick: (e) => { if (!e.target.closest("a, button")) openPage(t, p); } },
        sets ? h("td", { class: "mono muted", style: { fontSize: "12px" }, text: p.technology }) : null,
        h("td", { class: "r mono muted", text: p.ordinal }),
        h("td", { class: "subject" }, h("button", { class: "linkish", type: "button", text: p.title || "(untitled)",
          title: "Read this page as stored", onclick: () => openPage(t, p) })),
        h("td", { class: "wrap" }, p.url ? ext(p.url, p.url.replace(/^https?:\/\//, "")) : ""),
        h("td", { class: "r mono", text: n(p.chars) }),
        h("td", null, flagsOf(p.flags))))))) : h("p", { class: "muted", text: "None." }),
      rows.length > limit ? h("button", { class: "btn sm", style: { marginTop: "10px" }, type: "button", text: `Show ${Math.min(200, rows.length - limit)} more`,
        onclick: () => { limit += 200; show(which); } }) : null);
    }
    show("all");
    return h("div", null,
      h("div", { class: "card" },
        h("h2", { text: r.headline || "Harvest" }),
        h("p", { class: "sub", text: `${r.tool || ""} · ${r.seconds != null ? secs(r.seconds) : ""} · stored in this test's own store, not DocsStore` }),
        entries.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ["filed as", "version", "pages", "characters", "coverage", "strategy", "from"].map((c) => h("th", { text: c })))),
          h("tbody", null, entries.map((e) => h("tr", null,
            h("td", { class: "subject", text: e.technology }), h("td", { class: "mono", text: e.version }),
            h("td", { class: "r mono", text: n(e.pages) + (e.expected ? ` / ${n(e.expected)}` : "") }),
            h("td", { class: "r mono", text: compact(e.characters) }),
            h("td", null, e.complete === true ? h("span", { class: "chip lit", text: "complete" }) : e.complete === false ? h("span", { class: "chip bad", text: "incomplete" }) : h("span", { class: "chip", text: "unknown" })),
            h("td", { class: "mono muted", text: e.strategy || "" }),
            h("td", { class: "wrap" }, e.source ? ext(e.source, e.source) : "")))))) : h("p", { class: "muted", text: "Nothing was stored." }),
        pages.length ? h("div", { class: "metrics" },
          metric("pages", n(tot.pages)), metric("clean", `${n(tot.clean_pages)} (${pct(tot.clean_pages, tot.pages)})`),
          metric("thin", n(tot.thin_pages), tot.thin_pages > 0), metric("characters", compact(tot.chars)),
          metric("code blocks", n(tot.fences)), metric("collapsed code", n(tot.collapsed_fences), tot.collapsed_fences > 0),
          metric("chrome lines", n(tot.chrome_lines), tot.chrome_lines > 0), metric("relative links", n(tot.relative_links), tot.relative_links > 0),
          metric("permalink marks", n(tot.permalink_marks), tot.permalink_marks > 0)) : null,
        (r.log_tail || []).length ? h("details", { style: { marginTop: "10px" } }, h("summary", { class: "muted", text: "The harvest's own log (last lines)" }),
          h("pre", { class: "out", style: { marginTop: "8px" }, text: r.log_tail.join("\n") })) : null),
      pages.length ? h("div", { class: "section" },
        h("div", { class: "section-head" }, h("h2", { text: "Stored pages" }),
          h("span", { class: "muted", style: { fontSize: "12.5px" }, text: "Open one to read it beside the live page." })),
        tabs, list) : null);
  }

  // ── a step, and its trace ─────────────────────────────────
  function stepCard(s, open, openByDefault) {
    const key = String(s.id || s.trace_id);
    const d = h("details", { class: "step", open: open.has(key) || (openByDefault && !open.has("closed:" + key)) ? true : null });
    d.addEventListener("toggle", () => {
      if (d.open) { open.add(key); open.delete("closed:" + key); } else { open.delete(key); open.add("closed:" + key); }
    });
    const args = Object.entries(s.args || {}).filter(([, v]) => v !== "" && v != null && v !== false);
    const state = s.state === "done" ? "completed" : s.state;
    d.append(h("summary", { class: "step-head" },
      s.seq != null ? h("span", { class: "seq", text: String(s.seq).padStart(2, "0") }) : null,
      h("span", { class: "tool", text: s.tool }),
      h("span", { class: "args" }, args.map(([k, v]) => h("span", { class: "arg", title: `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}` },
        h("b", { text: k + " " }), typeof v === "string" ? v : JSON.stringify(v)))),
      h("span", { class: "spacer" }),
      stateMark(state),
      h("span", { class: "dur", text: ms(s.duration_ms) })));
    const body = h("div", { class: "step-body" });
    let built = false;
    const build = () => {
      if (built) return;
      built = true;
      const events = s.events || [];
      add(body, [
        s.error ? h("div", { class: "banner bad", style: { marginTop: "12px" }, text: s.error }) : null,
        h("h3", { text: s.state === "running" ? "Output — not returned yet" : "What it returned" }),
        s.output ? outputBlock(s.output, s.omitted) : h("p", { class: "muted", text: s.state === "running" ? "Still working." : "Nothing." }),
        events.length ? [h("h3", { text: `Trace — ${events.length} stage${events.length === 1 ? "" : "s"} and events` }), traceTree(events)] : null]);
    };
    if (d.open) build();
    d.addEventListener("toggle", () => { if (d.open) build(); });
    d.append(body);
    return d;
  }

  function outputBlock(text, omitted, { rendered = false } = {}) {
    const pre = h("pre", { class: "out", text: text || "" });
    const box = h("div", { class: "render-box md", hidden: true });
    let done = false;
    const rawBtn = h("button", { type: "button", "aria-pressed": "true", text: "raw" });
    const mdBtn = h("button", { type: "button", "aria-pressed": "false", text: "rendered" });
    async function mode(md) {
      rawBtn.setAttribute("aria-pressed", String(!md));
      mdBtn.setAttribute("aria-pressed", String(md));
      pre.hidden = md;
      box.hidden = !md;
      if (md && !done) {
        done = true;
        box.textContent = "Rendering…";
        try {
          const r = await api("/api/lab/render", { method: "POST", body: { markdown: text || "" } });
          box.innerHTML = r.html;
        } catch (e) { box.textContent = e.message; done = false; }
      }
    }
    rawBtn.onclick = () => mode(false);
    mdBtn.onclick = () => mode(true);
    if (rendered) mode(true);
    return h("div", null,
      h("div", { class: "muted", style: { fontSize: "12px", marginBottom: "6px" } }, `${n((text || "").length)} characters`,
        h("span", { class: "view-toggle" }, rawBtn, mdBtn),
        " ", h("button", { class: "btn sm ghost", type: "button", text: "copy", onclick: () => navigator.clipboard.writeText(text || "").then(() => toast("Copied.")) })),
      pre, box,
      omitted ? h("p", { class: "omitted", text: `${n(omitted)} more characters were returned and not kept here.` }) : null);
  }

  function traceTree(events) {
    const ids = new Set(events.map((e) => e.id));
    const kids = new Map();
    const roots = [];
    for (const e of events) {
      if (e.parent_id && ids.has(e.parent_id)) {
        if (!kids.has(e.parent_id)) kids.set(e.parent_id, []);
        kids.get(e.parent_id).push(e);
      } else roots.push(e);
    }
    const item = (e) => {
      const counters = Object.entries(e.counters || {});
      const li = h("li", null, h("div", { class: "ev" },
        stateMark(e.state), h("span", { class: "name", text: e.name }),
        e.message ? h("span", { class: "msg", text: e.message }) : null,
        e.target ? h("span", { class: "tgt", text: e.target }) : null,
        counters.map(([k, v]) => h("span", { class: "arg" }, h("b", { text: k + " " }), String(v))),
        e.error ? h("span", { class: "err", text: e.error }) : null),
      e.result && Object.keys(e.result).length ? h("details", null, h("summary", { text: "result" }),
        h("pre", { class: "out", text: JSON.stringify(e.result, null, 2) })) : null,
      e.output ? h("details", null, h("summary", { text: `output (${n(e.output.length)} characters)` }),
        h("pre", { class: "out", text: e.output }), e.omitted ? h("p", { class: "omitted", text: `${n(e.omitted)} more characters not kept.` }) : null) : null,
      kids.has(e.id) ? h("ul", { class: "trace" }, kids.get(e.id).map(item)) : null);
      return li;
    };
    return h("ul", { class: "trace" }, roots.map(item));
  }

  // ── the page reader (a harvest's stored page) ─────────────
  let drawerEls = null;
  function closeDrawer() {
    if (!drawerEls) return;
    drawerEls.forEach((el) => el.remove());
    drawerEls = null;
    document.removeEventListener("keydown", drawerKey);
  }
  function drawerKey(e) { if (e.key === "Escape") closeDrawer(); }
  async function openPage(t, p) {
    closeDrawer();
    const body = h("div", { class: "drawer-body" }, h("p", { class: "muted", text: "Reading…" }));
    const scrim = h("div", { class: "drawer-scrim", onclick: closeDrawer });
    const drawer = h("div", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": p.title || "Page" },
      h("div", { class: "drawer-head" },
        h("div", null, h("h2", { text: `${p.ordinal}. ${p.title || "(untitled)"}` }),
          p.url ? h("div", { style: { marginTop: "4px", fontSize: "13px" } }, ext(p.url, "open the live page")) : null),
        h("span", { class: "spacer" }),
        h("button", { class: "btn sm", type: "button", "aria-label": "Close", onclick: closeDrawer }, icon("x", "icon sm"))),
      body);
    document.body.append(scrim, drawer);
    drawerEls = [scrim, drawer];
    document.addEventListener("keydown", drawerKey);
    drawer.querySelector("button").focus();
    try {
      const r = await api(`/api/lab/tests/${t.id}/pages/${p.ordinal}?` + qs({ tech: p.technology, version: p.version }));
      put(body, h("div", null, flagsOf(r.flags)), metricsRow(r.metrics), h("div", { class: "md", html: r.html }));
    } catch (e) {
      put(body, h("div", { class: "banner bad", text: e.message }));
    }
  }

  // ── the verdict panel ─────────────────────────────────────
  const FORMS = {
    resolve: {
      title: "Did it resolve correctly?",
      questions: [
        { key: "url", primary: true, ask: (c) => `Is this the official documentation for ${c.subject}?`, options: [
          ["Yes — the right docs", "correct"], ["Right project, wrong part or edition", "partial"],
          ["Wrong project, or not documentation", "wrong"], ["Refused, but the docs exist", "wrong"],
          ["Refused, and rightly so", "correct"], ["Can't tell", "unsure"]] },
        { key: "content", when: (c) => c.hasPage, ask: () => "Does the fetched page show that documentation?",
          options: [["Yes"], ["Partly"], ["No"], ["Didn't look"]] },
      ],
      issues: ["wrong project", "wrong language edition", "marketing page, not docs", "unofficial mirror",
        "old version", "API reference only", "refused wrongly", "slow"],
      expected: "Where should it have landed?",
    },
    fetch: {
      title: "Is the extraction faithful?",
      questions: [
        { key: "extraction", primary: true, ask: () => "Does the Markdown match the live page?", options: [
          ["Faithful", "correct"], ["Mostly — small losses", "partial"], ["Broken, or content missing", "wrong"],
          ["Wrong page entirely", "wrong"], ["Can't tell", "unsure"]] },
      ],
      issues: ["content missing", "navigation or UI kept", "code blocks broken", "tables broken", "links broken",
        "headings wrong", "empty — needs JavaScript", "wrong title"],
    },
    harvest: {
      title: "Did it harvest the right docs, whole?",
      questions: [
        { key: "harvest", primary: true, ask: (c) => `Is this ${c.subject}'s documentation, whole?`, options: [
          ["Right docs, complete", "correct"], ["Right docs, with gaps", "partial"], ["Wrong docs", "wrong"],
          ["Failed, or nothing useful", "wrong"], ["Can't tell", "unsure"]] },
        { key: "pages", ask: () => "Are the pages you opened clean?", options: [["Clean"], ["Some problems"], ["Mostly broken"], ["Didn't open any"]] },
      ],
      issues: ["missing sections", "out-of-scope pages", "wrong project", "wrong version", "duplicate pages",
        "bad extraction", "stopped early", "slow"],
      expected: "Where are the right docs?",
    },
    turn: {
      title: "Your verdict on this answer",
      questions: [
        { key: "answer", primary: true, ask: () => "Was the answer right?", options: [
          ["Right", "correct"], ["Partly", "partial"], ["Wrong", "wrong"], ["Can't tell", "unsure"]] },
        { key: "tools", ask: () => "Did it use the tools well?", options: [["Yes"], ["Wrong tool"], ["Too many calls"], ["Didn't use them"]] },
      ],
      issues: ["answered from memory", "hallucinated", "wrong library", "ignored a tool result", "looped", "error surfaced", "slow"],
    },
    tool: {
      title: "Your verdict on this call",
      questions: [
        { key: "call", primary: true, ask: () => "Did this call do the right thing?", options: [
          ["Yes", "correct"], ["Partly", "partial"], ["No", "wrong"], ["Can't tell", "unsure"]] },
      ],
      issues: ["wrong arguments", "wrong result", "error", "slow", "output too long", "misleading output"],
    },
  };

  function verdictPanel({ targetKind, targetId, form, test, finished, feedback, from, subject }) {
    const spec = FORMS[form];
    const el = h("div", { class: "verdict-panel" });
    let list = feedback || [];
    let ctx = contextFor(test, subject);
    let isDone = finished;

    function contextFor(tt, subj) {
      if (!tt) return { subject: subj || "it", hasPage: false };
      const i = tt.input || {};
      return { subject: (i.name || i.url || "it") + (i.language ? ` (${i.language})` : ""),
        hasPage: !!(tt.result && tt.result.page), name: i.name, bestUrl: tt.result && tt.result.best_url };
    }

    function draw(ask) {
      el.className = "verdict-panel" + (ask ? " ask" : "");
      const mine = list.find((f) => f.user_id === S.user.id);
      const others = list.filter((f) => f.user_id !== S.user.id);
      if (!isDone) {
        put(el, h("h2", { text: "Your verdict" }),
          h("p", { class: "sub", text: "This test is still running. You'll be asked here the moment it finishes." }),
          others.length ? given(others, "Other verdicts") : null);
        return;
      }
      const answers = { ...((mine && mine.aspects) || {}) };
      const issues = new Set((mine && mine.issues) || []);
      let rating = mine ? mine.rating : null;
      const expected = h("input", { type: "text", value: (mine && mine.expected) || "", placeholder: "https://…" });
      const notes = h("textarea", { placeholder: "What was wrong, what should it have done? Anything that helps whoever fixes it." });
      notes.value = (mine && mine.notes) || "";
      const err = h("p", { class: "error", hidden: true });
      const save = h("button", { class: "btn primary wide", type: "submit", text: mine ? "Update verdict" : "Save verdict" });
      const afterSave = h("div");

      const questions = spec.questions.filter((q) => !q.when || q.when(ctx)).map((q) => {
        const label = q.ask(ctx);
        const opts = h("div", { class: "opts", role: "group", "aria-label": label });
        const buttons = q.options.map(([text, verdict]) => {
          const b = h("button", { class: "opt", type: "button", "aria-pressed": String(answers[q.key] === text), text,
            onclick: () => {
              answers[q.key] = text;
              if (q.primary) answers._verdict = verdict;
              buttons.forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
              if (q.primary) expectedField.hidden = !spec.expected || !["wrong", "partial"].includes(verdict);
            } });
          if (q.primary && answers[q.key] === text) answers._verdict = verdict;
          return b;
        });
        opts.append(...buttons);
        return h("div", { class: "q" }, h("p", { text: label }), opts);
      });
      const expectedField = spec.expected ? field(spec.expected, expected) : h("div");
      expectedField.hidden = !spec.expected || !["wrong", "partial"].includes(answers._verdict);
      const issueBox = h("div", { class: "issues" }, spec.issues.map((name) => {
        const b = h("button", { class: "issue", type: "button", "aria-pressed": String(issues.has(name)), text: name,
          onclick: () => { issues.has(name) ? issues.delete(name) : issues.add(name); b.setAttribute("aria-pressed", String(issues.has(name))); } });
        return b;
      }));
      const stars = h("div", { class: "stars", role: "group", "aria-label": "Rating" });
      const starBtns = [1, 2, 3, 4, 5].map((v) => h("button", { type: "button", "aria-pressed": String(rating === v), text: v,
        onclick: () => { rating = rating === v ? null : v; starBtns.forEach((x, k) => x.setAttribute("aria-pressed", String(rating === k + 1))); } }));
      stars.append(...starBtns);

      const formEl = h("form", { onsubmit: async (ev) => {
        ev.preventDefault();
        err.hidden = true;
        if (!answers._verdict) {
          err.textContent = "Answer the first question — that is the verdict.";
          err.hidden = false;
          return;
        }
        save.disabled = true;
        const aspects = Object.fromEntries(Object.entries(answers).filter(([k]) => !k.startsWith("_")));
        try {
          const saved = await api("/api/lab/feedback", { method: "POST", body: {
            target_kind: targetKind, target_id: targetId, verdict: answers._verdict, aspects,
            issues: [...issues], rating, expected: expected.value, notes: notes.value } });
          list = [saved, ...list.filter((f) => f.id !== saved.id)];
          refreshCounts();
          toast("Verdict saved. Thank you.");
          draw(false);
          if (from) nextInQueue(el, from, targetId);
          if (targetKind === "test" && form === "resolve" && saved.verdict !== "correct" && ctx.name) offerForget(el, ctx.name);
        } catch (e) {
          err.textContent = e.message;
          err.hidden = false;
          save.disabled = false;
        }
      } },
      questions,
      h("div", { class: "q" }, h("p", { text: "What went wrong? (any that apply)" }), issueBox),
      expectedField,
      h("div", { class: "q" }, h("p", { text: "Overall, 1 to 5 (optional)" }), stars),
      field("Notes", notes),
      save, err, afterSave);

      put(el, h("h2", { text: spec.title }),
        h("p", { class: "sub", text: mine ? `You judged this ${ago(mine.updated)} — status: ${mine.status}. Changing it re-opens it.` : "Your answer is what accuracy is counted from — the automatic check is only a hint." }),
        formEl,
        mine && mine.resolution ? h("div", { class: "banner", style: { marginTop: "12px" } }, h("span", null, h("b", { text: "Admin: " }), mine.resolution)) : null,
        others.length ? given(others, "Other verdicts") : null);
      if (ask) {
        const first = el.querySelector(".opt");
        if (first) setTimeout(() => first.focus({ preventScroll: window.innerWidth > 1180 }), 50);
      }
    }
    draw(false);
    return {
      el,
      finished(tt) {
        isDone = true;
        ctx = contextFor(tt, subject);
        list = tt.feedback || list;
        draw(true);
        if (window.innerWidth <= 1180) el.scrollIntoView({ behavior: "smooth", block: "start" });
      },
    };
  }

  function given(list, title) {
    return h("div", { class: "given" }, h("p", { class: "muted", style: { margin: "0 0 6px" }, text: title }),
      list.map((f) => h("div", { style: { marginBottom: "10px" } },
        h("div", { class: "row" }, verdictMark(f.verdict), h("b", { text: f.by }), h("span", { class: "muted", text: ago(f.updated) }),
          h("span", { class: "chip", text: f.status })),
        f.issues.length ? h("div", null, f.issues.map((x) => h("span", { class: "flag quiet", text: x }))) : null,
        f.expected ? h("p", null, "Should be: ", f.expected) : null,
        f.notes ? h("p", { text: f.notes }) : null)));
  }

  async function nextInQueue(panelEl, from, currentId) {
    const q = from === "review" ? { review: 1, limit: 2 } : from.startsWith("batch-") ? { batch: from.slice(6), review: 1, limit: 2 } : null;
    if (!q) return;
    try {
      const r = await api("/api/lab/tests?" + qs(q));
      const next = r.tests.find((x) => String(x.id) !== String(currentId));
      panelEl.append(next
        ? h("a", { class: "btn wide", style: { marginTop: "12px" }, href: `#/tests/${next.id}?from=${from}` }, `Next: #${next.id} ${subjectOf(next)} →`)
        : h("p", { class: "muted", style: { marginTop: "12px" }, text: "That was the last one waiting." }));
    } catch (e) { /* the queue is a convenience */ }
  }

  function offerForget(panelEl, name) {
    panelEl.append(h("div", { class: "given" },
      h("p", { class: "soft", style: { marginTop: 0 }, text: `The chat may still remember this answer for “${name}”.` }),
      h("button", { class: "btn sm", type: "button", text: "Forget the remembered answer", onclick: async (e) => {
        e.target.disabled = true;
        try { const r = await api("/api/lab/actions/forget-resolution", { method: "POST", body: { name } }); toast(r.message || "Forgotten."); }
        catch (ex) { fail(ex); e.target.disabled = false; }
      } })));
  }

  // ── batches ───────────────────────────────────────────────
  function progress(done, total) {
    const bar = h("div", { class: "meter" }, h("i", { style: { width: (total ? (100 * done) / total : 0) + "%" } }));
    return bar;
  }
  async function viewBatches(main) {
    const r = await api("/api/lab/batches");
    const holder = h("div");
    put(main, page("Batches", "Lists of tests that ran by themselves. Each finished test waits for a verdict.",
      [holder], [h("a", { class: "btn primary", href: "#/bench?tab=batch", text: "New batch" })]));
    const draw = (batches) => put(holder, batches.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" },
      h("thead", null, h("tr", null, ["#", "title", "kind", "progress", "automatic check", "judged", "by", "started"].map((c) => h("th", { text: c })))),
      h("tbody", null, batches.map((b) => h("tr", { class: "go", onclick: () => { location.hash = `#/batches/${b.id}`; } },
        h("td", { class: "r mono muted", text: b.id }), h("td", { class: "subject", text: b.title }),
        h("td", null, h("span", { class: "kind", text: KIND_WORD[b.kind] || b.kind })),
        h("td", { style: { minWidth: "160px" } }, progress(b.finished, b.total), h("div", { class: "muted mono", style: { fontSize: "11.5px", marginTop: "4px" }, text: `${b.finished} of ${b.total} finished` })),
        h("td", { class: "mono", text: b.auto_total ? `${b.auto_pass} of ${b.auto_total} pass` : "—" }),
        h("td", { class: "mono", text: `${b.reviewed} of ${b.total}` }),
        h("td", { class: "muted", text: b.by }), h("td", { class: "muted", text: ago(b.created) }))))))
      : h("div", { class: "empty" }, h("b", { text: "No batches yet" }), "Paste a list on the bench's Batch tab, or load a held-out round."));
    let last = JSON.stringify(r.batches);
    draw(r.batches);
    every(4000, async () => {
      const x = await api("/api/lab/batches");
      const sig = JSON.stringify(x.batches);
      if (sig !== last) { last = sig; draw(x.batches); }
    });
  }

  async function viewBatch(main, id) {
    const holder = h("div");
    put(main, holder);
    let last = "";
    async function load() {
      const b = await api(`/api/lab/batches/${encodeURIComponent(id)}`);
      const sig = JSON.stringify(b);
      if (sig === last) return;
      last = sig;
      const pending = b.tests.filter((t) => FINISHED.has(t.state) && !t.verdict);
      const v = b.verdicts || {};
      put(holder, page(b.title, `${KIND_WORD[b.kind] || b.kind} · ${b.total} tests · by ${b.by} · ${when(b.created)}`, [
        h("div", { class: "card" },
          h("div", { class: "meters" },
            meterRow("Finished", "", b.finished, b.total, `${b.finished} of ${b.total}` + (b.states.running ? ` · ${b.states.running} running` : "") + (b.states.queued ? ` · ${b.states.queued} queued` : "") + (b.states.failed ? ` · ${b.states.failed} failed` : "")),
            meterRow("Automatic check", "where an answer was written down", b.auto_pass, b.auto_total, b.auto_total ? `${b.auto_pass} of ${b.auto_total} pass (${pct(b.auto_pass, b.auto_total)})` : "no expected answers given", true),
            meterRow("Judged correct", "by testers", v.correct || 0, (v.correct || 0) + (v.partial || 0) + (v.wrong || 0),
              `${v.correct || 0} correct · ${v.partial || 0} partly · ${v.wrong || 0} wrong · ${v.unsure || 0} unsure · ${b.finished - b.reviewed} not yet judged`))),
        h("div", { class: "section" }, testsTable(b.tests, { from: "batch-" + b.id })),
      ], pending.length ? [h("a", { class: "btn primary", href: `#/tests/${pending[0].id}?from=batch-${b.id}`, text: `Review ${pending.length} waiting` })] : [],
      [h("a", { href: "#/batches", text: "Batches" }), " / ", String(b.id)]));
      return b.finished < b.total;
    }
    await load();
    every(3000, load);
  }

  // ── what the AI ran ───────────────────────────────────────
  async function viewActivity(main, tab, params) {
    const tabs = h("div", { class: "tabs" }, [["turns", "Chat turns"], ["tools", "Tool calls"]].map(([id, label]) =>
      h("a", { class: "tab", href: "#/activity/" + id, "aria-selected": String(id === tab), text: label })));
    const holder = h("div");
    const banner = S.recording ? null : h("div", { class: "banner bad", text: "Recording is off (DOCSFORGE_LAB_RECORD=0): nothing new is being kept." });
    put(main, page("What the AI ran", "Every chat turn and every tool call, with what each tool returned — recorded as it happens, kept on this machine.", [banner, tabs, holder]));
    if (tab === "tools") return activityTools(holder, params);
    return activityTurns(holder, params);
  }

  async function activityTurns(holder, params) {
    const q = h("input", { type: "search", placeholder: "Search questions and answers", value: params.q || "" });
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") location.hash = "#/activity/turns?" + qs({ q: q.value.trim() }); });
    const list = h("div");
    put(holder, h("div", { class: "filters" }, q), list);
    let last = "";
    async function load() {
      const r = await api("/api/lab/activity/turns?" + qs({ q: params.q || "", limit: 100 }));
      const sig = JSON.stringify(r.turns);
      if (sig === last) return;
      last = sig;
      put(list, r.turns.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", null, h("tr", null, ["when", "model", "asked", "tools called", "outcome", "took"].map((c, k) => h("th", { class: k === 5 ? "r" : null, text: c })))),
        h("tbody", null, r.turns.map((t) => h("tr", { class: "go", onclick: () => { location.hash = `#/activity/turn/${t.id}`; } },
          h("td", { class: "muted", text: ago(t.started), title: when(t.started) }),
          h("td", { class: "mono", text: `${t.provider}${t.model ? " · " + t.model : ""}` }),
          h("td", { class: "subject" }, h("div", { class: "clip", text: t.prompt || "(empty)" })),
          h("td", null, (t.tools || []).slice(0, 4).map((x) => h("span", { class: x.endsWith(":error") ? "flag" : "flag quiet", text: x.replace(/:ok$/, "") })),
            (t.tools || []).length > 4 ? h("span", { class: "muted", text: ` +${t.tools.length - 4}` }) : null,
            !(t.tools || []).length ? h("span", { class: "muted", text: "none" }) : null),
          h("td", null, stateMark(t.outcome === "done" ? "done" : t.outcome === "running" ? "running" : t.outcome === "cancelled" ? "cancelled" : "failed"), " ", h("span", { class: "muted mono", style: { fontSize: "11.5px" }, text: t.outcome })),
          h("td", { class: "r muted", text: t.duration_ms != null ? ms(t.duration_ms) : "" }))))))
        : h("div", { class: "empty" }, h("b", { text: "No chat turns recorded yet" }), "Ask something in the chat at / — each turn, its tool calls and its answer land here."));
    }
    await load();
    every(5000, load);
  }

  async function activityTools(holder, params) {
    if (!S.tools) S.tools = (await api("/api/lab/me")).tools || [];
    const tool = select([["", "every tool"], ...S.tools.map((t) => [t, t])], params.tool || "");
    const ok = select([["", "any result"], ["1", "succeeded"], ["0", "errored"]], params.ok || "");
    const source = select([["", "from anywhere"], ["chat", "from the chat"], ["lab", "from lab tests"], ["other", "other"]], params.source || "");
    const q = h("input", { type: "search", placeholder: "Search targets and arguments", value: params.q || "" });
    const apply = () => { location.hash = "#/activity/tools?" + qs({ tool: tool.value, ok: ok.value, source: source.value, q: q.value.trim() }); };
    [tool, ok, source].forEach((el) => el.addEventListener("change", apply));
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
    const list = h("div");
    put(holder, h("div", { class: "filters" }, tool, ok, source, q), list);
    let last = "";
    async function load() {
      const r = await api("/api/lab/activity/tools?" + qs({ tool: params.tool || "", ok: params.ok || "", source: params.source || "", q: params.q || "", limit: 100 }));
      const sig = JSON.stringify(r.calls);
      if (sig === last) return;
      last = sig;
      put(list, r.calls.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", null, h("tr", null, ["when", "tool", "acted on", "from", "result", "returned", "took"].map((c, k) => h("th", { class: k >= 5 ? "r" : null, text: c })))),
        h("tbody", null, r.calls.map((c) => h("tr", { class: "go", onclick: () => { location.hash = `#/activity/call/${encodeURIComponent(c.trace_id)}`; } },
          h("td", { class: "muted", text: ago(c.started), title: when(c.started) }),
          h("td", null, h("span", { class: "tool", text: c.tool })),
          h("td", { class: "wrap" }, h("div", { class: "clip", text: c.target || "—" })),
          h("td", { class: "mono muted", text: c.source + (c.test_id ? ` #${c.test_id}` : "") }),
          h("td", null, stateMark(c.running ? "running" : c.ok ? "done" : "failed")),
          h("td", { class: "r mono", text: compact(c.chars) }),
          h("td", { class: "r muted", text: ms(c.duration_ms) }))))))
        : h("div", { class: "empty" }, h("b", { text: "No tool calls match" }), "Tool calls from the chat and from lab tests are recorded here as they run."));
    }
    await load();
    every(5000, load);
  }

  function callAsStep(c) {
    return { id: c.trace_id, trace_id: c.trace_id, tool: c.tool, args: c.args, state: c.running ? "running" : c.ok ? "done" : "failed",
      duration_ms: c.duration_ms, output: c.output, omitted: c.omitted, events: c.events, error: "" };
  }

  async function viewTurn(main, id) {
    const t = await api(`/api/lab/activity/turns/${encodeURIComponent(id)}`);
    const answer = h("div", { class: "render-box md open" }, t.answer ? "Rendering…" : h("span", { class: "muted", text: "No answer was produced." }));
    if (t.answer) api("/api/lab/render", { method: "POST", body: { markdown: t.answer } }).then((r) => { answer.innerHTML = r.html; }).catch(() => { answer.textContent = t.answer; });
    const open = new Set();
    const panel = verdictPanel({ targetKind: "turn", targetId: t.id, form: "turn", finished: t.outcome !== "running", feedback: t.feedback, subject: "this answer" });
    put(main, h("div", { class: "page" },
      page(`Chat turn · ${t.provider}${t.model ? " · " + t.model : ""}`, "", [
        h("div", { class: "summary-line" }, stateMark(t.outcome === "done" ? "done" : t.outcome === "running" ? "running" : "failed"),
          h("span", { class: "mono", text: t.outcome }), h("span", { text: when(t.started) }), t.duration_ms != null ? h("span", { text: `took ${ms(t.duration_ms)}` }) : null,
          h("span", { text: `${(t.calls || []).length} tool call${(t.calls || []).length === 1 ? "" : "s"}` }))],
      null, [h("a", { href: "#/activity/turns", text: "AI activity" }), " / turn"]),
      h("div", { class: "detail" },
        h("div", null,
          h("div", { class: "card" }, h("h2", { text: "Asked" }), h("p", { style: { margin: "8px 0 0", whiteSpace: "pre-wrap" }, class: "wrap", text: t.prompt || "(empty)" })),
          h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", { text: "Tools the model called, in order" })),
            (t.calls || []).length ? t.calls.map((c, k) => stepCard({ ...callAsStep(c), seq: k + 1 }, open, false))
              : h("p", { class: "muted", text: "None — it answered without calling a tool." })),
          h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", { text: "Answered" })), answer)),
        h("aside", null, panel.el))));
  }

  async function viewCall(main, trace) {
    const c = await api(`/api/lab/activity/tools/${encodeURIComponent(trace)}`);
    const open = new Set([String(c.trace_id)]);
    const panel = verdictPanel({ targetKind: "tool", targetId: c.trace_id, form: "tool", finished: !c.running, feedback: c.feedback, subject: c.tool });
    const origin = c.turn_id ? h("a", { href: `#/activity/turn/${c.turn_id}`, text: "from a chat turn" })
      : c.test_id ? h("a", { href: `#/tests/${c.test_id}`, text: `from lab test #${c.test_id}` }) : h("span", { class: "muted", text: `source: ${c.source}` });
    put(main, h("div", { class: "page" },
      page(c.tool, c.target || "", [h("div", { class: "summary-line" }, stateMark(c.running ? "running" : c.ok ? "done" : "failed"),
        h("span", { text: when(c.started) }), h("span", { text: `took ${ms(c.duration_ms)}` }), h("span", { text: `returned ${n(c.chars)} characters` }), origin,
        c.provider ? h("span", { class: "chip", text: c.provider }) : null)], null,
      [h("a", { href: "#/activity/tools", text: "AI activity" }), " / tool call"]),
      h("div", { class: "detail" }, h("div", null, stepCard(callAsStep(c), open, true)), h("aside", null, panel.el))));
  }

  // ── verdicts: mine, or the inbox ──────────────────────────
  function targetLink(f) {
    if (f.target_kind === "test") return `#/tests/${f.target_id}`;
    if (f.target_kind === "turn") return `#/activity/turn/${f.target_id}`;
    return `#/activity/call/${encodeURIComponent(f.target_id)}`;
  }
  async function viewFeedback(main, params) {
    const admin = S.user.role === "admin";
    const status = select([["", "any status"], ["open", "open"], ["triaged", "triaged"], ["fixed", "fixed"], ["wontfix", "won't fix"]], params.status ?? (admin ? "open" : ""));
    const target = select([["", "on anything"], ["test", "on tests"], ["turn", "on chat turns"], ["tool", "on tool calls"]], params.target_kind || "");
    const verdict = select([["", "any verdict"], ["correct", "correct"], ["partial", "partly"], ["wrong", "wrong"], ["unsure", "unsure"]], params.verdict || "");
    const apply = () => { location.hash = "#/feedback?" + qs({ status: status.value || (admin ? "all" : ""), target_kind: target.value, verdict: verdict.value }); };
    [status, target, verdict].forEach((el) => el.addEventListener("change", apply));
    const wantStatus = params.status === "all" ? "" : (params.status ?? (admin ? "open" : ""));
    if (params.status === "all") status.value = "";
    const r = await api("/api/lab/feedback?" + qs({ status: wantStatus, target_kind: params.target_kind || "", verdict: params.verdict || "" }));
    const acts = admin ? [
      h("a", { class: "btn sm", href: "/api/lab/feedback/export?format=md", download: "" }, icon("down", "icon sm"), "Markdown"),
      h("a", { class: "btn sm", href: "/api/lab/feedback/export?format=json", download: "" }, icon("down", "icon sm"), "JSON"),
      h("a", { class: "btn sm", href: "/api/lab/export/cases", target: "_blank", rel: "noopener", title: "Resolution tests a tester judged, as scripts/heldout.py cases" }, "Held-out cases"),
    ] : [];
    put(main, page(admin ? "Verdicts inbox" : "My verdicts",
      admin ? "Everything testers reported. Move each along as it is dealt with; export the lot for whoever fixes it."
        : "What you reported, and where each stands. An admin marks them triaged or fixed.",
      [h("div", { class: "filters" }, status, target, verdict),
        r.feedback.length ? r.feedback.map((f) => feedbackCard(f, admin))
          : h("div", { class: "empty" }, h("b", { text: "Nothing here" }), admin ? "No verdicts with this status." : "Verdicts you give on tests and chat turns appear here.")],
      acts));
  }

  function feedbackCard(f, admin) {
    const c = f.context || {};
    const statusSel = select([["open", "open"], ["triaged", "triaged"], ["fixed", "fixed"], ["wontfix", "won't fix"]], f.status);
    const note = h("input", { type: "text", value: f.resolution || "", placeholder: "What was done about it (shown to the tester)" });
    const saveBtn = h("button", { class: "btn sm", type: "button", text: "Save", onclick: async () => {
      saveBtn.disabled = true;
      try {
        const u = await api(`/api/lab/feedback/${f.id}`, { method: "PATCH", body: { status: statusSel.value, resolution: note.value } });
        f.status = u.status;
        toast(`Marked ${u.status}.`);
        refreshCounts();
      } catch (e) { fail(e); } finally { saveBtn.disabled = false; }
    } });
    return h("div", { class: "fb" },
      h("div", { class: "fb-head" }, verdictMark(f.verdict),
        h("span", { class: "kind", text: (c.kind ? (KIND_WORD[c.kind] || c.kind) : f.target_kind) }),
        h("a", { class: "subject", href: targetLink(f), text: (c.subject || f.target_id) + (c.language ? ` · ${c.language}` : "") }),
        h("span", { class: "chip", text: f.status }),
        h("span", { class: "muted", text: `${f.by} · ${ago(f.updated)}` })),
      h("div", { class: "fb-body" },
        c.headline ? h("p", null, h("span", { class: "muted", text: "Result: " }), c.headline) : null,
        c.auto ? h("p", null, h("span", { class: "muted", text: "Automatic check: " }), autoMark(c.auto), " ", c.auto.reason || "") : null,
        Object.entries(f.aspects || {}).length ? h("p", { text: Object.entries(f.aspects).map(([k, v]) => `${k}: ${v}`).join(" · ") }) : null,
        f.issues.length ? h("div", null, f.issues.map((x) => h("span", { class: "flag", text: x }))) : null,
        f.expected ? h("p", null, h("span", { class: "muted", text: "Should be: " }), f.expected) : null,
        f.rating ? h("p", { class: "muted", text: `Rated ${f.rating}/5` }) : null,
        f.notes ? h("p", { text: f.notes }) : null,
        !admin && f.resolution ? h("div", { class: "banner", style: { marginTop: "8px" } }, h("span", null, h("b", { text: "Admin: " }), f.resolution)) : null),
      admin ? h("div", { class: "fb-triage" }, statusSel, note, saveBtn) : null);
  }

  // ── the overview (admin) ──────────────────────────────────
  function kpi(label, value, foot, href) {
    return h("div", { class: "kpi" }, h("div", { class: "label", text: label }),
      href ? h("a", { class: "value", href, text: value }) : h("div", { class: "value", text: value }),
      foot ? h("div", { class: "foot", text: foot }) : null);
  }
  const fill = (good, total) => ({ width: (total ? (100 * good) / total : 0) + "%" });
  function meterRow(name, sub, good, total, text, quiet) {
    return h("div", { class: "meter-row" },
      h("div", { class: "name" }, name, sub ? h("small", { text: sub }) : null),
      h("div", { class: "meter-body" },
        h("div", { class: "meter" + (quiet ? " quiet" : ""), role: "img", "aria-label": `${name}: ${text}` },
          h("i", { style: fill(good, total) })),
        h("div", { class: "meter-text", text })));
  }

  /** One test kind's accuracy: what testers said, and under it, thinner and
   *  grey, what the automatic check said. */
  function accuracyBlock(name, x) {
    const v = x.verdicts;
    const waiting = Math.max(0, (x.states.done || 0) + (x.states.failed || 0) - x.reviewed);
    const said = x.accuracy.total
      ? [h("b", { text: `${n(x.accuracy.good)} of ${n(x.accuracy.total)} correct` }),
        ` (${pct(x.accuracy.good, x.accuracy.total)}) · ${v.partial || 0} partly · ${v.wrong || 0} wrong · ${v.unsure || 0} unsure · ${waiting} waiting`]
      : `nothing judged yet · ${waiting} waiting for a verdict`;
    const auto = x.auto.total ? `automatic check: ${n(x.auto.good)} of ${n(x.auto.total)} pass (${pct(x.auto.good, x.auto.total)})`
      : "automatic check: no expected answers written down";
    return h("div", { class: "meter-row" },
      h("div", { class: "name" }, name, h("small", { text: `${n(x.total)} run` })),
      h("div", { class: "meter-body" },
        h("div", { class: "meter", role: "img", "aria-label": `${name}, judged by testers: ${x.accuracy.good} of ${x.accuracy.total} correct` },
          h("i", { style: fill(x.accuracy.good, x.accuracy.total) })),
        h("div", { class: "meter-text" }, said),
        h("div", { class: "meter thin quiet", role: "img", "aria-label": `${name}, ${auto}` },
          h("i", { style: fill(x.auto.good, x.auto.total) })),
        h("div", { class: "meter-text muted", text: auto })));
  }

  function columnChart(series) {
    const plot = h("div");
    const wrap = h("div", { class: "cols-chart" }, plot);
    let drawnAt = 0;
    const draw = () => {
      const W = Math.max(280, Math.round(wrap.clientWidth || 600));
      if (W === drawnAt) return;
      drawnAt = W;
      put(plot, columnSvg(series, W));
    };
    new ResizeObserver(draw).observe(wrap);
    const table = h("details", { style: { marginTop: "8px" } }, h("summary", { class: "muted", style: { cursor: "pointer", fontSize: "12.5px" }, text: "Show as a table" }),
      h("div", { class: "table-wrap", style: { marginTop: "8px" } }, h("table", { class: "t" },
        h("thead", null, h("tr", null, ["day", "tool calls", "chat turns", "tests"].map((c, k) => h("th", { class: k ? "r" : null, text: c })))),
        h("tbody", null, series.map((d) => h("tr", null, h("td", { class: "mono", text: d.day }),
          h("td", { class: "r", text: n(d.calls) }), h("td", { class: "r", text: n(d.turns) }), h("td", { class: "r", text: n(d.tests) })))))));
    wrap.append(table);
    return wrap;
  }

  function columnSvg(series, W) {
    const H = 190, top = 12, bottom = 24, left = 36;
    const max = Math.max(1, ...series.map((d) => d.calls));
    const step = niceStep(max);
    const ceil = Math.ceil(max / step) * step;
    const band = (W - left) / series.length;
    const barW = Math.min(24, band * 0.6);
    const y = (v) => top + (H - top - bottom) * (1 - v / ceil);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
      "aria-label": "Tool calls per day, last 14 days" });
    for (let v = 0; v <= ceil; v += step) {
      root.append(svg("line", { class: "grid", x1: left, x2: W, y1: y(v), y2: y(v) }),
        svg("text", { class: "axis-label", x: left - 8, y: y(v) + 3.5, "text-anchor": "end" }, compact(v)));
    }
    const labelEvery = band < 34 ? 2 : 1;
    series.forEach((d, i) => {
      const cx = left + band * i + band / 2;
      const hgt = y(0) - y(d.calls);
      const r = Math.min(4, hgt / 2, barW / 2);
      const x0 = cx - barW / 2, y0 = y(d.calls), yb = y(0);
      const bar = hgt > 0
        ? svg("path", { class: "bar", d: `M${x0},${yb} V${y0 + r} Q${x0},${y0} ${x0 + r},${y0} H${x0 + barW - r} Q${x0 + barW},${y0} ${x0 + barW},${y0 + r} V${yb} Z` })
        : svg("g");
      const hit = svg("rect", { class: "hit", x: left + band * i, y: top, width: band, height: H - top - bottom, tabindex: "0",
        "aria-label": `${d.day}: ${d.calls} tool calls, ${d.turns} chat turns, ${d.tests} tests` });
      const on = () => { bar.classList.add("on"); showTip(hit, d.day, [["tool calls", n(d.calls)], ["chat turns", n(d.turns)], ["tests", n(d.tests)]]); };
      const off = () => { bar.classList.remove("on"); hideTip(); };
      hit.addEventListener("pointerenter", on);
      hit.addEventListener("pointerleave", off);
      hit.addEventListener("focus", on);
      hit.addEventListener("blur", off);
      root.append(bar, hit);
      if (i % labelEvery === 0) root.append(svg("text", { class: "axis-label", x: cx, y: H - 7, "text-anchor": "middle" }, d.day.slice(5)));
    });
    return root;
  }

  function niceStep(max) {
    const raw = max / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const f = raw / mag;
    return Math.max(1, (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag);
  }

  function barTable(rows) {
    if (!rows.length) return h("p", { class: "muted", text: "No tool calls recorded yet." });
    const max = Math.max(...rows.map((r) => r.calls));
    return h("div", null,
      h("div", { class: "bar-row head" }, h("span", { text: "tool" }), h("span", { text: "" }), h("span", { class: "r", text: "calls" }), h("span", { class: "r", text: "errors" }), h("span", { class: "r", text: "average" })),
      rows.map((r) => {
        const track = h("div", { class: "track" }, h("i", { style: { width: (100 * r.calls) / max + "%" } }));
        const row = h("div", { class: "bar-row", tabindex: "0" },
          h("a", { class: "tool", href: "#/activity/tools?" + qs({ tool: r.tool }), text: r.tool }), track,
          h("span", { class: "r", text: n(r.calls) }),
          h("span", { class: "r" + (r.errors ? " bad" : " muted"), text: n(r.errors || 0) }),
          h("span", { class: "r muted", text: ms(r.avg_ms) }));
        const on = () => showTip(track, r.tool, [["calls", n(r.calls)], ["errors", n(r.errors || 0)], ["average", ms(r.avg_ms)], ["slowest", ms(r.max_ms)]]);
        row.addEventListener("pointerenter", on);
        row.addEventListener("focus", on);
        row.addEventListener("pointerleave", hideTip);
        row.addEventListener("blur", hideTip);
        return row;
      }));
  }

  async function viewOverview(main) {
    const holder = h("div", { class: "page" }, h("p", { class: "muted", text: "Counting…" }));
    put(main, holder);
    async function load(refresh) {
      const o = await api("/api/lab/overview" + (refresh ? "?refresh=1" : ""));
      put(holder, overviewBody(o, () => load(true)));
    }
    await load(false);
    every(30000, () => load(false));
  }

  function overviewBody(o, refresh) {
    const st = o.store, t = o.tests, a = o.activity, fb = o.feedback;
    const K = t.kinds;
    const storeCard = h("div", { class: "card" },
      h("h2", { text: "DocsStore" }),
      h("p", { class: "sub" }, `${st.kind} · ${st.location}`, st.degraded ? h("span", { class: "bad", text: ` — standing in: ${st.degraded}` }) : null),
      st.ok === false ? h("div", { class: "banner bad", text: st.error }) : h("dl", { class: "kv" },
        h("dt", { text: "Versions" }), h("dd", { text: n(st.versions) }),
        h("dt", { text: "Marked incomplete" }), h("dd", { class: st.incomplete ? "bad" : null, text: n(st.incomplete) }),
        h("dt", { text: "On disk" }), h("dd", { text: bytes(st.bytes) }),
        h("dt", { text: "Counted" }), h("dd", { text: ago(st.measured) })),
      (st.largest || []).length ? h("div", { class: "section", style: { marginTop: "16px" } }, h("p", { class: "muted", style: { margin: "0 0 6px", fontSize: "12.5px" }, text: "Largest" }),
        h("dl", { class: "kv" }, st.largest.map((x) => [h("dt", { class: "mono", text: x.name }), h("dd", { class: "mono soft", text: `${n(x.pages)} pages · ${compact(x.characters)} chars` })]))) : null,
      (st.recent || []).length ? h("div", { class: "section", style: { marginTop: "16px" } }, h("p", { class: "muted", style: { margin: "0 0 6px", fontSize: "12.5px" }, text: "Harvested most recently" }),
        h("dl", { class: "kv" }, st.recent.map((x) => [h("dt", { class: "mono", text: x.name }), h("dd", { class: "mono soft", text: `${x.latest} · ${x.harvested}` })]))) : null);
    const hv = o.harvests;
    const harvestCard = h("div", { class: "card" },
      h("h2", { text: "Harvests in this process" }),
      h("p", { class: "sub", text: "Background harvests the chat started. Lab harvests run in their own processes and show under Tests." }),
      hv.running.length ? hv.running.map((j) => h("div", { style: { marginBottom: "10px" } }, stateMark("running"), " ", h("b", { text: j.label }),
        h("div", { class: "muted", style: { fontSize: "12.5px" }, text: j.line || "" }))) : h("p", { class: "muted", text: "None running." }),
      hv.recent.length ? h("dl", { class: "kv", style: { marginTop: "12px" } }, hv.recent.map((j) => [h("dt", { class: "mono", text: j.label }), h("dd", null, stateMark(j.state === "done" ? "done" : j.state === "running" ? "running" : "failed"), " ", h("span", { class: "muted", text: j.error || j.line || "" }))])) : null,
      h("div", { class: "section", style: { marginTop: "18px" } }, h("p", { class: "muted", style: { margin: "0 0 6px", fontSize: "12.5px" }, text: "The log's recent lines, by kind" }),
        h("dl", { class: "kv" }, Object.entries(o.log.kinds).sort((x, y) => y[1] - x[1]).map(([k, v]) => [h("dt", { class: "mono", text: k }), h("dd", { class: "mono", text: n(v) })])),
        h("p", { style: { margin: "8px 0 0", fontSize: "13px" } }, o.log.errors ? h("a", { class: "bad", href: "#/logs/server?kind=error", text: `${n(o.log.errors)} errors among them →` }) : h("span", { class: "muted", text: "No errors among them." }))));

    return h("div", null,
      h("div", { class: "page-head" }, h("div", null, h("h1", { text: "Overview" }),
        h("p", { class: "lead", text: `What is stored, what testers found, and what the tools did. Counted ${ago(o.generated)}.` })),
      h("div", { class: "acts" }, h("button", { class: "btn sm", type: "button", onclick: refresh }, icon("redo", "icon sm"), "Count again"),
        h("a", { class: "btn sm primary", href: "#/bench", text: "Run a test" }))),
      h("div", { class: "kpis" },
        kpi("Technologies stored", st.ok === false ? "—" : n(st.technologies), st.ok === false ? "store unreachable" : `${n(st.versions)} versions`),
        kpi("Pages stored", st.ok === false ? "—" : compact(st.pages), st.ok === false ? "" : `${compact(st.characters)} characters`),
        kpi("Tests run", n(t.total), t.active ? `${t.active} queued or running` : "none running", "#/tests"),
        kpi("Awaiting a verdict", n(t.pending_review), "finished, not yet judged", "#/review"),
        kpi("Open reports", n((fb.status || {}).open || 0), `${n(fb.total)} verdicts in all`, "#/feedback"),
        kpi("Tool calls", compact(a.calls), `${n(a.errors)} errored · ${n(a.turns)} chat turns`, "#/activity/tools")),
      h("div", { class: "card" }, h("h2", { text: "Accuracy, as testers judged it" }),
        h("p", { class: "sub", text: "Correct out of the tests someone judged correct, partly or wrong. The grey line under each is the automatic check, where an expected answer was written down." }),
        h("div", { class: "meters" }, accuracyBlock("Resolution", K.resolve), accuracyBlock("Content", K.fetch),
          accuracyBlock("Harvest", K.harvest))),
      h("div", { class: "cols", style: { marginTop: "16px" } },
        h("div", { class: "card" }, h("h2", { text: "Tool calls per day" }), h("p", { class: "sub", text: "Last 14 days, from the chat and from lab tests. Hover a day for chat turns and tests." }), columnChart(a.daily)),
        h("div", { class: "card" }, h("h2", { text: "Tool calls by tool" }), h("p", { class: "sub", text: "Every call recorded. Errors are calls that returned an error to the model." }), barTable(a.by_tool))),
      h("div", { class: "cols", style: { marginTop: "16px" } }, storeCard, harvestCard),
      h("div", { class: "cols", style: { marginTop: "16px" } },
        h("div", { class: "card" }, h("h2", { text: "Testers" }), h("p", { class: "sub", text: "Who has been testing." }),
          h("div", { class: "table-wrap" }, h("table", { class: "t" },
            h("thead", null, h("tr", null, ["account", "role", "tests", "verdicts", "last verdict"].map((c, k) => h("th", { class: k === 2 || k === 3 ? "r" : null, text: c })))),
            h("tbody", null, o.testers.map((u) => h("tr", null, h("td", { class: "subject", text: u.username }), h("td", { class: "mono muted", text: u.role }),
              h("td", { class: "r", text: n(u.tests) }), h("td", { class: "r", text: n(u.verdicts) }), h("td", { class: "muted", text: ago(u.last_verdict) }))))))),
        h("div", { class: "card" }, h("h2", { text: "Most reported problems" }), h("p", { class: "sub", text: "Issue tags testers picked, across every verdict." }),
          fb.issues.length ? h("dl", { class: "kv" }, fb.issues.map(([k, v]) => [h("dt", { class: "mono", text: n(v) + "×" }), h("dd", { text: k })]))
            : h("p", { class: "muted", text: "None reported yet." }),
          h("p", { style: { margin: "14px 0 0" } }, h("a", { href: "#/feedback", text: "Open the verdicts inbox →" })))));
  }

  // ── setup (admin) ─────────────────────────────────────────
  async function viewSetup(main) {
    const s = await api("/api/lab/setup-info");
    const featureChip = (f) => h("span", { class: "chip" + (f.ok === true ? " lit" : f.ok === null ? " bad" : ""), text: f.state });
    const explained = (rows, value) => h("dl", { class: "explained" }, rows.map((r) => h("div", { class: "ex-row" },
      h("dt", { text: r.label }),
      h("dd", null, value(r), h("p", { class: "ex-about", text: r.about || r.meaning || "" }),
        r.change ? h("p", { class: "ex-change mono", text: r.change }) : null))));
    const problems = s.features.filter((f) => f.ok === null);
    const st = s.store;
    put(main, page("Setup", "How this DocsForge is configured, and what each setting means. Settings come from the environment or .env and take effect when the server restarts. Secrets are shown as set or not, never echoed.", [
      problems.length ? h("div", { class: "banner bad" }, h("span", null, h("b", { text: problems.length === 1 ? "One thing needs attention: " : `${problems.length} things need attention: ` }),
        problems.map((f) => f.label.toLowerCase()).join(", "), ". Details below.")) : null,
      h("div", { class: "card" }, h("h2", { text: "Features" }),
        h("p", { class: "sub", text: "Switches that change what DocsForge does. Lavender is on, grey is off by choice, red is a capability missing on this machine." }),
        explained(s.features, featureChip)),
      h("div", { class: "cols", style: { marginTop: "16px" } },
        h("div", { class: "card" }, h("h2", { text: "Build" }), h("p", { class: "sub", text: "What is running." }),
          explained(s.build, (r) => h("span", { class: "mono ex-value", text: r.value }))),
        h("div", { class: "card" }, h("h2", { text: "DocsStore" }),
          h("p", { class: "sub", text: "Where harvests are stored and read back from." }),
          explained([
            { label: "Backend", value: st.kind, about: st.kind === "postgres" ? "Postgres: a row per page, ranked full-text search, shared by every DocsForge pointed at it." : "Markdown files: one file per version, zero setup. Search works, unranked." },
            { label: "Location", value: st.location, about: st.kind === "postgres" ? "host:port/database (the password is never shown)." : "The folder the files live in." },
            st.degraded ? { label: "Standing in", value: st.degraded, about: "A database is configured but could not be reached, so the file store is answering instead. It is retried every 15 seconds." } : null,
            { label: "Stored", value: st.ok === false ? (st.error || "unreadable") : `${n(st.technologies)} technologies · ${n(st.pages)} pages`, about: "Everything harvested so far, every version counted." },
          ].filter(Boolean), (r) => h("span", { class: "mono ex-value", text: r.value })))),
      h("div", { class: "card" }, h("h2", { text: "Model providers" }),
        h("p", { class: "sub", text: "What the chat can answer with. Ready means its key is set, its program is installed, or its daemon answered; the key column is the variable it reads. Bounded reasoning, when on, asks the default one." }),
        h("div", { class: "table-wrap" }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ["provider", "model", "ready", "key", "notes"].map((c) => h("th", { text: c })))),
          h("tbody", null, s.providers.map((p) => h("tr", null, h("td", { class: "subject", text: p.label + (p.name === s.default_provider ? " (default)" : "") }),
            h("td", { class: "mono", text: p.model || "" }), h("td", null, stateMark(p.available ? "done" : "cancelled"), " ", h("span", { class: "muted", text: p.available ? "ready" : "not configured" })),
            h("td", { class: "mono muted", text: p.env_key || "none needed" }), h("td", { class: "soft", text: p.notes || "" }))))))),
      h("div", { class: "card" }, h("h2", { text: "Paths" }), h("p", { class: "sub", text: "Files this process reads and writes." }),
        explained(s.paths, (r) => h("span", { class: "mono ex-value" }, r.value || "—",
          r.size != null ? h("span", { class: "muted", text: `  ·  ${bytes(r.size)}` }) : null,
          r.count != null ? h("span", { class: "muted", text: `  ·  ${n(r.count)} remembered` }) : null))),
      h("div", { class: "card" }, h("h2", { text: "Environment" }), h("p", { class: "sub", text: "Every setting DocsForge reads, what it does, and its value here. Unset means the default applies." }),
        h("div", { class: "table-wrap" }, h("table", { class: "t" },
          h("thead", null, h("tr", null, ["variable", "value", "what it does"].map((c) => h("th", { text: c })))),
          h("tbody", null, s.env.map((e) => h("tr", null, h("td", { class: "mono", text: e.key }),
            h("td", { class: "mono wrap " + (e.set ? "" : "muted"), style: { maxWidth: "320px", fontSize: "12.5px" }, text: e.set ? e.value : "unset" }),
            h("td", { class: "soft", style: { minWidth: "300px" }, text: e.about || "" }))))))),
      h("div", { class: "card" }, h("h2", { text: "Tools" }), h("p", { class: "sub", text: "The surface a model is handed — the same over MCP and in the chat. The delete tool appears here only when it is switched on." }),
        h("div", null, s.tools.map((x) => h("span", { class: "arg", style: { display: "inline-block", margin: "0 6px 6px 0" }, text: x })))),
    ]));
  }

  // ── accounts (admin) ──────────────────────────────────────
  async function viewUsers(main) {
    const r = await api("/api/lab/users");
    const act = Object.fromEntries(r.activity.map((x) => [x.id, x]));
    const name = h("input", { type: "text", placeholder: "name", autocomplete: "off" });
    const pass = h("input", { type: "password", placeholder: "8+ characters", autocomplete: "new-password" });
    const role = select([["tester", "tester"], ["admin", "admin"]], "tester");
    const err = h("p", { class: "error", hidden: true });
    const addForm = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      err.hidden = true;
      try {
        await api("/api/lab/users", { method: "POST", body: { username: name.value.trim(), password: pass.value, role: role.value } });
        toast(`Added ${name.value.trim()}.`);
        route();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; }
    } }, h("div", { class: "grid4" }, field("Name", name), field("Password", pass), field("Role", role),
      h("div", { class: "field" }, h("span", { html: "&nbsp;" }), h("button", { class: "btn primary", type: "submit", text: "Add account" }))), err);
    const change = async (u, body, done) => {
      try { await api(`/api/lab/users/${u.id}`, { method: "PATCH", body }); toast(done); route(); } catch (e) { fail(e); route(); }
    };
    put(main, page("Accounts", "Who can sign in to the lab. A tester gets the bench; an admin gets everything.", [
      h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", null, h("tr", null, ["account", "role", "tests", "verdicts", "last sign-in", "status", ""].map((c) => h("th", { text: c })))),
        h("tbody", null, r.users.map((u) => {
          const roleSel = select([["tester", "tester"], ["admin", "admin"]], u.role);
          roleSel.style.width = "auto";
          roleSel.style.height = "30px";
          roleSel.addEventListener("change", () => change(u, { role: roleSel.value }, `${u.username} is now ${roleSel.value}.`));
          const me = u.id === S.user.id;
          return h("tr", null, h("td", { class: "subject", text: u.username + (me ? " (you)" : "") }), h("td", null, roleSel),
            h("td", { class: "r", text: n((act[u.id] || {}).tests) }), h("td", { class: "r", text: n((act[u.id] || {}).verdicts) }),
            h("td", { class: "muted", text: ago(u.last_login) }),
            h("td", null, u.disabled ? h("span", { class: "chip bad", text: "disabled" }) : h("span", { class: "chip", text: "active" })),
            h("td", null, h("div", { style: { display: "flex", gap: "6px", flexWrap: "wrap" } },
              h("button", { class: "btn sm", type: "button", text: "Reset password", onclick: () => {
                const pw = window.prompt(`New password for ${u.username} (8+ characters). Their sessions end.`);
                if (pw) change(u, { password: pw }, `Password reset for ${u.username}.`);
              } }),
              me ? null : h("button", { class: "btn sm" + (u.disabled ? "" : " warn"), type: "button", text: u.disabled ? "Enable" : "Disable",
                onclick: () => change(u, { disabled: !u.disabled }, `${u.username} ${u.disabled ? "enabled" : "disabled"}.`) }))));
        })))),
      h("div", { class: "card", style: { marginTop: "16px" } }, h("h2", { text: "Add an account" }),
        h("p", { class: "sub", text: "Lost the only admin password? From a terminal: python -m docsforge.lab reset NAME" }), addForm),
    ]));
  }

  // ── logs (admin) ──────────────────────────────────────────
  function logSummary(x) {
    switch (x.kind) {
      case "request": return `${x.method} ${x.path} → ${x.status} · ${x.duration_ms} ms`;
      case "tool_call": return `${x.name} ${x.ok ? "ok" : "error"} · ${x.duration_ms} ms` + (x.chars ? ` · ${n(x.chars)} chars` : "") + (x.error ? ` · ${x.error}` : "");
      case "turn": return `${(x.tools || []).join(" → ") || "no tools"} · ${x.outcome} · ${x.provider || ""} · ${x.duration_ms} ms`;
      case "trace_event": return `${x.stage} ${x.state}` + (x.message ? ` — ${x.message}` : "");
      case "harvest": return `${x.label} ${x.state} ${x.phase || ""} · ${n(x.pages)} pages` + (x.error ? ` · ${x.error}` : "");
      case "lab": return `${x.event}${x.user ? " by " + x.user : ""}` + Object.entries(x).filter(([k]) => !["ts", "kind", "event", "user"].includes(k)).map(([k, v]) => ` · ${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("");
      case "error": return `${x.where}: ${x.message}`;
      default: return x.message || JSON.stringify(x);
    }
  }
  async function viewLogs(main, tab, params) {
    const tabs = h("div", { class: "tabs" }, [["server", "Server log"], ["lab", "Lab events"]].map(([id, label]) =>
      h("a", { class: "tab", href: "#/logs/" + id, "aria-selected": String(id === tab), text: label })));
    const holder = h("div");
    put(main, page("Logs", "logs/docsforge.log, newest first — one line per request, tool call, turn, harvest and lab event. And the lab's own audit trail.", [tabs, holder]));
    if (tab === "lab") {
      const r = await api("/api/lab/events?limit=300");
      put(holder, r.events.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", null, h("tr", null, ["when", "who", "what", "detail"].map((c) => h("th", { text: c })))),
        h("tbody", null, r.events.map((e) => h("tr", null, h("td", { class: "muted", text: when(e.ts) }), h("td", { class: "mono", text: e.username || "—" }),
          h("td", { class: "mono", text: e.kind }), h("td", { class: "soft wrap mono", style: { fontSize: "12px" }, text: Object.entries(e.detail || {}).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ") }))))))
        : h("div", { class: "empty", text: "No lab events yet." }));
      return;
    }
    const kinds = [["-request", "all but requests"], ["", "every kind"], ...["request", "tool_call", "turn", "trace_event", "harvest", "lab", "error"].map((k) => [k, k])];
    const wanted = params.kind ?? "-request";
    const kind = select(kinds, wanted);
    const q = h("input", { type: "search", placeholder: "Contains…", value: params.q || "" });
    const limit = select([["200", "200 lines"], ["500", "500 lines"], ["2000", "2,000 lines"]], params.limit || "200");
    const apply = () => { location.hash = "#/logs/server?kind=" + encodeURIComponent(kind.value) + "&" + qs({ q: q.value.trim(), limit: limit.value }); };
    [kind, limit].forEach((el) => el.addEventListener("change", apply));
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
    const list = h("div");
    put(holder, h("div", { class: "filters" }, kind, q, limit, h("span", { class: "spacer" }),
      h("button", { class: "btn sm", type: "button", onclick: () => load() }, icon("redo", "icon sm"), "Reload")), list);
    async function load() {
      const r = await api("/api/lab/logs?" + qs({ kind: wanted, q: params.q || "", limit: params.limit || 200 }));
      put(list, r.lines.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", null, h("tr", null, ["time", "kind", "line"].map((c) => h("th", { text: c })))),
        h("tbody", null, r.lines.map((x) => {
          const bad = x.kind === "error" || (x.kind === "tool_call" && x.ok === false) || (x.kind === "request" && x.status >= 500);
          return h("tr", null, h("td", { class: "muted mono", style: { whiteSpace: "nowrap", fontSize: "12px" }, text: x.ts ? new Date(x.ts * 1000).toLocaleTimeString() : "" }),
            h("td", null, h("span", { class: bad ? "flag" : "flag quiet", text: x.kind || "?" })),
            h("td", { class: "mono wrap", style: { fontSize: "12px" } }, h("details", null, h("summary", { style: { cursor: "pointer", listStyle: "none" }, class: bad ? "bad" : "", text: logSummary(x) }),
              h("pre", { class: "out", style: { marginTop: "6px" }, text: JSON.stringify(x, null, 2) }))));
        })))) : h("div", { class: "empty" }, h("b", { text: "No lines match" }), "The log lives in logs/docsforge.log beside where the server was started."));
    }
    await load();
  }

  // ── your account ──────────────────────────────────────────
  async function viewAccount(main) {
    const cur = h("input", { type: "password", autocomplete: "current-password" });
    const next = h("input", { type: "password", autocomplete: "new-password" });
    const err = h("p", { class: "error", hidden: true });
    put(main, page("Your account", `Signed in as ${S.user.username} (${S.user.role}).`, [
      h("div", { class: "card", style: { maxWidth: "480px" } }, h("h2", { text: "Change password" }),
        h("p", { class: "sub", text: "Every session of yours ends, this one included." }),
        h("form", { onsubmit: async (e) => {
          e.preventDefault();
          err.hidden = true;
          try {
            await api("/api/lab/password", { method: "POST", body: { current: cur.value, new: next.value } });
            toast("Password changed. Sign in again.");
            S.user = null;
            boot();
          } catch (ex) { err.textContent = ex.message; err.hidden = false; }
        } }, field("Current password", cur), field("New password", next, "8 characters or more"),
        h("button", { class: "btn primary", type: "submit", text: "Change password" }), err)),
    ]));
  }

  boot();
})();
