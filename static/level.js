// The HSK level control in the top bar, on every public page.
//
// One level, one storage key ("hskLevel"), shared with the app — a reader who
// sets HSK 5 in the app and then opens an article should not have to say so
// again. Everything here degrades to the server-rendered HSK 3 view with
// scripting off, which is also what a crawler sees.
(function () {
  "use strict";
  var KEY = "hskLevel";
  // Index 0 is the app's "my list only" mode, which means nothing on a public
  // page (there is no known-words list here), so the bar starts at HSK 1.
  var NAMES = ["My list", "HSK1", "HSK2", "HSK3", "HSK4", "HSK5", "HSK6", "HSK7-9"];
  var slider = document.getElementById("levelSlider");
  var label = document.getElementById("levelLabel");
  if (!slider) return;

  function read() {
    var v = parseInt(localStorage.getItem(KEY) || "3", 10);
    if (isNaN(v) || v < 1) v = 3;      // "my list only" has no meaning here
    return Math.min(v, 7);
  }

  function levelIndex() { return read(); }

  // --- tables whose rows carry a per-level cumulative share ------------------
  // "Easiest first" has to mean easiest FOR YOU, or the ordering silently
  // answers a question the reader did not ask.
  function retable(lv) {
    document.querySelectorAll("table.sortbylevel").forEach(function (tbl) {
      var body = tbl.tBodies[0];
      if (!body) return;
      var rows = [].slice.call(body.rows);
      rows.forEach(function (tr) {
        var bar = tr.querySelector(".lvbar[data-cum]");
        if (!bar) { tr.__k = -1; return; }
        var cum = bar.getAttribute("data-cum").split(",");
        var pct = parseFloat(cum[lv - 1]);
        tr.__k = isNaN(pct) ? -1 : pct;
        var cut = bar.querySelector(".lvcut");
        if (cut) {
          cut.style.left = Math.min(pct, 100).toFixed(2) + "%";
          cut.setAttribute("title", Math.round(pct) + "% known at " + NAMES[lv]);
        }
      });
      rows.sort(function (a, b) { return b.__k - a.__k; });
      // Re-appending in order is a move, not a rebuild: no innerHTML, so links
      // and their event handlers survive.
      rows.forEach(function (tr) { body.appendChild(tr); });
    });
  }

  function apply(lv, persist) {
    if (label) label.textContent = NAMES[lv];
    if (slider.value !== String(lv)) slider.value = String(lv);
    if (persist) localStorage.setItem(KEY, String(lv));
    retable(lv);
    // The /artists scatter has its own slider under the chart. Drive it rather
    // than duplicating its logic, and let it drive us back.
    var sel = document.getElementById("lvselect");
    if (sel && sel.value !== String(lv)) {
      sel.value = String(lv);
      sel.dispatchEvent(new Event("input", { bubbles: true }));
    }
    document.querySelectorAll("[data-forlevel]").forEach(function (el) {
      el.classList.toggle("atlevel",
        el.getAttribute("data-forlevel") === String(lv));
    });
  }

  slider.min = "1";
  slider.max = "7";
  slider.addEventListener("input", function () {
    apply(parseInt(slider.value, 10), true);
  });

  var sel2 = document.getElementById("lvselect");
  if (sel2) {
    sel2.addEventListener("input", function () {
      var lv = parseInt(sel2.value, 10);
      if (lv !== parseInt(slider.value, 10)) apply(lv, true);
    });
  }

  apply(levelIndex(), false);
})();
