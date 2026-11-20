/* Live dashboard: periodic refresh plus project mute/solo (needs ProjectFilter
 * and attachProjectControls). The page is server-rendered, so a refresh re-fetches
 * the same URL with no-store and swaps only the live regions -- the search box
 * and scroll positions are left alone. Keep this file free of the dollar sign:
 * it is inlined after string.Template substitution, but stay consistent.
 */
(function () {
  "use strict";
  var PF = window.ProjectFilter;
  var LIVE_IDS = ["live-stats", "live-sessions", "live-health", "live-entries", "live-projects",
                  "live-agents", "live-updated"];
  var INTERVAL_MS = 20000;
  var state = PF.load(window.localStorage);
  var auto = true, lastLoad = Date.now(), busy = false;

  function setState(next) {
    state = PF.normalize(next);
    PF.save(window.localStorage, state);
    applyFilter();
  }

  function applyFilter() {
    var hidden = 0, keys = {};
    document.querySelectorAll("tr[data-project]").forEach(function (tr) {
      var key = tr.getAttribute("data-project");
      if (tr.classList.contains("project-row")) {
        var muted = PF.isMuted(state, key), solo = PF.isSolo(state, key);
        tr.classList.toggle("muted", muted);
        tr.classList.toggle("solo", solo);
        var tag = tr.querySelector(".tag");
        if (!tag) { tag = document.createElement("span"); tag.className = "tag"; tr.cells[0].appendChild(tag); }
        tag.textContent = solo ? "solo" : (muted ? "muted" : "");
        if (!PF.isVisible(state, key)) { keys[key] = true; }
        return;  // project rows stay listed (dimmed) so they can be unmuted
      }
      var out = !PF.isVisible(state, key);
      tr.classList.toggle("filtered-out", out);
      if (out) { hidden += 1; }
    });
    var note = document.getElementById("filter-note");
    var n = Object.keys(keys).length;
    note.textContent = state.solo ? "Solo: " + state.solo :
      (n ? n + " project" + (n === 1 ? "" : "s") + " muted" : "");
    if (hidden) { note.textContent += " (" + hidden + " rows hidden)"; }
  }

  function wireProjects() {
    var projects = Array.from(document.querySelectorAll("tr[data-project]"), function (tr) { return tr.dataset.project; });
    document.querySelectorAll("tr.project-row").forEach(function (tr) {
      var key = tr.getAttribute("data-project");
      attachProjectControls(tr, key, function () { return state; }, setState, projects);
      tr.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown" && tr.nextElementSibling) { e.preventDefault(); tr.nextElementSibling.focus(); }
        else if (e.key === "ArrowUp" && tr.previousElementSibling) { e.preventDefault(); tr.previousElementSibling.focus(); }
      });
    });
  }

  function ago() {
    var s = Math.round((Date.now() - lastLoad) / 1000);
    document.getElementById("fresh").textContent =
      "Updated " + (s < 5 ? "just now" : s + "s ago") + (auto ? "" : " (auto-refresh paused)");
  }

  function refresh(manual) {
    if (busy) { return Promise.resolve(); }
    busy = true;
    var btn = document.getElementById("refresh-btn");
    if (manual) { btn.textContent = "Refreshing\u2026"; }
    var focused = document.activeElement && document.activeElement.getAttribute
      ? document.activeElement.getAttribute("data-project") : null;
    return fetch(window.location.pathname + window.location.search, { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { if (!r.ok) { throw new Error("HTTP " + r.status); } return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        LIVE_IDS.forEach(function (id) {
          var fresh = doc.getElementById(id), cur = document.getElementById(id);
          if (fresh && cur) { cur.innerHTML = fresh.innerHTML; }
        });
        wireProjects();
        applyFilter();
        if (focused) {
          var again = document.querySelector("tr.project-row[data-project=\"" + CSS.escape(focused) + "\"]");
          if (again) { again.focus(); }
        }
        lastLoad = Date.now();
        ago();
      })
      .catch(function (err) { document.getElementById("fresh").textContent = "Refresh failed: " + err.message; })
      .then(function () { busy = false; btn.textContent = "Refresh"; });
  }

  document.getElementById("refresh-btn").addEventListener("click", function () { refresh(true); });
  document.getElementById("auto-btn").addEventListener("click", function () {
    auto = !auto;
    this.textContent = "Auto-refresh: " + (auto ? "on" : "off");
    this.setAttribute("aria-pressed", String(auto));
    ago();
  });
  document.getElementById("show-all").addEventListener("click", function () { setState(PF.reset()); });
  window.addEventListener("storage", function (e) {
    if (e.key === PF.STORAGE_KEY) { state = PF.load(window.localStorage); applyFilter(); }
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden && auto) { refresh(false); } });
  setInterval(function () { if (auto && !document.hidden) { refresh(false); } }, INTERVAL_MS);
  setInterval(ago, 5000);

  wireProjects();
  applyFilter();
})();
