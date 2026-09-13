/* Background harvests, made visible.
 *
 * A harvest that outlives the 25s deadline keeps running on a thread nobody
 * could see. It reported into no log, no panel and — with the `claudecode`
 * provider, whose tools run in a subprocess of their own — not even into the
 * `list_knowledge_base` that was supposed to report it. Documentation simply
 * appeared in DocsStore some minutes later, as though from nowhere.
 *
 * This polls /api/harvests and shows what is in flight, on every page. It owns
 * no layout: the card is fixed, and when nothing is running there is no card.
 */
(function () {
  "use strict";

  var BUSY_MS = 2000;    // something is running: often enough to feel live
  var IDLE_MS = 15000;   // nothing is: often enough to notice one starting
  var LINGER_MS = 20000; // how long a finished harvest stays on screen

  var host = null;
  var timer = null;
  var seen = Object.create(null);   // id -> state we last rendered
  var settled = [];                 // {job, at} recently finished/failed

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function secs(n) {
    n = Math.max(0, Math.round(n || 0));
    if (n < 90) return n + "s";
    var m = Math.floor(n / 60);
    return m < 60 ? m + "m" : Math.floor(m / 60) + "h " + (m % 60) + "m";
  }

  function mount() {
    if (host) return host;
    host = el("div", "harvest-tracker");
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");
    host.hidden = true;
    document.body.appendChild(host);
    return host;
  }

  /* One row. `fraction` is null wherever the backend has no honest
   * denominator — a crawl frontier grows as it is walked — and in that case no
   * bar is drawn at all rather than a made-up one. */
  function row(job, done) {
    var node = el("div", "harvest-row" + (done ? " harvest-" + job.state : ""));

    var head = el("div", "harvest-head");
    head.append(el("span", "harvest-name", job.label));
    head.append(el("span", "harvest-time", secs(job.elapsed)));
    node.append(head);

    var says = job.state === "stalled"
      ? "stopped reporting — the process running it most likely exited"
      : job.state === "failed"
        ? (job.error || "failed")
        : job.state === "done"
          ? "finished"
          : job.line || job.phase || "working";
    node.append(el("div", "harvest-line", says));

    if (job.state === "running") {
      var track = el("div", "harvest-bar");
      var fill = el("i", null);
      if (typeof job.fraction === "number") {
        fill.style.width = Math.round(job.fraction * 100) + "%";
      } else {
        // No denominator, so no claim: an indeterminate sweep says "working"
        // without implying how much is left.
        track.classList.add("unknown");
      }
      track.append(fill);
      node.append(track);
    }
    return node;
  }

  function paint(running) {
    var box = mount();
    var now = Date.now();
    settled = settled.filter(function (s) { return now - s.at < LINGER_MS; });

    if (!running.length && !settled.length) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }

    var frag = document.createDocumentFragment();
    if (running.length) {
      frag.append(el("div", "harvest-title",
        running.length === 1 ? "Harvesting" : running.length + " harvests running"));
    }
    running.forEach(function (job) { frag.append(row(job, false)); });
    settled.forEach(function (s) { frag.append(row(s.job, true)); });

    box.replaceChildren(frag);
    box.hidden = false;
  }

  function reconcile(data) {
    var running = data.running || [];
    var live = Object.create(null);
    running.forEach(function (j) { live[j.id] = j.state; });

    // A job we were watching that is no longer running has just settled.
    // Find how it ended in `recent` rather than assuming it succeeded.
    Object.keys(seen).forEach(function (id) {
      if (live[id]) return;
      var ended = (data.recent || []).filter(function (j) { return j.id === id; })[0];
      if (!ended) return;
      settled.push({ job: ended, at: Date.now() });
      if (ended.state === "done") {
        window.dispatchEvent(new CustomEvent("docsforge:harvest-finished",
          { detail: ended }));
      }
    });

    seen = live;
    paint(running);
    return running.length > 0 || settled.length > 0;
  }

  function poll() {
    fetch("/api/harvests", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        var busy = data ? reconcile(data) : false;
        schedule(busy ? BUSY_MS : IDLE_MS);
      })
      .catch(function () {
        // The server going away is not worth shouting about; keep the slow
        // beat so the tracker reappears on its own when it comes back.
        schedule(IDLE_MS);
      });
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(poll, ms);
  }

  // A hidden tab does not need a two-second beat; catch up on return.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) schedule(200);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", poll);
  } else {
    poll();
  }
})();
