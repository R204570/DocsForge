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
})();
