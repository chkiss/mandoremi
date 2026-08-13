/* Zero-backend usage telemetry: fire-and-forget beacons to /t, which nginx
   answers 204 and logs to its own file (the query string lands in the log).
   Nothing here can break the app — every path is wrapped.

   Privacy rule for this app: it has real accounts, so a beacon must never
   carry an email, a password, a song title, or a line of lyrics. Identifiers
   are numeric row ids and lengths only, plus a random per-page-load sid so a
   session can be followed without knowing who it is. */
"use strict";

var TLM = (function () {
  var sid = Math.random().toString(36).slice(2, 10); // one id per page load
  // Silent in dev: a localhost run has no /t endpoint and the 404s would spam
  // the console.
  var ENABLED = typeof location !== "undefined" &&
    !/^(localhost|127\.|0\.0\.0\.0|\[::1\])/.test(location.hostname);

  function tlog(ev, data) {
    try {
      if (!ENABLED) return;
      var q = new URLSearchParams({ev: ev, sid: sid});
      if (data) {
        for (var k in data) {
          if (Object.prototype.hasOwnProperty.call(data, k)) {
            q.set(k, String(data[k]).slice(0, 180));
          }
        }
      }
      var url = "/t?" + q.toString();
      if (!(navigator.sendBeacon && navigator.sendBeacon(url))) {
        fetch(url, {keepalive: true}).catch(function () {});
      }
    } catch (e) { /* telemetry must never throw */ }
  }

  // Global error capture, with whatever app context the caller can supply.
  function installErrorCapture(ctx) {
    try {
      window.addEventListener("error", function (e) {
        tlog("err", Object.assign({
          m: e.message || "",
          at: ((e.filename || "").split("/").pop() || "") + ":" + e.lineno
        }, safeCtx(ctx)));
      });
      window.addEventListener("unhandledrejection", function (e) {
        tlog("rej", Object.assign({m: String(e.reason || "")}, safeCtx(ctx)));
      });
    } catch (e) { /* ditto */ }
  }

  function safeCtx(ctx) {
    try { return ctx ? ctx() : {}; } catch (e) { return {}; }
  }

  return {log: tlog, installErrorCapture: installErrorCapture, sid: sid};
})();
