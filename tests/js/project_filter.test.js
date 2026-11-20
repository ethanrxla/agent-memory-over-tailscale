// Unit tests for agent_memory/static/project_filter.js (run: node tests/js/project_filter.test.js).
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const PF = require(path.join(__dirname, "..", "..", "agent_memory", "static", "project_filter.js"));

function memStorage(initial) {
  const data = Object.assign({}, initial);
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, data };
}

// empty / normalize / load tolerate garbage
assert.deepEqual(PF.load(memStorage()), { muted: [], solo: null });
assert.deepEqual(PF.load(memStorage({ [PF.STORAGE_KEY]: "{not json" })), { muted: [], solo: null });
assert.deepEqual(PF.normalize({ muted: ["a", "a", 3, ""], solo: 7 }), { muted: ["a"], solo: null });

// mute toggles, persists, round-trips
let s = PF.empty();
s = PF.toggleMute(s, "a");
assert.ok(PF.isMuted(s, "a") && !PF.isVisible(s, "a") && PF.isVisible(s, "b"));
const st = memStorage();
PF.save(st, s);
assert.deepEqual(PF.load(st), s);
s = PF.toggleMute(s, "a");
assert.ok(!PF.isMuted(s, "a") && PF.isVisible(s, "a"));
assert.ok(!PF.isFiltered(s));

// solo shows only that project, wins over mute, toggles off, showAll clears it
s = PF.toggleMute(PF.empty(), "a");
s = PF.toggleSolo(s, "a");
assert.ok(PF.isVisible(s, "a") && !PF.isVisible(s, "b"));
assert.ok(PF.isSolo(s, "a"));
assert.deepEqual(PF.toggleSolo(s, "a"), PF.empty());
assert.equal(PF.toggleSolo(s, "b").solo, "b");
assert.equal(PF.showAll(s).solo, null);
assert.deepEqual(PF.showAll(s).muted, ["a"]); // show all clears solo, not mutes
assert.deepEqual(PF.reset(), { muted: [], solo: null });

// muting the soloed project drops the solo instead of blanking the view
s = PF.toggleMute(PF.toggleSolo(PF.empty(), "a"), "a");
assert.equal(s.solo, null);
assert.ok(PF.isMuted(s, "a"));

// Clicking a hidden project during solo reveals both and keeps the rest muted.
s = PF.toggleSolo({ muted: ["a", "b", "archived"], solo: null }, "a");
s = PF.toggleMute(s, "b", ["a", "b", "c"]);
assert.deepEqual(s, { muted: ["archived", "c"], solo: null });
assert.deepEqual(PF.toggleMute(s, "b").muted, ["archived", "c", "b"]);

// Re-solo after expanding a selection, then unsolo to restore every channel.
s = PF.toggleSolo(s, "b");
assert.deepEqual(PF.toggleSolo(s, "b"), PF.empty());

// functions never mutate their input
const frozen = Object.freeze({ muted: Object.freeze(["x"]), solo: null });
PF.toggleMute(frozen, "y"); PF.toggleSolo(frozen, "y"); PF.apply(frozen, "reset", "x");

// context-menu items reflect state
const labels = (st2, key) => PF.menuItems(st2, key).map((i) => i.action);
assert.deepEqual(labels(PF.empty(), "a"), ["solo", "mute"]);
assert.deepEqual(labels(PF.toggleMute(PF.empty(), "a"), "a"), ["solo", "unmute", "reset"]);
assert.deepEqual(labels(PF.toggleSolo(PF.empty(), "a"), "a"), ["unsolo", "mute"]);
assert.deepEqual(labels(PF.toggleSolo(PF.empty(), "a"), "b"), ["solo", "mute", "showall"]);
assert.equal(PF.apply(PF.empty(), "solo", "a").solo, "a");
assert.equal(PF.apply(PF.toggleSolo(PF.empty(), "a"), "showall", "b").solo, null);
assert.deepEqual(PF.apply(PF.empty(), "nonsense", "a"), PF.empty());

// sort by last activity, newest first, unknown last, ties by key
const meta = {
  old: { last_active: "2026-01-01T00:00:00Z" },
  new: { last_active: "2026-11-18T09:00:00Z" },
  mid: { last_active: "2026-06-01T00:00:00+00:00" },
  none: {},
};
assert.deepEqual(PF.sortByActivity(["none", "old", "new", "mid", "zzz"], meta), ["new", "mid", "old", "none", "zzz"]);
const input = ["b", "a"];
PF.sortByActivity(input, {});
assert.deepEqual(input, ["b", "a"]); // not sorted in place

console.log("ok project_filter");
