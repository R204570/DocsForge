/* The sidebar, shared by all three pages.

   Collapsed it is the icon rail; open it is 260px of labelled links and the
   list of past conversations. Which state you left it in is remembered, so it
   does not reset itself every time you move between pages.

   It also owns conversation storage, because the list is the only thing that
   reads it and "New chat" without a list is a button that destroys work with
   no way back. */

(function () {
  const CHATS = "docsforge.chats";
  const OPEN = "docsforge.sidebar-open";
  const KEEP = 40;          // conversations kept before the oldest is dropped

  const $ = (sel, root = document) => root.querySelector(sel);

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

  // ── storage ────────────────────────────────────────────
  /* Only the Markdown is kept. The rendered HTML is several times larger and
     the server can rebuild it on demand, and localStorage is a few megabytes
     for the whole origin. */
  function read() {
    try {
      const raw = JSON.parse(localStorage.getItem(CHATS) || "[]");
      return Array.isArray(raw) ? raw : [];
    } catch {
      return [];
    }
  }

  function write(list) {
    const trimmed = list
      .slice()
      .sort((a, b) => (b.updated || 0) - (a.updated || 0))
      .slice(0, KEEP);
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        localStorage.setItem(CHATS, JSON.stringify(trimmed));
        return trimmed;
      } catch {
        // Out of room. Drop the oldest and try again rather than silently
        // losing the conversation someone is having right now.
        if (trimmed.length <= 1) return trimmed;
        trimmed.pop();
      }
    }
    return trimmed;
  }

  function save(chat) {
    if (!chat || !chat.turns || !chat.turns.length) return read();
    const list = read().filter((c) => c.id !== chat.id);
    list.unshift({
      id: chat.id,
      title: chat.title || "Untitled",
      updated: Date.now(),
      turns: chat.turns,
    });
    return write(list);
  }

  function remove(id) {
    return write(read().filter((c) => c.id !== id));
  }

  function when(ts) {
    if (!ts) return "";
    const days = Math.floor((Date.now() - ts) / 86400000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return `${days} days ago`;
    return new Date(ts).toLocaleDateString([], { month: "short", day: "numeric" });
  }

  // ── the panel ──────────────────────────────────────────
  const api = {
    read, save, remove,
    activeId: null,
    onOpenChat: null,
    onNewChat: null,
  };

  function isOpen() {
    return localStorage.getItem(OPEN) === "1";
  }

  function setOpen(open) {
    const nav = $("#side-nav");
    if (!nav) return;
    nav.dataset.open = String(open);
    document.body.classList.toggle("nav-open", open);
    const toggle = $("#side-toggle");
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close sidebar" : "Open sidebar");
    toggle.title = open ? "Close sidebar" : "Open sidebar";
    localStorage.setItem(OPEN, open ? "1" : "0");
    if (open) renderChats();
  }

  function renderChats() {
    const list = $("#chat-list");
    if (!list) return;
    list.replaceChildren();

    const chats = read();
    if (!chats.length) {
      list.append(el("p", "side-none", "Conversations you have will be listed here."));
      return;
    }

    chats.forEach((c) => {
      const row = el("div", "chat-row");
      if (c.id === api.activeId) row.setAttribute("aria-current", "true");

      const open = el("button", "chat-open");
      open.type = "button";
      open.title = c.title;
      open.append(el("span", "nm", c.title));
      open.append(el("span", "ago", when(c.updated)));
      open.addEventListener("click", () => {
        if (api.onOpenChat) api.onOpenChat(c.id);
        else location.href = `/?chat=${encodeURIComponent(c.id)}`;
      });

      const del = el("button", "chat-del");
      del.type = "button";
      del.title = `Delete "${c.title}"`;
      del.setAttribute("aria-label", `Delete ${c.title}`);
      del.append(icon("i-trash", "sm"));
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        remove(c.id);
        renderChats();
        document.dispatchEvent(new CustomEvent("chats:changed", { detail: { id: c.id } }));
      });

      row.append(open, del);
      list.append(row);
    });
  }

  api.refresh = renderChats;
  api.setActive = (id) => { api.activeId = id; renderChats(); };

  function mount() {
    const nav = $("#side-nav");
    if (!nav) return;

    $("#side-toggle").addEventListener("click", () => setOpen(nav.dataset.open !== "true"));

    const scrim = $("#side-scrim");
    if (scrim) scrim.addEventListener("click", () => setOpen(false));

    const fresh = $("#new-chat");
    if (fresh) {
      fresh.addEventListener("click", () => {
        if (api.onNewChat) api.onNewChat();
        else location.href = "/";
      });
    }

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setOpen(nav.dataset.open !== "true");
      }
      // On a narrow screen the panel covers the page, so Escape must close it.
      if (e.key === "Escape" && nav.dataset.open === "true"
          && window.matchMedia("(max-width: 760px)").matches) {
        setOpen(false);
      }
    });

    // Start from where it was left, but never covering a narrow screen on load.
    const narrow = window.matchMedia("(max-width: 760px)").matches;
    setOpen(isOpen() && !narrow);
  }

  window.Sidebar = api;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
