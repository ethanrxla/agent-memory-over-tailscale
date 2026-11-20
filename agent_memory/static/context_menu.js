/* Shared mouse and keyboard controls for project rows. */
(function (root) {
  "use strict";
  root.attachProjectControls = function (row, key, getState, setState) {
    function mute() { setState(root.ProjectFilter.toggleMute(getState(), key)); }
    function solo() { setState(root.ProjectFilter.toggleSolo(getState(), key)); }

    row.addEventListener("click", mute);
    row.addEventListener("contextmenu", function (event) {
      event.preventDefault();
      solo();
    });
    row.addEventListener("keydown", function (event) {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault();
        solo();
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        mute();
      }
    });
  };
})(typeof self !== "undefined" ? self : this);
