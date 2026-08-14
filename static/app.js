/* DocsForge — shoebox stack.
   Every answer is a card. Cards are addressable, navigable, and editable in
   place: browse and author are two modes of the same object. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const stageEl = $("#stage");
const indexEl = $("#index");
const inputEl = $("#input");
const sendEl = $("#send");
const statusEl = $("#status");
const formEl = $("#ask-form");

const state = {
  cards: [],
  current: -1, // -1 is the intro card
  trail: [],
  busy: false,
  authoring: false,
  tools: [],
  providers: [],
  provider: null,
};

let nextId = 1;

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
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

/* What the detector decided a card was forged from. The board's framed picture
   region, carrying the product's first principle instead of decoration. */
const KIND_ICON = {
  openapi: "k-openapi",
  github: "k-github",
  sitemap: "k-sitemap",
  html: "k-html",
  llms: "k-llms",
  raw: "k-raw",
};
const KIND_LABEL = {
  openapi: "OpenAPI",
  github: "GitHub",
  sitemap: "Sitemap",
  html: "HTML docs",
  llms: "llms.txt",
  raw: "Markdown",
};

function sourceFrame(kind, cls) {
  const frame = el("div", cls ? `frame ${cls}` : "frame");
  frame.title = KIND_LABEL[kind] || "Unknown source";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${KIND_ICON[kind] || "k-raw"}`);
  svg.append(use);
  frame.append(svg);
  return frame;
}

function labelled(tag, cls, text, iconId) {
  const n = el(tag, cls);
  if (iconId) n.append(icon(iconId));
  n.append(el("span", null, text));
  return n;
}

const card = (i) => state.cards[i];
const currentCard = () => (state.current >= 0 ? state.cards[state.current] : null);
const bodyOf = (c) => (c.authored !== null && c.authored !== undefined ? c.authored : c.markdown);

/** The Ask field has no placeholder — a greyed one would be the only value in
    this world that is not ink, paper or dither. The hint lives here instead,
    and working status replaces it while a card is being painted. */
const HINT = "Paste a docs URL and ask about it.";

function setStatus(text) {
  statusEl.textContent = text || HINT;
}

function setBusy(on) {
  state.busy = on;
  sendEl.disabled = on || !inputEl.value.trim();
  refreshMenus();
}

function stamp() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function titleFrom(question, markdown) {
  const heading = (markdown || "").match(/^#{1,3}\s+(.+)$/m);
  if (heading) return heading[1].replace(/[*`_]/g, "").trim().slice(0, 70);

  // Before the answer arrives, the question is the title — but a pasted URL
  // makes a miserable one, so show its host instead of 60 characters of path.
  const q = (question || "")
    .trim()
    .replace(/\s+/g, " ")
    .replace(/https?:\/\/([^\s/]+)\S*/g, (_, host) => host.replace(/^www\./, ""));
  return q.length > 70 ? q.slice(0, 67) + "…" : q || "Untitled card";
}

/** The card head already shows the title; a repeated leading H1 is noise. */
function stripLeadingHeading(html, title) {
  if (!html) return html;
  const box = document.createElement("div");
  box.innerHTML = html;
  const first = box.firstElementChild;
  if (first && /^H[123]$/.test(first.tagName)) {
    const same = first.textContent.trim().toLowerCase() === (title || "").trim().toLowerCase();
    if (same) first.remove();
  }
  return box.innerHTML;
}

// ── the shoebox index ────────────────────────────────────
function renderIndex() {
  indexEl.innerHTML = "";

  if (!state.cards.length) {
    const li = el("li", "index-empty");
    const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    art.setAttribute("class", "shoebox-art");
    art.setAttribute("viewBox", "0 0 120 92");
    art.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-shoebox");
    art.append(use);
    li.append(art);
    li.append(el("p", null, "The stack is empty."));
    li.append(el("p", "dim", "Ask below and the first card gets painted here."));
    indexEl.append(li);
    return;
  }

  state.cards.forEach((c, i) => {
    const li = el("li");
    const btn = el("button", "index-card");
    btn.type = "button";
    if (i === state.current) btn.setAttribute("aria-current", "true");
    if (c.status === "painting") btn.classList.add("painting");

    if (c.kind) btn.append(sourceFrame(c.kind, "sm"));

    const text = el("span", "index-text");
    text.append(el("span", "t", c.title));

    const meta = el("span", "m");
    meta.append(el("span", null, c.status === "error" ? "failed" : `card ${i + 1}`));
    const chars = bodyOf(c).length;
    if (chars) meta.append(el("span", "num", `${chars.toLocaleString()} ch`));
    text.append(meta);
    btn.append(text);

    btn.addEventListener("click", () => goTo(i));
    li.append(btn);
    indexEl.append(li);
  });

  const active = indexEl.querySelector('[aria-current="true"]');
  if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
}

// ── the stage ────────────────────────────────────────────
function renderStage() {
  const c = currentCard();
  stageEl.innerHTML = "";

  if (!c) {
    stageEl.append(introCard());
    return;
  }

  const art = el("article", "card painted");

  // head
  const head = el("header", "card-head");
  if (c.kind) head.append(sourceFrame(c.kind));
  head.append(el("h2", "card-title", c.title));
  const boxes = el("span", "boxes");
  boxes.setAttribute("aria-hidden", "true");
  boxes.append(el("i"), el("i"), el("i"));
  head.append(boxes);
  art.append(head);

  // meta
  const meta = el("div", "card-meta");
  meta.append(el("span", null, `card ${state.current + 1} of ${state.cards.length}`));
  if (c.kind) meta.append(el("span", null, KIND_LABEL[c.kind] || c.kind));
  if (c.model) meta.append(el("span", null, c.model));
  meta.append(el("span", null, c.time));
  if (c.authored !== null && c.authored !== undefined) meta.append(el("span", null, "edited"));
  if (c.status === "done") meta.append(el("span", null, `${bodyOf(c).length.toLocaleString()} characters`));
  art.append(meta);

  // sources
  if (c.sources.length) art.append(sourceList(c));

  if (c.notices.length) {
    const list = el("ul", "sources notices");
    c.notices.forEach((n) => {
      const li = el("li");
      li.append(icon("i-bang", "ico"), el("span", "nm", n));
      list.append(li);
    });
    art.append(list);
  }

  // field
  const field = el("div", "card-field");

  if (c.status === "error") {
    field.append(errorBlock(c.error));
  } else if (state.authoring) {
    const ta = el("textarea", "author");
    ta.value = bodyOf(c);
    ta.spellcheck = false;
    ta.addEventListener("input", () => {
      c.authored = ta.value;
    });
    field.append(ta);
    setTimeout(() => ta.focus(), 0);
  } else if (c.status === "painting") {
    const pre = el("pre", "raw-view streaming");
    pre.textContent = c.markdown;
    field.append(pre);
  } else if (c.html) {
    const md = el("div", "md");
    md.innerHTML = c.html;
    field.append(md);
  } else {
    const pre = el("pre", "raw-view");
    pre.textContent = bodyOf(c);
    field.append(pre);
  }
  art.append(field);

  // foot
  art.append(cardFoot(c));
  stageEl.append(art);
}

function sourceList(c) {
  const ul = el("ul", "sources");
  c.sources.forEach((s) => {
    const li = el("li");
    if (s.running) li.classList.add("running");

    if (s.running) {
      li.append(el("span", "bar"));
    } else {
      li.append(icon(s.ok ? "i-tick" : "i-bang", "ico"));
    }

    const target = s.args && (s.args.url || s.args.out_dir);
    li.append(el("span", "nm", target ? `${s.name} ${target}` : s.name));

    if (!s.running) {
      li.append(el("span", "sz", s.ok ? `${(s.chars || 0).toLocaleString()} ch` : "failed"));
    }
    ul.append(li);
  });
  return ul;
}

function errorBlock(message) {
  const box = el("div", "err");
  box.append(el("span", "chip", "Stopped"));
  box.append(el("p", null, message));
  box.append(el("p", null, "The card stayed in the stack so you can edit the question and ask again."));
  return box;
}

function cardFoot(c) {
  const foot = el("footer", "card-foot");

  const back = labelled("button", "btn", "Back", "i-back");
  back.type = "button";
  back.disabled = !state.trail.length;
  back.addEventListener("click", goBack);
  foot.append(back);

  const author = labelled("button", "btn", state.authoring ? "Done Editing" : "Edit This Card", "i-brush");
  author.type = "button";
  author.disabled = c.status !== "done";
  author.setAttribute("aria-pressed", String(state.authoring));
  author.addEventListener("click", toggleAuthor);
  foot.append(author);

  foot.append(el("span", "spacer"));

  if (c.authored !== null && c.authored !== undefined) {
    const revert = labelled("button", "btn", "Revert");
    revert.type = "button";
    revert.addEventListener("click", revertCard);
    foot.append(revert);
  }

  const copy = labelled("button", "btn", "Copy", "i-copy");
  copy.type = "button";
  copy.disabled = c.status === "painting";
  copy.addEventListener("click", () => copyCard(copy));
  foot.append(copy);

  const dl = labelled("button", "btn primary", "Download .md", "i-down");
  dl.type = "button";
  dl.disabled = c.status === "painting";
  dl.addEventListener("click", () => downloadCard(c));
  foot.append(dl);

  return foot;
}

function introCard() {
  const tpl = document.getElementById("intro-template");
  const art = tpl.content.firstElementChild.cloneNode(true);
  $$(".starter", art).forEach((b) => {
    b.addEventListener("click", () => {
      inputEl.value = b.dataset.q;
      autosize();
      submit();
    });
  });
  return art;
}

// ── navigation ───────────────────────────────────────────
function goTo(index, { push = true, trail = true } = {}) {
  if (index === state.current) return;
  if (state.authoring) state.authoring = false;
  if (trail && state.current !== index) state.trail.push(state.current);

  state.current = index;
  dissolve();
  renderStage();
  renderIndex();
  refreshMenus();

  if (push) {
    const c = currentCard();
    const hash = c ? `#c${c.id}` : "#home";
    if (location.hash !== hash) history.pushState({ index }, "", hash);
  }
}

function goBack() {
  if (!state.trail.length) return;
  const target = state.trail.pop();
  goTo(target, { trail: false });
}

function dissolve() {
  stageEl.classList.remove("dissolving");
  void stageEl.offsetWidth; // restart the animation
  stageEl.classList.add("dissolving");
}

window.addEventListener("popstate", (e) => {
  const idx = e.state && typeof e.state.index === "number" ? e.state.index : -1;
  if (idx !== state.current) goTo(idx, { push: false, trail: false });
});

// ── card actions ─────────────────────────────────────────
function toggleAuthor() {
  const c = currentCard();
  if (!c || c.status !== "done") return;

  if (state.authoring) {
    state.authoring = false;
    // Re-render the edited Markdown through the server's sanitiser.
    fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: bodyOf(c) }),
    })
      .then((r) => r.json())
      .then((d) => {
        c.html = stripLeadingHeading(d.html, c.title);
        renderStage();
        renderIndex();
      })
      .catch(() => {
        c.html = "";
        renderStage();
      });
  } else {
    if (c.authored === null || c.authored === undefined) c.authored = c.markdown;
    state.authoring = true;
    renderStage();
  }
}

function revertCard() {
  const c = currentCard();
  if (!c) return;
  c.authored = null;
  c.html = c.renderedHtml || "";
  state.authoring = false;
  renderStage();
  renderIndex();
}

async function copyCard(btn) {
  const c = currentCard();
  if (!c) return;
  const label = btn.querySelector("span");
  try {
    await navigator.clipboard.writeText(bodyOf(c));
    label.textContent = "Copied";
  } catch {
    label.textContent = "Copy failed";
  }
  setTimeout(() => (label.textContent = "Copy"), 1400);
}

function saveFile(name, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function slug(text) {
  return (text || "card")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48) || "card";
}

function downloadCard(c) {
  saveFile(`docsforge-${slug(c.title)}.md`, bodyOf(c));
}

function downloadStack() {
  if (!state.cards.length) return;
  const body = state.cards
    .map((c) => `<!-- card ${c.id}: ${c.title} -->\n\n${bodyOf(c)}`)
    .join("\n\n---\n\n");
  saveFile(`docsforge-stack-${state.cards.length}-cards.md`, body);
}

function newStack() {
  if (state.busy) return;
  state.cards = [];
  state.trail = [];
  state.current = -1;
  state.authoring = false;
  nextId = 1;
  history.pushState({ index: -1 }, "", "#home");
  renderStage();
  renderIndex();
  refreshMenus();
  inputEl.focus();
}

// ── menus ────────────────────────────────────────────────
function closeMenus() {
  $$(".menu").forEach((m) => {
    m.dataset.open = "false";
    $(".menu-title", m).setAttribute("aria-expanded", "false");
  });
}

function wireMenus() {
  $$(".menu").forEach((menu) => {
    const title = $(".menu-title", menu);

    title.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = menu.dataset.open === "true";
      closeMenus();
      if (!open) {
        menu.dataset.open = "true";
        title.setAttribute("aria-expanded", "true");
        refreshMenus();
      }
    });

    // Once one menu is open, sliding across the bar opens the others.
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
      runAction(btn.dataset.act, btn.dataset.arg);
      return;
    }
    if (!e.target.closest(".menu")) closeMenus();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMenus();
  });
}

function runAction(act, arg) {
  const c = currentCard();
  switch (act) {
    case "new": return newStack();
    case "download": return c && downloadCard(c);
    case "download-all": return downloadStack();
    case "copy": return c && navigator.clipboard.writeText(bodyOf(c)).then(() => setStatus("copied"));
    case "author": return toggleAuthor();
    case "revert": return revertCard();
    case "first": return state.cards.length && goTo(0);
    case "prev": return goTo(Math.max(0, state.current - 1));
    case "next": return goTo(Math.min(state.cards.length - 1, state.current + 1));
    case "last": return goTo(state.cards.length - 1);
    case "goto": return goTo(Number(arg));
    case "library": location.href = "/library"; return undefined;
    case "provider": return pickProvider(arg);
    default: return undefined;
  }
}

/** Switch model provider. The choice rides on the next request, so a stack
    can mix providers — useful when one hits its daily cap mid-session. */
function pickProvider(name) {
  const chosen = state.providers.find((p) => p.name === name);
  if (!chosen || !chosen.available) return;
  state.provider = name;
  $("#model-chip").textContent = `${chosen.label} · ${chosen.model || "cli"}`;
  renderProviderMenu();
  setStatus(`Now using ${chosen.label}.`);
}

function renderProviderMenu() {
  const menu = $("#model-menu");
  if (!menu) return;
  menu.innerHTML = "";

  state.providers.forEach((p) => {
    const li = el("li");
    const b = el("button");
    b.type = "button";
    b.dataset.act = "provider";
    b.dataset.arg = p.name;
    b.disabled = !p.available;
    // Unavailable means different things now: a hosted provider is missing its
    // key, a local one is not running. Say which.
    const reason = p.env_key ? "no key" : "not running";
    b.title = p.available
      ? `${p.notes} — ${p.model || "uses the CLI default model"}`
      : (p.env_key
          ? `Needs ${p.env_key} in .env — ${p.docs}`
          : `Not reachable on this machine — ${p.docs}`);
    b.append(el("span", null, p.label));
    b.append(el("span", "key", p.name === state.provider ? "•" : (p.available ? "" : reason)));
    li.append(b);
    menu.append(li);
  });

  const note = el("li", "empty");
  note.append(el("span", null, "no key = add it to .env · not running = start it locally"));
  menu.append(el("li", "sep"), note);
}

/** Enable/disable menu items against what the stack can actually do. */
function refreshMenus() {
  const c = currentCard();
  const has = Boolean(c);
  const set = (act, enabled) => {
    const b = $(`.menu-drop button[data-act="${act}"]`);
    if (b) b.disabled = !enabled;
  };

  set("new", !state.busy && state.cards.length > 0);
  set("download", has && c.status !== "painting");
  set("download-all", state.cards.length > 0);
  set("copy", has && c.status !== "painting");
  set("author", has && c.status === "done");
  set("revert", has && c.authored !== null && c.authored !== undefined);
  set("first", state.cards.length > 0 && state.current !== 0);
  set("prev", state.current > 0);
  set("next", state.current >= 0 && state.current < state.cards.length - 1);
  set("last", state.cards.length > 0 && state.current !== state.cards.length - 1);

  const go = $("#go-menu");
  go.innerHTML = "";

  // Everything harvested so far lives on its own surface; Go is where you
  // leave this stack for it.
  const store = el("li");
  const storeBtn = el("button");
  storeBtn.type = "button";
  storeBtn.dataset.act = "library";
  storeBtn.append(el("span", null, "DocsStore"));
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  storeBtn.append(el("span", "key", (mac ? "⌘" : "Ctrl+") + "L"));
  store.append(storeBtn);
  go.append(store, el("li", "sep"));

  if (!state.cards.length) {
    const li = el("li", "empty");
    li.append(el("span", null, "No cards yet"));
    go.append(li);
  } else {
    state.cards.forEach((cd, i) => {
      const li = el("li");
      const b = el("button");
      b.type = "button";
      b.dataset.act = "goto";
      b.dataset.arg = String(i);
      b.append(el("span", null, `${i + 1}. ${cd.title}`));
      if (i === state.current) b.append(el("span", "key", "•"));
      li.append(b);
      go.append(li);
    });
  }
}

// ── streaming a card into existence ──────────────────────
function historyForServer() {
  const msgs = [];
  state.cards.forEach((c) => {
    if (c.status === "error") return;
    msgs.push({ role: "user", content: c.question });
    const body = bodyOf(c);
    if (body.trim()) msgs.push({ role: "assistant", content: body });
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

async function ask(question) {
  const prior = historyForServer();

  const c = {
    id: nextId++,
    title: titleFrom(question, ""),
    question,
    markdown: "",
    html: "",
    renderedHtml: "",
    authored: null,
    sources: [],
    notices: [],
    kind: "",
    provider: "",
    model: "",
    status: "painting",
    error: null,
    time: stamp(),
  };
  state.cards.push(c);
  goTo(state.cards.length - 1);
  setBusy(true);
  setStatus("asking…");

  const live = () => currentCard() === c;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [...prior, { role: "user", content: question }],
        provider: state.provider,
      }),
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
          c.markdown += data.text;
          setStatus("painting…");
          if (live()) {
            const pre = $(".raw-view", stageEl);
            if (pre) {
              pre.textContent = c.markdown;
              const f = $(".card-field", stageEl);
              if (f) f.scrollTop = f.scrollHeight;
            }
          }
        } else if (event === "tool") {
          if (data.phase === "start") {
            c.sources.push({ name: data.name, args: data.args, running: true });
            setStatus(`${data.name}…`);
          } else {
            const s = [...c.sources].reverse().find((x) => x.running && x.name === data.name);
            if (s) {
              s.running = false;
              s.ok = data.ok;
              s.chars = data.chars;
              s.kind = data.kind || "";
            }
            if (data.kind) c.kind = data.kind;
            setStatus("reading…");
          }
          if (live()) renderStage();
        } else if (event === "notice") {
          c.notices.push(data.message);
          setStatus(data.message);
          if (live()) renderStage();
        } else if (event === "done") {
          finished = true;
          c.provider = data.provider || "";
          c.model = data.model || "";
          c.markdown = data.markdown;
          c.status = "done";
          c.title = titleFrom(question, data.markdown);
          c.html = stripLeadingHeading(data.html, c.title);
          c.renderedHtml = c.html;
        } else if (event === "error") {
          finished = true;
          c.status = "error";
          c.error = data.message;
        }
      }
    }

    if (!finished) {
      if (c.markdown.trim()) {
        c.status = "done";
        c.title = titleFrom(question, c.markdown);
      } else {
        c.status = "error";
        c.error = "The connection closed before a reply arrived.";
      }
    }
  } catch (err) {
    c.status = "error";
    c.error = String((err && err.message) || err);
  } finally {
    setBusy(false);
    setStatus("");
    if (live()) renderStage();
    renderIndex();
    refreshMenus();
  }
}

// ── composer ─────────────────────────────────────────────
function autosize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 132) + "px";
}

function submit() {
  const text = inputEl.value.trim();
  if (!text || state.busy) return;
  inputEl.value = "";
  autosize();
  ask(text);
}

inputEl.addEventListener("input", () => {
  autosize();
  sendEl.disabled = state.busy || !inputEl.value.trim();
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
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (e.metaKey || e.ctrlKey) {
    const key = e.key.toLowerCase();
    if (key === "n") { e.preventDefault(); newStack(); }
    else if (key === "s") { e.preventDefault(); const c = currentCard(); if (c) downloadCard(c); }
    else if (key === "e") { e.preventDefault(); toggleAuthor(); }
    else if (key === "l") { e.preventDefault(); runAction("library"); }
    return;
  }
  if (typing) return;
  if (e.key === "ArrowLeft") runAction("prev");
  if (e.key === "ArrowRight") runAction("next");
});

// ── boot ─────────────────────────────────────────────────
function bootIntro() {
  // Move the server-rendered intro card into a template so it can be recreated.
  const existing = $("#intro-card");
  const tpl = document.createElement("template");
  tpl.id = "intro-template";
  tpl.content.append(existing.cloneNode(true));
  document.body.append(tpl);
  existing.remove();
  stageEl.append(introCard());
}

/** Menu shortcuts name the key this platform actually uses. */
function labelShortcuts() {
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  $$(".menu-drop .key[data-key]").forEach((k) => {
    k.textContent = (mac ? "⌘" : "Ctrl+") + k.dataset.key;
  });
}

bootIntro();
labelShortcuts();
wireMenus();
renderIndex();
refreshMenus();
sendEl.disabled = true;
history.replaceState({ index: -1 }, "", location.hash || "#home");

fetch("/api/config")
  .then((r) => r.json())
  .then((cfg) => {
    state.tools = cfg.tools || [];
    state.providers = cfg.providers || [];
    state.provider = cfg.provider || null;

    const current = state.providers.find((p) => p.name === state.provider);
    $("#model-chip").textContent = current
      ? `${current.label} · ${current.model || "cli"}`
      : "no provider";
    renderProviderMenu();

    const menu = $("#tools-menu");
    menu.innerHTML = "";
    state.tools.forEach((t) => {
      const li = el("li");
      const b = el("button");
      b.type = "button";
      b.disabled = true;
      b.title = t.description;
      b.append(el("span", null, t.name));
      li.append(b);
      menu.append(li);
    });
    const note = el("li");
    note.className = "empty";
    note.append(el("span", null, "The model calls these; so can any MCP client."));
    menu.append(el("li", "sep"), note);

    if (!cfg.ready) {
      setStatus("No provider configured — add a key to .env, or install the claude CLI.");
    }
  })
  .catch(() => {
    $("#model-chip").textContent = "offline";
  });
