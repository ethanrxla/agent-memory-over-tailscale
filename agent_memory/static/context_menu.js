/* Keyboard-accessible right-click menu for project rows (needs ProjectFilter).
 *
 * attachProjectMenu(row, key, getState, setState) wires one row:
 *   right-click, the ContextMenu key or Shift+F10 opens the menu;
 *   ArrowUp/ArrowDown/Home/End move, Enter/Space activate, Escape or Tab close.
 * Kept free of the dollar sign (see project_filter.js).
 */
(function (root) {
  "use strict";
  var menu = null, owner = null;

  function close(refocus) {
    if (!menu) { return; }
    menu.remove();
    menu = null;
    document.removeEventListener("mousedown", outside, true);
    if (refocus && owner) { owner.focus(); }
    owner = null;
  }

  function outside(e) { if (menu && !menu.contains(e.target)) { close(false); } }

  function open(row, key, getState, setState, x, y) {
    close(false);
    owner = row;
    menu = document.createElement("div");
    menu.className = "ctx-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", "Project actions");
    var items = root.ProjectFilter.menuItems(getState(), key);
    items.forEach(function (item) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.textContent = item.label;
      b.dataset.action = item.action;
      b.addEventListener("click", function () {
        setState(root.ProjectFilter.apply(getState(), item.action, key));
        close(true);
      });
      menu.appendChild(b);
    });
    menu.addEventListener("keydown", function (e) {
      var buttons = Array.prototype.slice.call(menu.querySelectorAll("button"));
      var i = buttons.indexOf(document.activeElement);
      if (e.key === "Escape") { e.preventDefault(); close(true); }
      else if ((e.key === "Enter" || e.key === " ") && i !== -1) { e.preventDefault(); buttons[i].click(); }
      else if (e.key === "Tab") { close(true); }
      else if (e.key === "ArrowDown") { e.preventDefault(); buttons[(i + 1) % buttons.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); buttons[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); buttons[buttons.length - 1].focus(); }
    });
    document.body.appendChild(menu);
    var r = menu.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 4)) + "px";
    menu.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 4)) + "px";
    document.addEventListener("mousedown", outside, true);
    var first = menu.querySelector("button");
    if (first) { first.focus(); }
  }

  function attachProjectMenu(row, key, getState, setState) {
    row.setAttribute("aria-haspopup", "menu");
    row.addEventListener("contextmenu", function (e) {
      e.preventDefault();
      open(row, key, getState, setState, e.clientX, e.clientY);
    });
    row.addEventListener("keydown", function (e) {
      if (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) {
        e.preventDefault();
        var r = row.getBoundingClientRect();
        open(row, key, getState, setState, r.left + 24, r.bottom);
      }
    });
  }

  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && menu) { close(true); } });
  root.attachProjectMenu = attachProjectMenu;
  root.closeProjectMenu = close;
})(typeof self !== "undefined" ? self : this);
