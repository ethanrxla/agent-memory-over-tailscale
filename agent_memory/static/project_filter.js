/* Project mute/solo state for the dashboard and the vector-tree sidebar.
 *
 * Pure functions over a plain {muted: [...], solo: key|null} object so they
 * can be unit-tested under node (tests/test_project_filter_js.py) and shared
 * by both pages, which inline this file. Persisted in localStorage under one
 * key, so muting a project on /graph also hides it on / and vice versa.
 * Keep this file free of the dollar sign: the dashboard template is rendered
 * with string.Template.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) { module.exports = api; }
  else { root.ProjectFilter = api; }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  var STORAGE_KEY = "agent-memory.project-filter.v1";

  function empty() { return { muted: [], solo: null }; }

  function normalize(raw) {
    if (!raw || typeof raw !== "object") { return empty(); }
    var muted = Array.isArray(raw.muted) ? raw.muted.filter(function (k) {
      return typeof k === "string" && k.length > 0;
    }) : [];
    muted = muted.filter(function (k, i) { return muted.indexOf(k) === i; });
    var solo = typeof raw.solo === "string" && raw.solo.length > 0 ? raw.solo : null;
    return { muted: muted, solo: solo };
  }

  function load(storage) {
    try { return normalize(JSON.parse(storage.getItem(STORAGE_KEY) || "null")); }
    catch (e) { return empty(); }
  }

  function save(storage, state) {
    try { storage.setItem(STORAGE_KEY, JSON.stringify(normalize(state))); } catch (e) { /* private mode */ }
  }

  function isMuted(state, key) { return normalize(state).muted.indexOf(key) !== -1; }
  function isSolo(state, key) { return normalize(state).solo === key; }

  // Solo wins over mute: soloing a muted project shows it (and only it).
  function isVisible(state, key) {
    var s = normalize(state);
    if (s.solo) { return key === s.solo; }
    return s.muted.indexOf(key) === -1;
  }

  function isFiltered(state) {
    var s = normalize(state);
    return Boolean(s.solo) || s.muted.length > 0;
  }

  function toggleMute(state, key, projects) {
    var s = normalize(state);
    if (s.solo && s.solo !== key && projects) {
      var muted = new Set(s.muted.concat(projects));
      muted.delete(s.solo);
      muted.delete(key);
      return { muted: Array.from(muted), solo: null };
    }
    if (s.muted.indexOf(key) !== -1) {
      return { muted: s.muted.filter(function (k) { return k !== key; }), solo: s.solo };
    }
    // Muting the soloed project would leave nothing on screen: drop the solo.
    return { muted: s.muted.concat([key]), solo: s.solo === key ? null : s.solo };
  }

  function toggleSolo(state, key) {
    var s = normalize(state);
    if (s.solo === key) { return empty(); }
    return { muted: s.muted, solo: key };
  }

  function showAll(state) { return { muted: normalize(state).muted, solo: null }; }
  function reset() { return empty(); }

  // Context-menu entries for one project, in display order.
  function menuItems(state, key) {
    var s = normalize(state);
    var items = [];
    items.push(s.solo === key ? { action: "unsolo", label: "Unsolo (show all)" } : { action: "solo", label: "Solo" });
    items.push(isMuted(s, key) ? { action: "unmute", label: "Unmute" } : { action: "mute", label: "Mute" });
    if (s.solo && s.solo !== key) { items.push({ action: "showall", label: "Show all" }); }
    if (s.muted.length) { items.push({ action: "reset", label: "Unmute all" }); }
    return items;
  }

  function apply(state, action, key) {
    switch (action) {
      case "solo": case "unsolo": return toggleSolo(state, key);
      case "mute": case "unmute": return toggleMute(state, key);
      case "showall": return showAll(state);
      case "reset": return reset();
      default: return normalize(state);
    }
  }

  // Most recently active first; projects with no activity sink to the bottom.
  function sortByActivity(keys, meta) {
    meta = meta || {};
    return keys.slice().sort(function (a, b) {
      var ta = (meta[a] && meta[a].last_active) || "";
      var tb = (meta[b] && meta[b].last_active) || "";
      var da = ta ? Date.parse(ta) : NaN, db = tb ? Date.parse(tb) : NaN;
      var va = isNaN(da) ? -Infinity : da, vb = isNaN(db) ? -Infinity : db;
      if (va !== vb) { return vb - va; }
      return a < b ? -1 : a > b ? 1 : 0;
    });
  }

  return {
    STORAGE_KEY: STORAGE_KEY, empty: empty, normalize: normalize, load: load, save: save,
    isMuted: isMuted, isSolo: isSolo, isVisible: isVisible, isFiltered: isFiltered,
    toggleMute: toggleMute, toggleSolo: toggleSolo, showAll: showAll, reset: reset,
    menuItems: menuItems, apply: apply, sortByActivity: sortByActivity
  };
});
