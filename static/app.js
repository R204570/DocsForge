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

function workRow(step) {
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
  return row;
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
    t.steps.forEach((s) => work.append(workRow(s)));
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

/** While streaming, only the tail changes — repainting the whole thread
    would throw away the user's scroll position on every token. */
function paintTail() {
  const t = state.turns[state.turns.length - 1];
  if (!t) return;
  const node = turnsEl.querySelector(`[data-turn="${t.id}"]`);
  if (!node) return render();
  const fresh = renderTurn(t);
  fresh.style.animation = "none";
  node.replaceWith(fresh);
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
      steps: t.steps,
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
    state.chatId = newId();
    state.turns = [];
    render();
    Sidebar.setActive(state.chatId);
  }
});

// Leaving mid-conversation should not lose it.
window.addEventListener("beforeunload", persist);

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
