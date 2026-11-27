/* Shared behaviour for the public site. Everything here is enhancement:
   the pages read fully with this script never running. */
(function () {
  var root = document.documentElement;
  root.classList.add("js");
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

  // ── reveals: once, when a block enters the viewport ──
  var targets = document.querySelectorAll(".rv, .rv-list");
  if (reduce.matches || !("IntersectionObserver" in window)) {
    targets.forEach(function (el) { el.classList.add("in"); });
  } else {
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("in"); seen.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -12% 0px", threshold: 0.12 });
    targets.forEach(function (el) { seen.observe(el); });
  }

  // ── wide/narrow diagrams: assistive tech reads the one that is showing ──
  var narrowQuery = window.matchMedia("(max-width: 640px)");
  function labelVariants() {
    document.querySelectorAll(".diagram.wide, .diagram.narrow").forEach(function (svg) {
      var showing = svg.classList.contains("narrow") === narrowQuery.matches;
      svg.setAttribute("aria-hidden", showing ? "false" : "true");
    });
  }
  labelVariants();
  if (narrowQuery.addEventListener) narrowQuery.addEventListener("change", labelVariants);

  // ── figures that count up when their diagram plays ──
  var counters = document.querySelectorAll(".count[data-to]");
  if (counters.length && !reduce.matches && "IntersectionObserver" in window) {
    var tick = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        tick.unobserve(e.target);
        var el = e.target, to = parseInt(el.getAttribute("data-to"), 10) || 0;
        var delay = parseFloat((el.closest("[style*='--d']") || el).style.getPropertyValue("--d")) || 0;
        var t0 = null, dur = 900;
        setTimeout(function () {
          requestAnimationFrame(function step(ts) {
            if (t0 === null) t0 = ts;
            var k = Math.min(1, (ts - t0) / dur), eased = 1 - Math.pow(1 - k, 3);
            el.textContent = String(Math.round(to * eased));
            if (k < 1) requestAnimationFrame(step);
          });
        }, delay);
      });
    }, { threshold: 0.4 });
    counters.forEach(function (el) { tick.observe(el); });
  }

  // ── a copy button on every code block ──
  // What is copied is what to run: the `# …` notes beside a command are for
  // reading (cmd.exe would choke on them), so they are left out, along with
  // the padding that lines them up.
  var copyText = function (value) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(value);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = value; ta.setAttribute("readonly", "");
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
      if (ok) resolve(); else reject();
    });
  };
  var runnable = function (pre) {
    var clone = pre.cloneNode(true);
    clone.querySelectorAll(".c").forEach(function (note) { note.remove(); });
    return clone.textContent.split("\n").map(function (line) { return line.replace(/\s+$/, ""); })
      .join("\n").replace(/^\n+|\n+$/g, "");
  };
  document.querySelectorAll(".codeblock").forEach(function (block) {
    var pre = block.querySelector("pre");
    if (!pre) return;
    var bar = block.querySelector(".bar");
    var first = runnable(pre).split("\n")[0];
    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy " + first);
    var timer = null;
    var flash = function (label, done) {
      button.textContent = label;
      button.classList.toggle("done", done);
      clearTimeout(timer);
      timer = setTimeout(function () {
        button.textContent = "Copy";
        button.classList.remove("done");
      }, 1600);
    };
    button.addEventListener("click", function () {
      copyText(runnable(pre)).then(function () { flash("Copied", true); }, function () {
        // No clipboard here: select the text so Ctrl+C finishes the job.
        var range = document.createRange();
        range.selectNodeContents(pre);
        var sel = window.getSelection();
        sel.removeAllRanges(); sel.addRange(range);
        flash("Press Ctrl+C", false);
      });
    });
    if (bar) bar.appendChild(button);
    else { block.classList.add("has-copy"); block.appendChild(button); }
  });

  // ── the live resolver on the home page: find_docs, from this server ──
  // Without script the form still works: it submits to /find-docs and the
  // browser shows the answer as JSON.
  var form = document.getElementById("try-form");
  if (form && window.fetch) {
    var input = document.getElementById("try-name");
    var go = form.querySelector("button");
    var out = document.getElementById("try-out");
    var REGISTRY = { pypi: "PyPI", npm: "npm", crates: "crates.io" };
    var busy = false;

    var el = function (tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    };
    var shell = function (name) {
      out.hidden = false;
      out.textContent = "";
      var box = el("div", "snippet"), bar = el("div", "bar"), kind = el("span", "k", "resolving");
      bar.appendChild(el("span", "t", 'find_docs(name="' + name + '")'));
      bar.appendChild(kind);
      var body = el("div", "body");
      box.appendChild(bar);
      box.appendChild(body);
      out.appendChild(box);
      return { kind: kind, body: body };
    };
    var line = function (view, cls, text) {
      view.body.textContent = "";
      view.body.appendChild(el("p", cls, text));
    };
    var show = function (name, d, view) {
      var rivals = d.candidates.filter(function (c) { return c.alternative; }).length;
      view.kind.textContent = (d.ambiguous ? "ambiguous · " + rivals + " projects · " : "")
        + (d.remembered ? "from memory · " : "live · ")
        + d.seconds.toFixed(d.seconds < 1 ? 2 : 1) + " s";
      if (!d.candidates.length) {
        line(view, "next", d.note || "Nothing found for " + name + ".");
        return;
      }
      view.body.textContent = "";
      var list = el("ol", "cands");
      d.candidates.forEach(function (c) {
        var best = c.url === d.best;
        var li = el("li", "cand" + (best ? " best" : "") + (c.alternative ? " alt" : "")
          + (c.verified ? "" : " dim"));
        if (/^https?:\/\//i.test(c.url)) {
          var a = el("a", "u", c.url);
          a.href = c.url;
          a.target = "_blank";
          a.rel = "noopener noreferrer nofollow";
          li.appendChild(a);
        } else {
          li.appendChild(el("span", "u", c.url));
        }
        var verdict = c.verified === true ? "verified" : c.verified === false ? "unverified" : "not checked";
        var meta = el("div", "meta");
        if (c.alternative) meta.appendChild(el("b", "", c.label));
        meta.appendChild(document.createTextNode((c.alternative ? " · " : "") + (best ? "best · " : "")
          + verdict + " · confidence " + c.confidence.toFixed(2)));
        li.appendChild(meta);
        if (c.evidence) li.appendChild(el("div", "ev", c.evidence));
        if (c.reason) li.appendChild(el("div", "ev", c.reason));
        list.appendChild(li);
      });
      view.body.appendChild(list);
      var next = el("p", "next");
      if (d.best) {
        next.appendChild(document.createTextNode("Next, "));
        next.appendChild(el("code", "", 'learn_technology(name="' + name + '")'));
        next.appendChild(document.createTextNode(" harvests the best one and stores it."
          + (d.release ? " " + (REGISTRY[d.ecosystem] || d.ecosystem) + "'s current release: " + d.release + "." : "")));
      } else if (d.ambiguous) {
        next.appendChild(document.createTextNode(rivals + " different projects are called " + name
          + ", and nothing says which one is meant, so none was picked. Your assistant picks from context — or name one: "));
        next.appendChild(el("code", "", "ecosystem="));
        next.appendChild(document.createTextNode(" for a package, or the URL you mean to "));
        next.appendChild(el("code", "", "harvest_docs"));
        next.appendChild(document.createTextNode("."));
      } else {
        next.textContent = "Nothing verified, so DocsForge would ask for a URL rather than guess.";
      }
      view.body.appendChild(next);
      // An ambiguous answer's note lists the same projects again; the rows say it.
      if (d.note && !d.ambiguous) view.body.appendChild(el("p", "next", d.note));
      // The answer is the moment someone wants the rest: say where it is.
      var setup = el("p", "next");
      var guide = el("a", "inline", "Set it up on your machine");
      guide.href = "/setup";
      setup.appendChild(guide);
      setup.appendChild(document.createTextNode(" to harvest and read the docs from your own AI coding assistant."));
      view.body.appendChild(setup);
    };
    var run = function (raw) {
      var name = String(raw || "").replace(/\s+/g, " ").trim();
      if (!name || busy) return;
      busy = true;
      go.disabled = true;
      input.value = name;
      var view = shell(name), t0 = Date.now();
      var wait = function () {
        line(view, "wait", "Asking npm, PyPI and crates.io, then reading the pages they name … "
          + Math.round((Date.now() - t0) / 1000) + " s");
      };
      wait();
      var clock = setInterval(wait, 1000);
      var ctl = window.AbortController ? new AbortController() : null;
      var stop = setTimeout(function () { if (ctl) ctl.abort(); }, 90000);
      fetch("/find-docs?name=" + encodeURIComponent(name),
            { headers: { Accept: "application/json" }, signal: ctl ? ctl.signal : undefined })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          clearInterval(clock);
          if (res.ok) { show(name, res.d, view); return; }
          view.kind.textContent = "not answered";
          line(view, "next", res.d.message || "The lookup failed.");
        })
        .catch(function () {
          clearInterval(clock);
          view.kind.textContent = "not answered";
          line(view, "next", "No answer from the server. Try again in a moment.");
        })
        .then(function () { clearTimeout(stop); busy = false; go.disabled = false; });
    };
    form.addEventListener("submit", function (e) { e.preventDefault(); run(input.value); });
    document.querySelectorAll("#try-picks [data-name]").forEach(function (b) {
      b.addEventListener("click", function () { run(b.getAttribute("data-name")); });
    });
  }
})();
