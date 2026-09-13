/* DocsForge — the chat surface.

   A turn is one question and the answer built from it, plus the tool calls
   that went into the answer. Those are shown rather than hidden: knowing an
   answer came from a page that was actually fetched, and how much of it came
   back, is the difference between this and a model guessing.

   Nothing here explains the product. That is /docs. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const turnsEl = $("#turns");
const threadEl = $("#thread");
const inputEl = $("#input");
const sendEl = $("#send");
const footEl = $("#foot");
const formEl = $("#ask-form");
const titleEl = $("#thread-title");
const pickerEl = $("#picker");

const state = {
  chatId: null,
  turns: [],
  busy: false,
  tools: [],
  providers: [],
  provider: null,
  abort: null,
};

const newId = () =>
  Date.now().toString(36) + Math.random().toString(36).slice(2, 7);

// ── helpers ──────────────────────────────────────────────
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

const KIND_LABEL = {
  openapi: "OpenAPI", github: "GitHub", sitemap: "sitemap",
  html: "HTML docs", llms: "llms.txt", raw: "Markdown",
};

const bodyOf = (t) => (t.authored ?? t.markdown);

function stamp() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function bytes(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

/* The resting line under the composer. It names the one thing worth knowing
   before you trust an answer, and points at the page that explains the rest. */
const REST = "Answers come from pages DocsForge fetched. Check anything important.";

function setFoot(text) {
  if (text) {
    footEl.textContent = text;
    return;
  }
  footEl.replaceChildren(document.createTextNode(REST + " "));
  const a = el("a", null, "How it works");
  a.href = "/docs";
  footEl.append(a);
}

function titleFrom(question, markdown) {
  const heading = (markdown || "").match(/^#{1,3}\s+(.+)$/m);
  if (heading) return heading[1].replace(/[*`_]/g, "").trim().slice(0, 70);
  // A pasted URL makes a miserable title; show its host, not 60 characters
  // of path.
  const q = (question || "").trim().replace(/\s+/g, " ")
    .replace(/https?:\/\/([^\s/]+)\S*/g, (_, host) => host.replace(/^www\./, ""));
  return q.length > 64 ? q.slice(0, 61) + "…" : q || "New chat";
}

/** The topbar names the conversation; a repeated leading H1 is noise. */
function stripLeadingHeading(html, title) {
  if (!html) return html;
  const box = el("div");
  box.innerHTML = html;
  const first = box.firstElementChild;
  if (first && /^H[123]$/.test(first.tagName)
      && first.textContent.trim().toLowerCase() === (title || "").trim().toLowerCase()) {
    first.remove();
  }
  return box.innerHTML;
}

/** Wide tables scroll inside their own box instead of forcing the whole
    column sideways. */
function wrapTables(root) {
  $$("table", root).forEach((table) => {
    if (table.parentElement.classList.contains("scroll-x")) return;
    const box = el("div", "scroll-x");
    table.replaceWith(box);
    box.append(table);
  });
}

function atBottom() {
  return threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 120;
}

function scrollDown(force = false) {
  if (force || atBottom()) threadEl.scrollTop = threadEl.scrollHeight;
}

// ── rendering a turn ─────────────────────────────────────
function blankScreen() {
  const blank = el("div", "blank");
  const glyph = el("div", "glyph");
  glyph.append(icon("i-forge"));
  blank.append(glyph);
  blank.append(el("h1", null, "DocsForge"));
  blank.append(el("p", null,
    "Give it a documentation URL. It reads the site and answers from it."));

  const starters = el("div", "starters");
  [
    ["Learn a whole library",
     "Harvest the whole documentation at https://www.effect.website/docs/v3/getting-started/introduction/ and tell me what Effect is for."],
    ["Turn a spec into a table",
     "Fetch https://petstore3.swagger.io/api/v3/openapi.json and give me a table of the pet endpoints with their summaries."],
    ["Read a repo",
     "Read the README at https://github.com/psf/requests and summarise what the library does and how to install it."],
  ].forEach(([label, prompt]) => {
    const b = el("button", "starter", label);
    b.type = "button";
    b.addEventListener("click", () => {
      inputEl.value = prompt;
      autosize();
      submit();
    });
    starters.append(b);
  });

  blank.append(starters);
  return blank;
}

// ── execution trace (the timeline inside one tool call) ──
// A trace event's `id` is stable across re-emissions (a running stage
// ticking its page count re-sends the same id) -- keeping one Map keyed on
// that id, rather than an array, is what turns 360 progress ticks on a big
// harvest into one row that updates instead of 360 rows that accumulate.
const TRACE_STATE_LABEL = {
  queued: "queued", running: "running", completed: "done",
  failed: "failed", skipped: "skipped", cancelled: "cancelled",
};

function ensureTrace(step) {
  if (!step.trace) {
    step.trace = {
      events: new Map(),   // id -> latest TraceEvent
      order: [],           // ids, first-seen order
      expanded: false,
      status: "idle",      // idle | connecting | live | closed | error
      source: null,        // the live EventSource, while connected
      note: "",
    };
  }
  return step.trace;
}

function closeTrace(tr) {
  if (tr.source) {
    tr.source.close();
    tr.source = null;
  }
}

/** Every EventSource any turn in the current conversation still has open.
    Dropping the JS reference to a turn (switching chats, starting a new
    one) does not close its connection on its own -- the browser keeps
    streaming until told to stop, which would otherwise leak one open
    connection per trace panel a user ever expanded. */
function closeAllTraces() {
  state.turns.forEach((t) => {
    (t.steps || []).forEach((s) => {
      if (s.trace) closeTrace(s.trace);
    });
  });
}

function openTrace(step, turn) {
  const tr = ensureTrace(step);
  if (tr.source || !step.traceId) return;
  tr.status = "connecting";
  let es;
  try {
    es = new EventSource(`/api/trace/${encodeURIComponent(step.traceId)}`);
  } catch {
    tr.status = "error";
    tr.note = "Could not open a live connection.";
    return;
  }
  tr.source = es;

  es.addEventListener("trace", (e) => {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch {
      return;
    }
    if (data.type === "heartbeat") return;
    tr.status = "live";
    if (!tr.events.has(data.id)) tr.order.push(data.id);
    tr.events.set(data.id, data);
    repaintStick(turn);
  });
  es.addEventListener("trace_error", (e) => {
    try {
      tr.note = JSON.parse(e.data).message || "This trace is no longer available.";
    } catch {
      tr.note = "This trace is no longer available.";
    }
    tr.status = "error";
    closeTrace(tr);
    repaintStick(turn);
  });
  es.addEventListener("trace_end", () => {
    tr.status = "closed";
    closeTrace(tr);
    repaintStick(turn);
  });
  es.addEventListener("error", () => {
    // A transport-level drop, not a `trace_error` from the server (that has
    // its own listener above and already closes cleanly). EventSource
    // retries on its own; reflect that rather than claiming the work itself
    // stopped -- it may well still be running server-side.
    if (tr.status !== "error" && tr.status !== "closed") {
      tr.status = "reconnecting";
      repaintStick(turn);
    }
  });
}

function traceRowLabel(ev) {
  const bits = [ev.name || "(event)"];
  if (ev.counters && ev.counters.pages) {
    bits.push(ev.counters.expected
      ? `${ev.counters.pages}/${ev.counters.expected} pages`
      : `${ev.counters.pages} pages`);
  }
  return bits.join(" — ");
}

/** The arguments an operation was called with, as one readable line.
    This is the "what was executed" half of the question. */
function traceArgs(meta) {
  const entries = Object.entries(meta || {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== "" && v !== false);
  if (!entries.length) return null;
  const text = entries
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join("  ·  ");
  const line = el("div", "trace-args");
  line.append(el("span", "trace-label", "input"));
  line.append(el("code", null, text));
  return line;
}

/** What the operation returned. Rendered as text rather than parsed as
    Markdown: this view exists to show exactly what the model was handed,
    and rendering it would be showing something else. */
/* FastMCP has no object schema for a tool that returns a bare string, so it
   wraps the value as {"result": "..."} and that JSON is what reaches the
   trace. Unwrapped server-side too; done here as well so a server that has
   not been restarted still renders its output as text rather than as one
   unbroken line of escaped newlines.

   The same narrow rule on both sides: only an object whose single key is
   `result`, holding a string. A tool that genuinely returns JSON keeps it,
   because this panel's job is to show what the tool actually returned. */
function unwrapResult(output) {
  const text = String(output).trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return output;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return output;
  }
  const keys = parsed && typeof parsed === "object" ? Object.keys(parsed) : [];
  if (keys.length === 1 && keys[0] === "result" && typeof parsed.result === "string") {
    return parsed.result;
  }
  return output;
}

function traceOutput(ev) {
  if (!ev.output) return null;
  // Once, so the character count names what is actually on screen. Counting
  // the envelope while showing the contents would be off by the wrapper.
  const shown = unwrapResult(ev.output);
  const box = el("div", "trace-output");
  const head = el("div", "trace-output-head");
  head.append(el("span", "trace-label", ev.state === "failed" ? "error output" : "output"));
  if (ev.omitted) {
    head.append(el("span", "trace-omitted",
      `showing the first ${shown.length.toLocaleString()} characters, ` +
      `${ev.omitted.toLocaleString()} more omitted`));
  }
  box.append(head);
  box.append(el("pre", null, shown));
  return box;
}

function traceRow(ev) {
  const row = el("div", `trace-row trace-${ev.state} trace-${ev.type}`);
  const mk = el("span", "mk");
  if (ev.state === "running") {
    const s = el("div", "spin xs");
    s.setAttribute("aria-hidden", "true");
    mk.append(s);
  } else {
    const glyph = { completed: "i-tick", failed: "i-bang", skipped: "i-bang",
                    cancelled: "i-bang", queued: "i-bang" }[ev.state] || "i-tick";
    mk.append(icon(glyph, "sm"));
  }
  row.append(mk);

  const body = el("div", "trace-body");
  body.append(el("span", "nm", traceRowLabel(ev)));
  const msg = ev.state === "failed" ? (ev.error || ev.message) : ev.message;
  if (msg) body.append(el("span", "trace-msg", msg));
  if (ev.target) body.append(el("span", "trace-target", ev.target));
  row.append(body);

  const state = el("span", "trace-state", TRACE_STATE_LABEL[ev.state] || ev.state);
  row.append(state);
  return row;
}

/** One operation and everything beneath it. Recursive rather than the two
    fixed levels this started with: a tool call now owns its own stages,
    which own their own events, so the depth is data rather than a
    constant. */
function traceNode(ev, byParent, depth) {
  const node = el("div", "trace-node");
  node.append(traceRow(ev));

  const args = traceArgs(ev.metadata);
  if (args) node.append(args);
  const out = traceOutput(ev);
  if (out) node.append(out);

  const children = byParent.get(ev.id) || [];
  if (children.length) {
    const nested = el("div", "trace-children");
    children.forEach((child) => nested.append(traceNode(child, byParent, depth + 1)));
    node.append(nested);
  }
  return node;
}

function renderTracePanel(step) {
  const tr = step.trace;
  const panel = el("div", "trace-panel");
  if (!tr.order.length) {
    const waiting = tr.status === "error"
      ? (tr.note || "This trace is no longer available.")
      : "Waiting for the first event…";
    panel.append(el("div", "trace-empty", waiting));
    return panel;
  }
  // Index by parent, then render from the roots down. A tool call is its
  // own root row, its stages hang beneath it, and their events beneath
  // those -- so the depth comes from the data rather than being fixed here.
  const byParent = new Map();
  tr.order.forEach((id) => {
    const ev = tr.events.get(id);
    const key = ev.parent_id || "";
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(ev);
  });
  (byParent.get("") || []).forEach((ev) => {
    panel.append(traceNode(ev, byParent, 0));
  });
  if (tr.status === "reconnecting") panel.append(el("div", "trace-empty", "Reconnecting…"));
  return panel;
}

function workRow(step, turn) {
  const row = el("div", `step ${step.running ? "run" : step.ok ? "ok" : "bad"}`);
  if (step.running) {
    const s = el("div", "spin");
    s.setAttribute("aria-hidden", "true");
    row.append(s);
  } else {
    const mk = el("span", "mk");
    mk.append(icon(step.ok ? "i-tick" : "i-bang", "sm"));
    row.append(mk);
  }

  row.append(el("span", "nm", step.name));

  const target = (step.args && (step.args.url || step.args.name || step.args.section)) || "";
  row.append(el("span", "arg", target));

  if (!step.running) {
    const bits = [];
    if (step.kind && KIND_LABEL[step.kind]) bits.push(KIND_LABEL[step.kind]);
    if (step.chars) bits.push(bytes(step.chars));
    if (bits.length) row.append(el("span", "sz", bits.join(" · ")));
  }

  if (!step.running && step.traceId) {
    // The whole row is the control, not just the chevron. A 22px target at
    // the end of the row is findable only if you already know it is there,
    // and "what did this tool actually do" is the first question a reader
    // has -- so clicking anywhere on the row answers it. The chevron stays
    // as the affordance that says the row opens at all.
    const tr = ensureTrace(step);
    const toggle = () => {
      tr.expanded = !tr.expanded;
      if (tr.expanded) openTrace(step, turn);
      repaintStick(turn);
    };

    row.classList.add("openable");
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    row.setAttribute("aria-expanded", String(tr.expanded));
    row.title = tr.expanded ? "Hide what this ran" : "Show what this ran and what it returned";
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });

    const chev = el("span", "trace-toggle");
    chev.setAttribute("aria-hidden", "true");
    chev.append(icon("i-chev", "sm"));
    row.append(chev);
  }

  const wrap = el("div", "step-wrap");
  wrap.append(row);
  if (!step.running && step.traceId && step.trace && step.trace.expanded) {
    wrap.append(renderTracePanel(step));
  }
  return wrap;
}

function actionsFor(t) {
  const acts = el("div", "acts");

  const copy = el("button", "ghost");
  copy.type = "button";
  copy.append(icon("i-copy", "sm"), el("span", null, "Copy"));
  copy.addEventListener("click", async () => {
    const label = $("span", copy);
    try {
      await navigator.clipboard.writeText(bodyOf(t));
      label.textContent = "Copied";
    } catch {
      label.textContent = "Copy failed";
    }
    setTimeout(() => (label.textContent = "Copy"), 1400);
  });

  const save = el("button", "ghost");
  save.type = "button";
  save.append(icon("i-save", "sm"), el("span", null, "Download .md"));
  save.addEventListener("click", () => download(t));

  const edit = el("button", "ghost");
  edit.type = "button";
  edit.setAttribute("aria-pressed", String(!!t.editing));
  edit.append(icon("i-edit", "sm"), el("span", null, t.editing ? "Done" : "Edit"));
  edit.addEventListener("click", () => {
    if (t.editing) {
      const box = $("textarea.author", turnsEl.querySelector(`[data-turn="${t.id}"]`));
      if (box) t.authored = box.value;
    }
    t.editing = !t.editing;
    render();
    persist();
  });

  acts.append(copy, save, edit);

  if (t.authored != null) {
    const undo = el("button", "ghost");
    undo.type = "button";
    undo.append(icon("i-undo", "sm"), el("span", null, "Revert"));
    undo.addEventListener("click", () => {
      t.authored = null;
      t.editing = false;
      render();
      persist();
    });
    acts.append(undo);
  }

  const bits = [t.provider && t.model ? `${t.provider} · ${t.model}` : t.provider, t.time]
    .filter(Boolean).join(" · ");
  if (bits) acts.append(el("span", "stamp", bits));
  return acts;
}

function renderTurn(t) {
  const node = el("article", "turn");
  node.dataset.turn = String(t.id);
  // The entrance is for arriving, not for every repaint. A turn that has
  // already been on screen must not slide in again when the thread redraws.
  if (t.seen) node.style.animation = "none";
  t.seen = true;

  const ask = el("div", "ask");
  ask.append(el("div", "bubble", t.question));
  node.append(ask);

  const reply = el("div", "reply");

  if (t.steps.length) {
    const work = el("div", "work");
    t.steps.forEach((s) => work.append(workRow(s, t)));
    reply.append(work);
  }
  t.notices.forEach((m) => reply.append(el("div", "notice", m)));

  if (t.status === "error") {
    const box = el("div", "failed");
    box.append(el("strong", null, "That did not go through"));
    box.append(el("span", null, t.error || "Unknown error."));
    reply.append(box);
    node.append(reply);
    return node;
  }

  if (t.status === "streaming") {
    const pre = el("p", "streaming");
    pre.textContent = t.markdown;
    pre.append(el("span", "caret"));
    reply.append(pre);
    node.append(reply);
    return node;
  }

  if (t.editing) {
    const box = el("textarea", "author");
    box.value = bodyOf(t);
    box.spellcheck = false;
    reply.append(box);
  } else {
    const md = el("div", "md");
    if (t.authored != null) {
      // Edited text has not been through the server's renderer, so it is
      // shown as the plain text it now is rather than guessed at as HTML.
      md.append(el("p", "plain", t.authored));
    } else {
      md.innerHTML = t.html || "";
      wrapTables(md);
    }
    reply.append(md);
  }

  reply.append(actionsFor(t));
  node.append(reply);
  return node;
}

function render() {
  turnsEl.replaceChildren();
  if (!state.turns.length) {
    turnsEl.append(blankScreen());
    titleEl.textContent = "New chat";
    return;
  }
  state.turns.forEach((t) => turnsEl.append(renderTurn(t)));
  titleEl.textContent = state.turns[state.turns.length - 1].title;
}

/** Repaint one turn in place — replacing the whole thread would throw away
    the user's scroll position on every token, and (for a trace update) on
    every stage tick of a harvest that may still be running minutes later. */
function repaintTurn(t) {
  if (!t) return;
  const node = turnsEl.querySelector(`[data-turn="${t.id}"]`);
  if (!node) return render();
  const fresh = renderTurn(t);
  fresh.style.animation = "none";
  node.replaceWith(fresh);
}

/** Same, but only for whichever turn is still streaming — the common case,
    and the only one that used to exist before trace panels could update a
    turn other than the last one. */
function paintTail() {
  repaintTurn(state.turns[state.turns.length - 1]);
}

/** Repaint a specific turn, preserving scroll position the same way the
    main stream loop already does: stick to the bottom only if already
    there, so a user reading history is never yanked down by a background
    harvest's trace tick. */
function repaintStick(t) {
  const stick = atBottom();
  repaintTurn(t);
  if (stick) scrollDown(true);
}

// ── files ────────────────────────────────────────────────
function slug(text) {
  return (text || "answer").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "answer";
}

function download(t) {
  const blob = new Blob([bodyOf(t)], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a");
  a.href = url;
  a.download = `docsforge-${slug(t.title)}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ── the model picker ─────────────────────────────────────
function renderPicker() {
  const menu = $("#model-menu");
  menu.replaceChildren();

  state.providers.forEach((p) => {
    const b = el("button", "menu-item");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.disabled = !p.available;
    // Unavailable means different things: a hosted provider is missing its
    // key, a local one is not running. Say which.
    const why = p.env_key ? `needs ${p.env_key} in .env` : "not running locally";
    b.title = p.available ? p.notes : `${why} — ${p.docs}`;

    const nm = el("div", "nm");
    nm.append(el("div", null, p.label));
    nm.append(el("span", "sub", p.available ? (p.model || "uses the CLI default") : why));
    b.append(nm);

    if (p.name === state.provider) {
      const tick = el("span", "tick");
      tick.append(icon("i-tick", "sm"));
      b.append(tick);
    }
    b.addEventListener("click", () => pick(p.name));
    menu.append(b);
  });

  menu.append(el("div", "menu-note",
    "Greyed out means no key in .env, or the local runtime is not started."));
}

function pick(name) {
  const chosen = state.providers.find((p) => p.name === name);
  if (!chosen || !chosen.available) return;
  state.provider = name;
  $("#model-name").textContent = chosen.label;
  $("#model-chip").title = chosen.model || chosen.notes;
  renderPicker();
  closePicker();
}

function closePicker() {
  pickerEl.dataset.open = "false";
  $("#model-chip").setAttribute("aria-expanded", "false");
}

$("#model-chip").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = pickerEl.dataset.open === "true";
  pickerEl.dataset.open = open ? "false" : "true";
  $("#model-chip").setAttribute("aria-expanded", String(!open));
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#picker")) closePicker();
});

// ── asking ───────────────────────────────────────────────
function historyForServer() {
  const msgs = [];
  state.turns.forEach((t) => {
    if (t.status === "error") return;
    msgs.push({ role: "user", content: t.question });
    const body = bodyOf(t);
    if (body && body.trim()) msgs.push({ role: "assistant", content: body });
  });
  return msgs;
}

function parseEvent(block) {
  let event = "message";
  const data = [];
  for (const raw of block.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

function setBusy(on) {
  state.busy = on;
  sendEl.setAttribute("aria-label", on ? "Stop" : "Send");
  sendEl.title = on ? "Stop (Esc)" : "Send";
  sendEl.replaceChildren(icon(on ? "i-stop" : "i-up"));
  // Never disabled while working — that button is the way out of a run that
  // is taking longer than you want to wait for.
  sendEl.disabled = on ? false : !inputEl.value.trim();
}

async function ask(question) {
  const prior = historyForServer();

  const t = {
    id: state.turns.length + 1,
    title: titleFrom(question, ""),
    question,
    markdown: "",
    html: "",
    authored: null,
    editing: false,
    steps: [],
    notices: [],
    provider: "",
    model: "",
    status: "streaming",
    error: null,
    time: stamp(),
  };
  state.turns.push(t);
  render();
  scrollDown(true);
  setBusy(true);
  setFoot("Working…");

  const controller = new AbortController();
  state.abort = controller;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [...prior, { role: "user", content: question }],
        provider: state.provider,
      }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}${detail ? ` — ${detail.slice(0, 240)}` : ""}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finished = false;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";

      for (const block of blocks) {
        const parsed = parseEvent(block);
        if (!parsed) continue;
        const { event, data } = parsed;

        if (event === "token") {
          t.markdown += data.text;
          setFoot("Writing…");
        } else if (event === "tool") {
          if (data.phase === "start") {
            t.steps.push({ name: data.name, args: data.args, running: true });
            setFoot(`${data.name}…`);
          } else {
            const s = [...t.steps].reverse().find((x) => x.running && x.name === data.name);
            if (s) {
              s.running = false;
              s.ok = data.ok;
              s.chars = data.chars;
              s.kind = data.kind || "";
              s.traceId = data.trace_id || null;
            }
            setFoot("Reading…");
          }
        } else if (event === "notice") {
          t.notices.push(data.message);
          setFoot(data.message);
        } else if (event === "done") {
          finished = true;
          t.provider = data.provider || "";
          t.model = data.model || "";
          t.markdown = data.markdown;
          t.status = "done";
          t.title = titleFrom(question, data.markdown);
          t.html = stripLeadingHeading(data.html, t.title);
        } else if (event === "error") {
          finished = true;
          t.status = "error";
          t.error = data.message;
        }

        const stick = atBottom();
        paintTail();
        if (stick) scrollDown(true);
      }
    }

    if (!finished) {
      if (t.markdown.trim()) {
        t.status = "done";
        t.title = titleFrom(question, t.markdown);
        t.html = "";
        t.authored = t.markdown;   // never rendered by the server, so keep it as text
      } else {
        t.status = "error";
        t.error = "The connection closed before a reply arrived.";
      }
    }
  } catch (err) {
    if (err && err.name === "AbortError") {
      // Stopped on purpose. Whatever arrived is still worth keeping.
      if (t.markdown.trim()) {
        t.status = "done";
        t.title = titleFrom(question, t.markdown);
        t.html = "";
        t.authored = t.markdown;
        t.notices.push("Stopped. This answer is unfinished.");
      } else {
        t.status = "error";
        t.error = "Stopped before anything arrived.";
      }
    } else {
      t.status = "error";
      t.error = String((err && err.message) || err);
    }
  } finally {
    state.abort = null;
    setBusy(false);
    setFoot("");
    render();
    scrollDown();
    persist();
  }
}

// ── composer ─────────────────────────────────────────────
function autosize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 184) + "px";
}

function submit() {
  if (state.busy) {
    if (state.abort) state.abort.abort();
    return;
  }
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = "";
  autosize();
  ask(text);
}

// ── conversations ────────────────────────────────────────
/* Only the Markdown is stored; the rendered HTML is rebuilt from it on the
   way back in. Sidebar owns the storage — this just hands it the current
   conversation whenever it changes. */
function persist() {
  if (!state.turns.length) return;
  Sidebar.save({
    id: state.chatId,
    title: state.turns[state.turns.length - 1].title,
    turns: state.turns.map((t) => ({
      question: t.question,
      markdown: t.markdown,
      authored: t.authored,
      title: t.title,
      // Conversation history and execution telemetry are different things:
      // keep the trace id so a same-session reload can still open the live
      // panel, drop the accumulated events (and the live EventSource, which
      // is not serialisable at all) so chat history does not grow with
      // every page a harvest touched.
      steps: t.steps.map((s) => ({
        name: s.name, args: s.args, running: s.running, ok: s.ok,
        chars: s.chars, kind: s.kind, traceId: s.traceId || null,
      })),
      notices: t.notices,
      provider: t.provider,
      model: t.model,
      status: t.status,
      error: t.error,
      time: t.time,
    })),
  });
  Sidebar.setActive(state.chatId);
}

async function restore(chat) {
  state.chatId = chat.id;
  state.turns = chat.turns.map((t, i) => ({ ...t, id: i + 1, editing: false, html: "" }));
  render();

  // Re-render each answer through the server, so a restored conversation
  // reads exactly like a live one rather than as a wall of raw Markdown.
  await Promise.all(state.turns.map(async (t) => {
    if (t.status !== "done" || t.authored != null || !t.markdown) return;
    try {
      const res = await fetch("/api/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ markdown: t.markdown }),
      });
      t.html = stripLeadingHeading((await res.json()).html, t.title);
    } catch {
      t.authored = t.markdown;   // the text is still worth having
    }
  }));

  render();
  scrollDown(true);
  Sidebar.setActive(state.chatId);
}

function newChat() {
  if (state.busy) return;
  persist();
  closeAllTraces();
  state.chatId = newId();
  state.turns = [];
  history.replaceState(null, "", "/");
  render();
  Sidebar.setActive(state.chatId);
  inputEl.focus();
}

function openChat(id) {
  if (state.busy) return;
  if (id === state.chatId) return;
  persist();
  closeAllTraces();
  const chat = Sidebar.read().find((c) => c.id === id);
  if (chat) restore(chat);
}

inputEl.addEventListener("input", () => {
  autosize();
  if (!state.busy) sendEl.disabled = !inputEl.value.trim();
});

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
});

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  submit();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closePicker();
    if (state.busy && state.abort) state.abort.abort();
    return;
  }
  if (!(e.metaKey || e.ctrlKey)) return;
  const key = e.key.toLowerCase();
  if (key === "n") { e.preventDefault(); newChat(); }
  else if (key === "l") { e.preventDefault(); location.href = "/library"; }
  else if (key === "s") {
    const last = state.turns.filter((t) => t.status === "done").pop();
    if (last) { e.preventDefault(); download(last); }
  }
});

// ── boot ─────────────────────────────────────────────────
Sidebar.onNewChat = newChat;
Sidebar.onOpenChat = openChat;

// Deleting the conversation you are looking at should leave you somewhere
// sensible rather than on a transcript that no longer exists anywhere.
document.addEventListener("chats:changed", (e) => {
  if (e.detail && e.detail.id === state.chatId) {
    closeAllTraces();
    state.chatId = newId();
    state.turns = [];
    render();
    Sidebar.setActive(state.chatId);
  }
});

// Leaving mid-conversation should not lose it.
window.addEventListener("beforeunload", () => {
  persist();
  closeAllTraces();
});

const wanted = new URLSearchParams(location.search).get("chat");
const saved = wanted && Sidebar.read().find((c) => c.id === wanted);

state.chatId = saved ? saved.id : newId();
render();
setBusy(false);
setFoot("");
Sidebar.setActive(state.chatId);
if (saved) restore(saved);
else inputEl.focus();

fetch("/api/config")
  .then((r) => r.json())
  .then((cfg) => {
    state.tools = cfg.tools || [];
    state.providers = cfg.providers || [];
    state.provider = cfg.provider || null;

    const current = state.providers.find((p) => p.name === state.provider);
    $("#model-name").textContent = current ? current.label : "no model";
    if (current) $("#model-chip").title = current.model || current.notes;
    renderPicker();

    if (!cfg.ready) {
      setFoot("No model is configured. Add a key to .env, or start Ollama.");
    }
  })
  .catch(() => {
    $("#model-name").textContent = "offline";
    setFoot("Could not reach the DocsForge server.");
  });
