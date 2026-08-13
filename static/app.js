/* HSK Lyrics Analyzer frontend. Level codes: 1-7 HSK (7 = 7-9), 8 beyond, 9 unknown, 0 filler. */
"use strict";

const view = document.getElementById("view");
const popover = document.getElementById("popover");
const slider = document.getElementById("levelSlider");
const levelLabel = document.getElementById("levelLabel");

const LEVEL_NAMES = {0:"Pre-HSK1",1:"HSK1",2:"HSK2",3:"HSK3",4:"HSK4",5:"HSK5",6:"HSK6",7:"HSK7-9",8:"Beyond HSK",9:"Unknown"};
const DIST_COLORS = {1:"#4c9f50",2:"#7ab648",3:"#c2b93a",4:"#d99a2b",5:"#d96f2b",6:"#c94f38",7:"#a83a4f",8:"#8a8a90",9:"#c4c4c8"};

let learnerLevel = parseInt(localStorage.getItem("hskLevel") || "3");
// Migration: the retired "custom vocabulary only" checkbox was a shortcut for
// level 0. One model now: known = HSK level + personal list, always.
if (localStorage.getItem("customVocab") === "1") { learnerLevel = 0; localStorage.setItem("hskLevel", "0"); }
localStorage.removeItem("customVocab");
let knownCount = 0;  // size of the personal known-words list (0 when anonymous)

function effLevel() { return learnerLevel; }
function levelTag() {
  if (!knownCount) return LEVEL_NAMES[learnerLevel];
  return learnerLevel === 0 ? "my list" : `${LEVEL_NAMES[learnerLevel]} + my list`;
}
// Telemetry. Beacons carry ids, counts and levels — never an email, a title,
// or a line of lyrics. See static/telemetry.js.
const tlog = (ev, data) => TLM.log(ev, data);
TLM.installErrorCapture(() => ({
  lv: learnerLevel,
  p: (location.hash || "#home").split("/")[0],
}));

let currentSong = null;      // analysis being viewed
let currentPlaylist = null;  // playlist data being viewed
let homePlaylists = null;    // playlist summaries on the home view

function rerenderLevelViews() {
  if (currentSong) renderSongDynamic();
  if (currentPlaylist) renderPlaylistDynamic();
  if (homePlaylists && document.getElementById("plList")) renderHomePlaylistTable();
}

function syncLevelControls() {
  slider.value = learnerLevel;
  levelLabel.textContent = knownCount && learnerLevel === 0 ? "My list only" : LEVEL_NAMES[learnerLevel];
}
syncLevelControls();
slider.addEventListener("input", () => {
  learnerLevel = parseInt(slider.value);
  localStorage.setItem("hskLevel", learnerLevel);
  syncLevelControls();
  rerenderLevelViews();
  tlog("level", {lv: learnerLevel});
});
document.getElementById("brand").addEventListener("click", () => nav("home"));
document.getElementById("logoutBtn").addEventListener("click", async () => {
  await api("POST", "/api/logout"); showAuth();
});
document.addEventListener("click", e => {
  if (!e.target.closest(".tok") && !e.target.closest("#popover")) popover.classList.add("hidden");
});

async function api(method, url, body) {
  const opts = {method, headers: {}};
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  // 401 means "session gone" — except on the auth endpoints themselves, where
  // it's "wrong credentials" and the real message must reach the form
  if (r.status === 401 && url !== "/api/login" && url !== "/api/register") {
    showAuth(); throw new Error("Not logged in");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    // Log the route shape, not the row id, so failures group in the log. Auth
    // endpoints are skipped: a bad password is already logged as auth ok=0.
    if (url !== "/api/login" && url !== "/api/register") {
      tlog("apierr", {ep: `${method} ${url.replace(/\d+/g, "#")}`, st: r.status,
                      m: data.detail || r.statusText});
    }
    throw new Error(data.detail || r.statusText);
  }
  return data;
}

function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
function platformName(url) {
  url = (url || "").toLowerCase();
  if (url.includes("spotify")) return "Spotify";
  if (url.includes("apple")) return "Apple Music";
  if (url.includes("youtu")) return "YouTube";
  if (url.includes("163")) return "NetEase";
  return "a streaming platform";
}
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

// "artist — " prefix, linked to the public difficulty page when one exists.
// artist_slug is resolved server-side (app/public.py): the lookup folds
// traditional to simplified, which we cannot do in the browser. No slug means
// no public page for that artist, so we render plain text rather than a 404.
function artistPrefix(s) {
  if (!s.artist) return "";
  const name = esc(s.artist);
  const linked = s.artist_slug
    ? `<a class="artistlink" href="/artist/${encodeURIComponent(s.artist_slug)}"
         title="How hard are ${name}'s lyrics?">${name}</a>`
    : name;
  return linked + " — ";
}

// The title, linked to its public difficulty page when the song is in the
// shared corpus. song_path is null for a song nobody has seeded, so we never
// render a link that 404s.
function songTitleHtml(s) {
  const t = esc(s.title);
  return s.song_path
    ? `<a class="artistlink" href="${esc(s.song_path)}"
         title="Difficulty breakdown for ${t}">${t}</a>`
    : t;
}

// Artist links sit inside rows whose own click opens the song; without this the
// browser would follow the link AND navigate the SPA underneath it.
document.addEventListener("click", ev => {
  const a = ev.target.closest && ev.target.closest("a.artistlink");
  if (a) ev.stopPropagation();
}, true);
function pct(x) { return (100 * x).toFixed(1) + "%"; }

function chrome(show) {
  document.getElementById("levelbox").classList.toggle("hidden", !show);
  document.getElementById("userbox").classList.toggle("hidden", !show);
}

/* ---------- auth ---------- */

function showAuth() {
  chrome(false); currentSong = currentPlaylist = homePlaylists = null;
  view.innerHTML = `
  <div class="panel">
    <h2>How hard is that Chinese song, really?</h2>
    <p class="muted">Paste the lyrics of any Chinese song and see its HSK difficulty:
      coverage at your level, which words and grammar make it hard, and whether it's
      worth studying. Free, no account needed. Traditional characters are fine.</p>
    <p class="muted">Lyrics only — no artist or title needed. Nothing is saved;
      sign in below if you want to keep the song.</p>
    <form class="stack" id="tryForm">
      <textarea id="tryText" placeholder="Paste Chinese lyrics here — just the words, no song details…"></textarea>
      <div class="error hidden" id="tryErr"></div>
      <button type="submit">Analyze</button>
    </form>
  </div>
  <div class="panel" style="max-width:420px">
    <h2>Sign in</h2>
    <p class="muted">An account adds saving: build playlists, import from Spotify,
      Apple Music, YouTube or NetEase, and get a recommended study order across songs.</p>
    <form class="stack" id="authForm">
      <input type="email" id="email" placeholder="Email" required>
      <input type="password" id="password" placeholder="Password (8+ chars)" required>
      <div id="authCaptcha"></div>
      <div class="error hidden" id="authErr"></div>
      <div style="display:flex;gap:.6rem">
        <button type="submit">Log in</button>
        <button type="button" class="ghost" id="regBtn">Create account</button>
      </div>
    </form>
    <p class="muted" style="font-size:.85rem;margin:.8rem 0 0">
      <a id="forgotLink" style="cursor:pointer">Forgot your password?</a> ·
      <a href="/about#privacy">Privacy</a></p>
    <form class="stack hidden" id="resetForm" style="margin-top:.8rem">
      <p class="muted" style="margin:0">Password resets are handled by a person, not a
        robot — send a request and you'll get a new password by email, usually within a day.</p>
      <input type="email" id="resetEmail" placeholder="Your account email" required>
      <input type="text" id="resetNote" placeholder="Anything we should know (optional)">
      <div id="resetCaptcha"></div>
      <div class="error hidden" id="resetErr"></div>
      <span class="muted hidden" id="resetOk"></span>
      <button type="submit">Request a password reset</button>
    </form>
  </div>`;
  const authCap = mountCaptcha(document.getElementById("authCaptcha"));
  const submit = async (path) => {
    const kind = path === "/api/register" ? "register" : "login";
    // Only registration is captcha-gated; logging in is guarded by the rate
    // limiter and the lockout ladder.
    const body = {email: email.value, password: password.value,
                  captcha: kind === "register" ? authCap.token() : ""};
    try { await api("POST", path, body); tlog("auth", {k: kind, ok: 1}); boot(); }
    catch (e) {
      tlog("auth", {k: kind, ok: 0, m: e.message});
      authCap.reset();                 // a Turnstile token is single-use
      authErr.textContent = e.message; authErr.classList.remove("hidden");
    }
  };
  document.getElementById("authForm").addEventListener("submit", e => { e.preventDefault(); submit("/api/login"); });
  document.getElementById("regBtn").addEventListener("click", () => submit("/api/register"));

  const resetForm = document.getElementById("resetForm");
  const resetCap = mountCaptcha(document.getElementById("resetCaptcha"));
  document.getElementById("forgotLink").addEventListener("click", () => {
    resetForm.classList.toggle("hidden");
    if (!resetForm.classList.contains("hidden")) document.getElementById("resetEmail").focus();
  });
  resetForm.addEventListener("submit", async e => {
    e.preventDefault();
    const err = document.getElementById("resetErr"), ok = document.getElementById("resetOk");
    err.classList.add("hidden"); ok.classList.add("hidden");
    try {
      const r = await api("POST", "/api/password-reset-request", {
        email: document.getElementById("resetEmail").value,
        note: document.getElementById("resetNote").value,
        captcha: resetCap.token()});
      tlog("reset", {ok: 1});
      ok.textContent = r.message; ok.classList.remove("hidden");
      resetForm.querySelectorAll("input").forEach(i => i.value = "");
    } catch (ex) {
      tlog("reset", {ok: 0, m: ex.message});
      resetCap.reset();
      err.textContent = ex.message; err.classList.remove("hidden");
    }
  });
  document.getElementById("tryForm").addEventListener("submit", async e => {
    e.preventDefault();
    const err = document.getElementById("tryErr");
    err.classList.add("hidden");
    const text = document.getElementById("tryText").value;
    const t0 = performance.now();
    try {
      const r = await api("POST", "/api/analyze", {text});
      tlog("anon", {ok: 1, chars: text.length, ms: Math.round(performance.now() - t0)});
      showAnonymousResult(r.analysis);
    } catch (ex) {
      tlog("anon", {ok: 0, chars: text.length, m: ex.message});
      err.textContent = ex.message; err.classList.remove("hidden");
    }
  });
}

function showAnonymousResult(analysis) {
  currentSong = {analysis}; currentPlaylist = homePlaylists = null;
  document.getElementById("levelbox").classList.remove("hidden");
  view.innerHTML = "";
  view.appendChild(el(`<div class="panel">
    <div class="topline"><h2>Your song</h2><a id="tryAgain">← Analyze another</a></div>
    <p class="muted">Set <b>My level</b> above to see coverage from your point of view.
      Nothing was saved — <a id="anonSignup">create a free account</a> to keep songs and build playlists.</p>
    <div id="songBody"></div></div>`));
  document.getElementById("tryAgain").addEventListener("click", showAuth);
  document.getElementById("anonSignup").addEventListener("click", showAuth);
  renderSongDynamic();
}

/* ---------- home ---------- */

async function showHome() {
  chrome(true); currentSong = currentPlaylist = null;
  const [playlists, songs] = await Promise.all([api("GET", "/api/playlists"), api("GET", "/api/songs")]);
  homePlaylists = playlists;
  const loose = songs.filter(s => !s.playlist_id);
  view.innerHTML = "";
  const p = el(`<div class="panel">
    <div class="topline"><h2>Playlists</h2>
      <button class="small" id="newPlBtn">New empty playlist</button>
      <button class="small ghost" id="spotifyBtn">Import playlist</button></div>
    <div id="plList"></div>
    <form class="stack hidden" id="plForm" style="margin-top:.8rem">
      <input type="text" id="plName" placeholder="Playlist name" required>
      <div class="error hidden" id="plErr"></div>
      <button type="submit">Create</button>
    </form>
    <form class="stack hidden" id="spForm" style="margin-top:.8rem">
      <input type="text" id="plUrl" placeholder="Playlist URL — Spotify, Apple Music, YouTube or NetEase (网易云)" required>
      <p class="muted" style="margin:.2rem 0">Imports the track names of a public playlist
        (Spotify and YouTube show at most the first 100). Lyrics are pasted per song afterwards.</p>
      <div class="error hidden" id="spErr"></div>
      <button type="submit">Import</button>
    </form></div>`);
  view.appendChild(p);
  renderHomePlaylistTable();
  const plForm = p.querySelector("#plForm"), spForm = p.querySelector("#spForm");
  p.querySelector("#newPlBtn").addEventListener("click", () => { plForm.classList.toggle("hidden"); spForm.classList.add("hidden"); });
  p.querySelector("#spotifyBtn").addEventListener("click", () => { spForm.classList.toggle("hidden"); plForm.classList.add("hidden"); p.querySelector("#plUrl").focus(); });
  plForm.addEventListener("submit", async e => {
    e.preventDefault();
    const err = p.querySelector("#plErr");
    err.classList.add("hidden");
    try {
      const r = await api("POST", "/api/playlists", {name: p.querySelector("#plName").value});
      nav("playlist", r.id);
    } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
  });
  spForm.addEventListener("submit", async e => {
    e.preventDefault();
    const err = p.querySelector("#spErr");
    err.classList.add("hidden");
    try {
      const u = p.querySelector("#plUrl").value;
      const r = await api("POST", "/api/playlists", {spotify_url: u});
      tlog("import", {src: platformName(u), n: r.imported, cap: r.capped ? 1 : 0});
      importNotice = r.capped
        ? `Imported the first ${r.imported} tracks — ${platformName(u)}'s public page caps what it shows.`
        : `Imported ${r.imported} tracks.`;
      nav("playlist", r.id);
    } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
  });

  const kw = el(`<div class="panel">
    <div class="topline"><h2>My known words</h2><span class="muted" id="kwCount">…</span>
      <button class="small ghost" id="kwEditBtn">Edit list</button></div>
    <p class="muted" style="margin:.2rem 0">Words you already know beyond your HSK level —
      they count as known in every song's coverage, colors and study ranking.
      Paste them below (any separator) or upload a text file. Traditional characters are fine.</p>
    <form class="stack hidden" id="kwForm">
      <textarea id="kwText" placeholder="爱 喜欢 蝴蝶 …"></textarea>
      <div style="display:flex;gap:.6rem;align-items:center">
        <button type="submit">Save list</button>
        <label class="ghost" style="cursor:pointer;color:var(--accent)">Upload file
          <input type="file" id="kwFile" accept=".txt,.csv,text/plain" style="display:none"></label>
        <span class="muted" id="kwMsg"></span>
      </div>
      <div class="error hidden" id="kwErr"></div>
    </form></div>`);
  view.appendChild(kw);
  api("GET", "/api/known").then(k => {
    kw.querySelector("#kwCount").textContent = `${k.count} words`;
    kw.querySelector("#kwText").value = k.words.join(" ");
  });
  kw.querySelector("#kwEditBtn").addEventListener("click", () => kw.querySelector("#kwForm").classList.toggle("hidden"));
  kw.querySelector("#kwFile").addEventListener("change", e => {
    const f = e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => { kw.querySelector("#kwText").value = reader.result; kw.querySelector("#kwMsg").textContent = `${f.name} loaded — hit Save list`; };
    reader.readAsText(f);
  });
  kw.querySelector("#kwForm").addEventListener("submit", async e => {
    e.preventDefault();
    const err = kw.querySelector("#kwErr");
    err.classList.add("hidden");
    try {
      const r = await api("PUT", "/api/known", {text: kw.querySelector("#kwText").value});
      kw.querySelector("#kwCount").textContent = `${r.count} words`;
      kw.querySelector("#kwMsg").textContent = "Saved.";
      knownCount = r.count; syncLevelControls();
    } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
  });

  const s = el(`<div class="panel">
    <div class="topline"><h2>Single songs</h2><button class="small" id="newSongBtn">Add song</button></div>
    <div id="songList"></div>
    <form class="stack hidden" id="songForm" style="margin-top:.8rem">
      <input type="text" id="songArtist" placeholder="Artist">
      <input type="text" id="songTitle" placeholder="Title" required>
      <textarea id="songLyrics" placeholder="Paste the lyrics here (optional — traditional OK, converted automatically)"></textarea>
      <div class="error hidden" id="songErr"></div>
      <button type="submit">Add</button>
    </form></div>`);
  view.appendChild(s);
  const sl = s.querySelector("#songList");
  if (!loose.length) sl.innerHTML = `<div class="muted">No standalone songs.</div>`;
  else for (const song of loose) {
    const row = el(`<div class="rowactions" style="padding:.3rem 0">
      <span>${artistPrefix(song)}<a class="songlink">${esc(song.title)}</a></span>
      <span class="muted">${song.analyzed ? "analyzed" : "no lyrics yet"}</span></div>`);
    row.querySelector("a.songlink").addEventListener("click", () => nav("song", song.id));
    sl.appendChild(row);
  }
  s.querySelector("#newSongBtn").addEventListener("click", () => s.querySelector("#songForm").classList.toggle("hidden"));

  // Arriving from a public song page (/song/<artist>/<title> -> "Paste the
  // lyrics"): open the form ready to go, so the visitor lands on the one field
  // they came here to fill. The params survive a login round-trip because we
  // never rewrite the URL, only clear it once the form is populated.
  const q = new URLSearchParams(location.search);
  if (q.get("artist") || q.get("title")) {
    const form = s.querySelector("#songForm");
    form.classList.remove("hidden");
    s.querySelector("#songArtist").value = q.get("artist") || "";
    s.querySelector("#songTitle").value = q.get("title") || "";
    s.querySelector("#songLyrics").focus();
    form.scrollIntoView({block: "center"});
    history.replaceState({}, "", location.pathname);
  }
  // The song row is created first, then the lyrics are attached. If the lyrics
  // are rejected (no Chinese in them) we keep the id and reuse it on the next
  // submit, so fixing the text doesn't leave a trail of duplicate songs behind.
  let pendingSongId = null;
  s.querySelector("#songForm").addEventListener("submit", async e => {
    e.preventDefault();
    const err = s.querySelector("#songErr");
    err.classList.add("hidden");
    const lyrics = s.querySelector("#songLyrics").value.trim();
    try {
      if (pendingSongId === null) {
        const r = await api("POST", "/api/songs", {
          artist: s.querySelector("#songArtist").value,
          title: s.querySelector("#songTitle").value});
        pendingSongId = r.id;
      }
      if (lyrics) {
        await api("PUT", `/api/songs/${pendingSongId}/lyrics`, {text: lyrics});
        tlog("paste", {ok: 1, chars: lyrics.length, single: 1});
      }
      nav("song", pendingSongId);
    } catch (ex) {
      if (lyrics) tlog("paste", {ok: 0, chars: lyrics.length, single: 1, m: ex.message});
      err.textContent = ex.message; err.classList.remove("hidden");
    }
  });

  const settings = el(`<div class="panel">
    <h2>Settings</h2>
    <label style="display:flex;gap:.5rem;align-items:center;font-size:.92rem;cursor:pointer">
      <input type="checkbox" id="setLyrSearch">
      Open a web search for the lyrics in a new tab when I click a song that has none
    </label>
    <div class="topline" style="margin-top:1rem"><h2 style="font-size:1rem">Change password</h2>
      <button class="small ghost" id="pwBtn">Change</button></div>
    <form class="stack hidden" id="pwForm">
      <input type="password" id="pwCur" placeholder="Current password" required autocomplete="current-password">
      <input type="password" id="pwNew" placeholder="New password (8+ chars)" required autocomplete="new-password">
      <div class="error hidden" id="pwErr"></div>
      <span class="muted hidden" id="pwOk">Password changed. Other devices have been signed out.</span>
      <button type="submit">Save new password</button>
    </form></div>`);
  view.appendChild(settings);
  const cb = settings.querySelector("#setLyrSearch");
  cb.checked = localStorage.getItem("lyricsSearch") === "yes";
  cb.addEventListener("change", () => localStorage.setItem("lyricsSearch", cb.checked ? "yes" : "no"));

  const pwForm = settings.querySelector("#pwForm");
  settings.querySelector("#pwBtn").addEventListener("click", () => pwForm.classList.toggle("hidden"));
  pwForm.addEventListener("submit", async e => {
    e.preventDefault();
    const err = settings.querySelector("#pwErr"), ok = settings.querySelector("#pwOk");
    err.classList.add("hidden"); ok.classList.add("hidden");
    try {
      await api("POST", "/api/change-password", {
        current_password: settings.querySelector("#pwCur").value,
        new_password: settings.querySelector("#pwNew").value});
      pwForm.reset(); ok.classList.remove("hidden");
    } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
  });
}

/* Turnstile is mounted only where a bot costs us something: account creation
   and password-reset requests. Never on the anonymous analyze box, which is
   the funnel. Returns {token, reset}; both are no-ops when unconfigured. */
function mountCaptcha(container) {
  if (!window.TURNSTILE_SITE_KEY) return {token: () => "", reset: () => {}};
  const div = document.createElement("div");
  div.className = "cf-turnstile";
  div.style.margin = ".2rem 0";
  // Forms here are rendered by JS after the async api.js has loaded, so
  // implicit auto-rendering would miss them; render explicitly. The data-*
  // attributes are still set so the markup matches the canonical embed.
  div.setAttribute("data-sitekey", window.TURNSTILE_SITE_KEY);
  div.setAttribute("data-action", window.TURNSTILE_ACTION || "turnstile-spin-v2");
  container.appendChild(div);
  let id = null;
  const render = () => {
    id = window.turnstile.render(div, {
      sitekey: window.TURNSTILE_SITE_KEY,
      action: window.TURNSTILE_ACTION || "turnstile-spin-v2",
    });
  };
  if (window.turnstile && window.turnstile.render) render();
  else {
    // the script is async; poll briefly rather than blocking form rendering
    const t = setInterval(() => {
      if (window.turnstile && window.turnstile.render) { clearInterval(t); render(); }
    }, 200);
    setTimeout(() => clearInterval(t), 15000);
  }
  return {
    token: () => (id !== null && window.turnstile) ? window.turnstile.getResponse(id) : "",
    reset: () => { if (id !== null && window.turnstile) window.turnstile.reset(id); },
  };
}

const TIP_LV = "Learning value (0–100) scores how efficient a song is to study at your level: it rewards unknown words that repeat often, few distinct unknown words, and comfortable (~90%) coverage.";
const TIP_RICH = "Vocabulary richness = unique words ÷ total words (0–1). Higher means more varied vocabulary; lower means more repetition.";

function renderHomePlaylistTable() {
  const list = document.getElementById("plList");
  if (!list) return;
  list.innerHTML = "";
  if (!homePlaylists.length) { list.innerHTML = `<div class="muted">No playlists yet.</div>`; return; }
  const tbl = el(`<table><thead><tr><th>Name</th><th>Songs</th><th>Analyzed</th>
    <th>Avg coverage @ ${levelTag()}</th>
    <th title="${TIP_LV}">Avg learning value</th>
    <th title="${TIP_RICH}">Avg richness</th><th></th></tr></thead><tbody></tbody></table>`);
  for (const pl of homePlaylists) {
    const a = pl.avg ? pl.avg.per_level[effLevel()] : null;
    const tr = el(`<tr class="clickable"><td>${esc(pl.name)}</td><td>${pl.songs}</td><td>${pl.analyzed}</td>
      <td>${a ? pct(a.coverage) : "—"}</td>
      <td>${a ? a.learning_value.toFixed(0) : "—"}</td>
      <td>${pl.avg ? pl.avg.richness.toFixed(2) : "—"}</td>
      <td><button class="small ghost" data-del>Delete</button></td></tr>`);
    tr.addEventListener("click", e => {
      if (e.target.hasAttribute("data-del")) { delPlaylist(pl); return; }
      nav("playlist", pl.id);
    });
    tbl.querySelector("tbody").appendChild(tr);
  }
  list.appendChild(tbl);
}

async function delPlaylist(pl) {
  if (!confirm(`Delete playlist "${pl.name}" and its ${pl.songs} songs?`)) return;
  await api("DELETE", `/api/playlists/${pl.id}`);
  showHome();
}

/* ---------- playlist ---------- */

let plSort = {key: "lv", dir: -1};
let importNotice = null; // one-shot message after a Spotify import

async function showPlaylist(pid) {
  chrome(true); currentSong = homePlaylists = null;
  currentPlaylist = await api("GET", `/api/playlists/${pid}`);
  view.innerHTML = "";
  // Exactly 100 songs = the import cap (Spotify/YouTube), still worth
  // explaining; anything else just notes the list is a snapshot.
  const src = currentPlaylist.playlist.source_url;
  const platform = platformName(src);
  const capNote = !src ? ""
    : currentPlaylist.songs.length === 100 && ["Spotify", "YouTube"].includes(platform)
      ? `<p class="muted" style="margin:.2rem 0 .6rem">Imported from ${platform} — limited to the
         first 100 tracks. Missing songs can be added with “Add songs (list)”.</p>`
      : `<p class="muted" style="margin:.2rem 0 .6rem">Imported from ${platform} (not live-updated).</p>`;
  view.appendChild(el(`<div class="panel">
    <a id="backHome" class="backlink">← All playlists</a>
    <div class="topline"><h2>${esc(currentPlaylist.playlist.name)}</h2>
      <button class="small ghost" id="importBtn">Add songs (list)</button>
      ${currentPlaylist.songs.some(s => !s.analyzed)
        ? `<button class="small ghost" id="autoAllBtn" title="Fetches each song's lyrics, analyzes them, and stores only the statistics — never the text">Auto-analyze all</button>` : ""}
      <span class="muted" id="autoAllStatus" style="font-size:.82rem"></span></div>
    ${capNote}
    <form class="stack hidden" id="importForm" style="margin-top:.6rem">
      <textarea id="importText" placeholder="One per line: Artist - Title"></textarea>
      <button type="submit">Add songs</button></form>
    <div id="plAgg"></div>
    <h3 style="display:flex;align-items:baseline"><span>Songs</span>
      <span style="margin-left:auto;font-size:.72rem;text-transform:none;letter-spacing:0">★ recommended study order</span></h3>
    <div id="plSongs"></div></div>`));
  document.getElementById("backHome").addEventListener("click", () => nav("home"));
  const autoAll = document.getElementById("autoAllBtn");
  if (autoAll) autoAll.addEventListener("click", async () => {
    const todo = currentPlaylist.songs.filter(s => !s.analyzed);
    autoAll.disabled = true;
    const status = document.getElementById("autoAllStatus");
    let ok = 0, fail = 0;
    for (const [i, s] of todo.entries()) {
      status.textContent = `Auto-analyzing ${i + 1}/${todo.length}: ${s.title}…`;
      try { await api("POST", `/api/songs/${s.id}/autofetch`); ok++; }
      catch (ex) {
        fail++;
        if (/Too many requests/.test(ex.message)) {          // server politeness cap
          status.textContent = "Rate limit reached — pausing a minute…";
          await new Promise(r => setTimeout(r, 65000));
          try { await api("POST", `/api/songs/${s.id}/autofetch`); ok++; fail--; }
          catch (e2) { /* keep as failed */ }
        }
      }
    }
    tlog("autoall", {n: todo.length, ok, fail});
    importNotice = `Auto-analyze done: ${ok} songs analyzed` +
      (fail ? `, ${fail} had no confident match (paste those manually).` : ".");
    showPlaylist(pid);
  });
  document.getElementById("importBtn").addEventListener("click", () => document.getElementById("importForm").classList.toggle("hidden"));
  document.getElementById("importForm").addEventListener("submit", async e => {
    e.preventDefault();
    await api("POST", "/api/songs/import", {text: document.getElementById("importText").value, playlist_id: pid});
    showPlaylist(pid);
  });
  renderPlaylistDynamic();
}

function songLevelStats(s) { return s.stats ? s.stats.per_level[effLevel()] : null; }

function lyricsSearchUrl(s) {
  return "https://www.google.com/search?q=" + encodeURIComponent(`${s.artist} ${s.title} lyrics`.trim());
}

// On the first click of a lyrics-less song, offer to also open a web search
// for its lyrics; remember the answer and keep doing (or not doing) it.
function maybeOpenLyricsSearch(s) {
  let pref = localStorage.getItem("lyricsSearch");
  if (pref === null) {
    pref = confirm(`Also open a web search for “${s.artist} ${s.title} lyrics” in a new tab?\n\n` +
      "(Your choice is remembered for future songs without lyrics.)") ? "yes" : "no";
    localStorage.setItem("lyricsSearch", pref);
  }
  if (pref === "yes") window.open(lyricsSearchUrl(s), "_blank", "noopener");
}

function studyOrder(songs) {
  // Ranking: learning value desc, coverage desc, fewest unique unknown, highest reps
  const ranked = songs.filter(s => s.stats).slice();
  ranked.sort((a, b) => {
    const pa = songLevelStats(a), pb = songLevelStats(b);
    return (pb.learning_value - pa.learning_value) || (pb.coverage - pa.coverage)
      || (pa.unique_unknown - pb.unique_unknown) || (pb.avg_reps_unknown - pa.avg_reps_unknown);
  });
  const rank = new Map();
  ranked.forEach((s, i) => rank.set(s.id, i + 1));
  return rank;
}

function renderPlaylistDynamic() {
  const songs = currentPlaylist.songs;
  const analyzed = songs.filter(s => s.stats);
  const agg = document.getElementById("plAgg");
  let notice = "";
  if (importNotice) { notice = `<div class="muted" style="margin-bottom:.5rem">✓ ${esc(importNotice)}</div>`; importNotice = null; }
  if (analyzed.length < songs.length) {
    // Full how-to (highlighted) only until this user has pasted lyrics once;
    // after that, a quiet progress line is enough.
    if (!currentPlaylist.user_analyzed_count) {
      notice += `<div class="panel" style="background:var(--accent-soft);border-color:var(--accent)">
        <b>${analyzed.length}/${songs.length} songs analyzed.</b> Songs import as titles only —
        streaming platforms don't provide lyrics. To analyze one: <b>click the song</b>, paste its lyrics
        into the box, and hit <b>Analyze</b>. Each analyzed song then joins the coverage stats,
        sorting, and study-order ranking below.</div>`;
    } else {
      notice += `<p class="muted" style="margin:.2rem 0 .6rem">${analyzed.length}/${songs.length}
        analyzed — click an unanalyzed song to paste its lyrics.</p>`;
    }
  }
  if (!analyzed.length) {
    agg.innerHTML = notice;
  } else {
    agg.innerHTML = notice;
    const avg = f => analyzed.reduce((t, s) => t + f(s), 0) / analyzed.length;
    const avgCov = avg(s => songLevelStats(s).coverage);
    const avgLV = avg(s => songLevelStats(s).learning_value);
    const avgRich = avg(s => s.stats.richness);
    const dist = {};
    for (let l = 1; l <= 9; l++) dist[l] = analyzed.reduce((t, s) => t + (s.stats.counts_by_level[l] || 0), 0);
    agg.appendChild(el(`<div class="cards">
      <div class="card"><div class="num">${pct(avgCov)}</div><div class="lbl">avg coverage @ ${levelTag()}</div></div>
      <div class="card" title="Learning value (0–100) scores how efficient a song is to study at your level: it rewards unknown words that repeat often, few distinct unknown words, and comfortable (~90%) coverage — high value means a few new words with lots of practice.">
        <div class="num">${avgLV.toFixed(0)}</div><div class="lbl">avg learning value</div></div>
      <div class="card" title="Vocabulary richness = unique words ÷ total words in the song (0–1). Higher means more varied vocabulary; lower means the song repeats the same words a lot.">
        <div class="num">${avgRich.toFixed(2)}</div><div class="lbl">avg vocabulary richness</div></div>
      <div class="card"><div class="num">${analyzed.length}/${songs.length}</div><div class="lbl">songs analyzed</div></div></div>`));
    agg.appendChild(distBar(dist));
  }
  const rank = studyOrder(songs);
  const origIndex = new Map(songs.map((s, i) => [s.id, i + 1]));
  const cols = [
    ["rank", "★"], ["num", "#"], ["title", "Song"], ["cov", "Coverage"], ["lv", "Learning value"],
    ["unk", "Unique unknown"], ["reps", "Reps/unknown"], ["hsk3", `${LEVEL_NAMES[Math.max(effLevel(), 1)]} words`], ["beyond", "Beyond-HSK"],
  ];
  const val = (s, key) => {
    const p = songLevelStats(s);
    switch (key) {
      case "num": return origIndex.get(s.id);
      case "rank": return rank.get(s.id) || 999;
      case "title": return (s.artist + s.title).toLowerCase();
      case "cov": return p ? p.coverage : -1;
      case "lv": return p ? p.learning_value : -1;
      case "unk": return p ? p.unique_unknown : 1e9;
      case "reps": return p ? p.avg_reps_unknown : -1;
      case "hsk3": return s.stats ? (s.stats.unique_by_level[Math.max(effLevel(), 1)] || 0) : -1;
      case "beyond": return s.stats ? (s.stats.unique_by_level[8] || 0) : 1e9;
    }
  };
  const sorted = songs.slice().sort((a, b) => {
    const va = val(a, plSort.key), vb = val(b, plSort.key);
    return (va < vb ? -1 : va > vb ? 1 : 0) * plSort.dir;
  });
  const tbl = el(`<table><thead><tr>${cols.map(([k, lbl]) =>
    `<th data-k="${k}">${lbl}${plSort.key === k ? (plSort.dir > 0 ? " ↑" : " ↓") : ""}</th>`).join("")}</tr></thead><tbody></tbody></table>`);
  tbl.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (plSort.key === k) plSort.dir *= -1; else plSort = {key: k, dir: ["num", "title", "rank", "unk", "beyond"].includes(k) ? 1 : -1};
    renderPlaylistDynamic();
  }));
  for (const s of sorted) {
    const p = songLevelStats(s);
    const r = rank.get(s.id);
    const tr = el(`<tr class="clickable">
      <td>${r ? `<span class="rank">${r === 1 ? "★ " : ""}${r}</span>` : ""}</td>
      <td class="muted">${origIndex.get(s.id)}</td>
      <td>${artistPrefix(s)}${songTitleHtml(s)}${s.stats ? "" : ' <span class="muted">(click to paste lyrics)</span>'}</td>
      <td>${p ? pct(p.coverage) : "—"}</td><td>${p ? p.learning_value.toFixed(0) : "—"}</td>
      <td>${p ? p.unique_unknown : "—"}</td><td>${p ? p.avg_reps_unknown.toFixed(2) : "—"}</td>
      <td>${s.stats ? s.stats.unique_by_level[Math.max(effLevel(), 1)] || 0 : "—"}</td>
      <td>${s.stats ? s.stats.unique_by_level[8] || 0 : "—"}</td></tr>`);
    tr.addEventListener("click", () => {
      if (!s.stats) maybeOpenLyricsSearch(s);
      nav("song", s.id);
    });
    tbl.querySelector("tbody").appendChild(tr);
  }
  const holder = document.getElementById("plSongs");
  holder.innerHTML = ""; holder.appendChild(tbl);
}

/* ---------- song ---------- */

async function showSong(sid) {
  chrome(true); currentPlaylist = homePlaylists = null;
  const song = await api("GET", `/api/songs/${sid}`);
  currentSong = song;
  view.innerHTML = "";
  view.appendChild(el(`<div class="panel">
    <a id="backLink" class="backlink">← Back</a>
    <div class="topline"><h2>${artistPrefix(song)}${esc(song.title)}</h2>
      <button class="small ghost" id="lyricsBtn">${song.has_lyrics ? "Replace lyrics" : "Paste lyrics"}</button>
      ${song.analysis ? "" : `<button class="small ghost" id="autoBtn" title="Fetches lyrics, analyzes them, and stores only the statistics — never the text">Auto-analyze</button>`}
      <button class="small ghost" id="delSongBtn">Delete song</button></div>
    <form class="stack ${song.analysis ? "hidden" : ""}" id="lyricsForm" style="margin-top:.6rem">
      <p class="muted" style="margin:0">
        <a href="${lyricsSearchUrl(song)}" target="_blank" rel="noopener">Search the web for these lyrics ↗</a>
        — then paste them below.</p>
      <textarea id="lyricsText" placeholder="Paste the Chinese lyrics here (traditional OK — converted automatically)"></textarea>
      <div class="error hidden" id="lyricsErr"></div>
      <button type="submit">Analyze</button></form>
    <div id="songBody"></div></div>`));
  document.getElementById("backLink").addEventListener("click", () =>
    song.playlist_id ? nav("playlist", song.playlist_id) : nav("home"));
  document.getElementById("lyricsBtn").addEventListener("click", () =>
    document.getElementById("lyricsForm").classList.toggle("hidden"));
  const autoBtn = document.getElementById("autoBtn");
  if (autoBtn) autoBtn.addEventListener("click", async () => {
    autoBtn.disabled = true;
    autoBtn.textContent = "Fetching…";
    const t0 = performance.now();
    try {
      await api("POST", `/api/songs/${sid}/autofetch`);
      tlog("auto", {ok: 1, ms: Math.round(performance.now() - t0)});
      showSong(sid);
    }
    catch (ex) {
      tlog("auto", {ok: 0, m: ex.message});
      autoBtn.disabled = false;
      autoBtn.textContent = "Auto-analyze";
      const err = document.getElementById("lyricsErr");
      err.textContent = ex.message;
      err.classList.remove("hidden");
      document.getElementById("lyricsForm").classList.remove("hidden");
    }
  });
  document.getElementById("delSongBtn").addEventListener("click", async () => {
    if (!confirm("Delete this song and its analysis?")) return;
    await api("DELETE", `/api/songs/${sid}`);
    song.playlist_id ? nav("playlist", song.playlist_id) : nav("home");
  });
  document.getElementById("lyricsForm").addEventListener("submit", async e => {
    e.preventDefault();
    const err = document.getElementById("lyricsErr");
    err.classList.add("hidden");
    const text = document.getElementById("lyricsText").value;
    try {
      await api("PUT", `/api/songs/${sid}/lyrics`, {text});
      tlog("paste", {ok: 1, chars: text.length});
      showSong(sid);
    } catch (ex) {
      tlog("paste", {ok: 0, chars: text.length, m: ex.message});
      err.textContent = ex.message; err.classList.remove("hidden");
    }
  });
  renderSongDynamic();
}

function distBar(counts) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  const wrap = el(`<div><div class="distbar"></div><div class="legend"></div></div>`);
  const bar = wrap.querySelector(".distbar"), legend = wrap.querySelector(".legend");
  for (let l = 1; l <= 9; l++) {
    const c = counts[l] || 0;
    if (!c) continue;
    const seg = el(`<div title="${LEVEL_NAMES[l]}: ${c} (${pct(c / total)})"></div>`);
    seg.style.width = (100 * c / total) + "%";
    seg.style.background = DIST_COLORS[l];
    bar.appendChild(seg);
    legend.appendChild(el(`<span><i style="background:${DIST_COLORS[l]}"></i>${LEVEL_NAMES[l]} ${pct(c / total)}</span>`));
  }
  return wrap;
}

function tokClass(lvl, personallyKnown) {
  if (lvl === 0) return "tok filler";
  if (lvl === 9) return "tok latin";
  if (personallyKnown) return "tok known";
  if (lvl === 8) return "tok beyond";
  if (lvl <= effLevel()) return "tok known";
  if (lvl === effLevel() + 1) return "tok next";
  return "tok hard";
}

function renderSongDynamic() {
  const body = document.getElementById("songBody");
  const a = currentSong.analysis;
  if (!a) { body.innerHTML = `<div class="muted" style="margin-top:.6rem">Paste lyrics above to analyze this song.</div>`; return; }
  const st = a.stats, p = st.per_level[effLevel()];
  body.innerHTML = "";
  body.appendChild(el(`<div class="cards" style="margin-top:.8rem">
    <div class="card"><div class="num">${pct(p.coverage)}</div><div class="lbl">coverage @ ${levelTag()}</div></div>
    <div class="card" title="Learning value (0–100) scores how efficient this song is to study at your level: it rewards unknown words that repeat often, few distinct unknown words, and comfortable (~90%) coverage."><div class="num">${p.learning_value.toFixed(0)}</div><div class="lbl">learning value</div></div>
    <div class="card"><div class="num">${p.unique_unknown}</div><div class="lbl">unique unknown words</div></div>
    <div class="card"><div class="num">${p.repeated_unknown}</div><div class="lbl">repeated unknown words</div></div>
    <div class="card"><div class="num">${p.avg_reps_unknown.toFixed(2)}</div><div class="lbl">avg reps per unknown</div></div>
    <div class="card"><div class="num">${st.unique_vocab}</div><div class="lbl">unique vocabulary</div></div>
    <div class="card"><div class="num">${st.chinese_tokens}</div><div class="lbl">Chinese tokens</div></div>
    <div class="card" title="Vocabulary richness = unique words ÷ total words in the song (0–1). Higher means more varied vocabulary; lower means the song repeats the same words a lot."><div class="num">${st.richness.toFixed(2)}</div><div class="lbl">vocabulary richness</div></div></div>`));
  body.appendChild(el(`<h3>HSK distribution</h3>`));
  body.appendChild(distBar(st.counts_by_level));

  if (a.lines) {
    body.appendChild(el(`<h3>Lyrics</h3>`));
    body.appendChild(el(`<div class="legend"><span><i style="background:var(--known-bg);border:1px solid var(--known)"></i>Known${knownCount ? " (level or your list)" : ""}</span>
      <span><i style="background:var(--next-bg);border:1px solid var(--next)"></i>One level up</span>
      <span><i style="background:var(--hard-bg);border:1px solid var(--hard)"></i>Two+ levels up</span>
      <span><i style="background:var(--beyond-bg);border:1px solid var(--beyond)"></i>Beyond HSK / unknown</span>
      <span>Dotted underline = idiom</span></div>`));
    const lyr = el(`<div class="lyrics"></div>`);
    for (const line of a.lines) {
      const ln = el(`<div></div>`);
      for (const t of line) {
        const v = a.vocab[t.n];
        const span = el(`<span class="${tokClass(t.lvl, v && v.known)}${t.i ? " idiom" : ""}">${esc(t.t)}</span>`);
        span.addEventListener("click", e => showTokenPopover(e, t, a));
        ln.appendChild(span);
        if (/[A-Za-z0-9]$/.test(t.t)) ln.appendChild(document.createTextNode(" "));
      }
      lyr.appendChild(ln);
    }
    body.appendChild(lyr);
  } else {
    body.appendChild(el(`<p class="muted" style="margin:.8rem 0">Difficulty was computed
      from auto-fetched lyrics; the text itself isn't stored. Paste your own copy
      (<b>Paste lyrics</b> above) to see the color-coded lyric view.</p>`));
  }

  if (a.grammar.length) {
    body.appendChild(el(`<h3>Grammar patterns</h3>`));
    const tbl = el(`<table><thead><tr><th>Pattern</th><th>Level</th><th>Count</th><th>Example</th></tr></thead><tbody></tbody></table>`);
    for (const g of a.grammar)
      tbl.querySelector("tbody").appendChild(el(`<tr><td>${esc(g.name)}</td>
        <td><span class="pill l${g.level}">${LEVEL_NAMES[g.level]}</span></td>
        <td>${g.count}</td><td class="muted">${esc((g.examples || [])[0] || "")}</td></tr>`));
    body.appendChild(tbl);
  }
  if (a.idioms.length) {
    body.appendChild(el(`<h3>Idioms (成语)</h3>`));
    const wl = el(`<div class="wordlist"></div>`);
    for (const i of a.idioms)
      wl.appendChild(el(`<span class="pill l${i.lvl}">${esc(i.word)} ×${i.count}</span>`));
    body.appendChild(wl);
  }
  const unknown = Object.entries(a.vocab)
    .filter(([, v]) => v.lvl > effLevel() && v.lvl !== 9 && !v.known)
    .sort((x, y) => (x[1].lvl - y[1].lvl) || (y[1].count - x[1].count));
  // Words that only appear inside words on the personal list (没 when the
  // list has 没有) are probably known — group them apart at any level.
  const probable = unknown.filter(([, v]) => v.p);
  const toLearn = unknown.filter(([, v]) => !v.p);

  const wordTable = (entries, withAdd) => {
    const tbl = el(`<table class="vocabtbl"><thead><tr>
      <th>Word</th><th>Level</th><th>Count</th><th>Meaning</th>${withAdd ? "<th>Known</th>" : ""}</tr></thead><tbody></tbody></table>`);
    const tb = tbl.querySelector("tbody");
    for (const [w, v] of entries) {
      const tr = el(`<tr><td><span class="${tokClass(v.lvl, false)}">${esc(w)}</span></td>
        <td class="muted">${LEVEL_NAMES[v.lvl]}</td><td class="muted">×${v.count}</td>
        <td class="muted gloss">${v.g ? esc(v.g) : "—"}</td>
        ${withAdd ? `<td><button class="addkw" title="I know this word — add it to my known words">+</button></td>` : ""}</tr>`);
      if (withAdd)
        tr.querySelector("button").addEventListener("click", async (e) => {
          e.target.disabled = true;
          try {
            const r = await api("POST", "/api/known/add", {words: [w]});
            knownCount = r.count; syncLevelControls(); showSong(currentSong.id);
          } catch (ex) { e.target.disabled = false; alert(ex.message); }
        });
      tb.appendChild(tr);
    }
    return tbl;
  };

  if (toLearn.length) {
    body.appendChild(el(`<h3>Words to learn</h3>`));
    body.appendChild(el(`<p class="muted" style="margin:.1rem 0 .4rem;font-size:.85rem">
      The ${toLearn.length} word${toLearn.length === 1 ? "" : "s"} in this song above ${levelTag()} — easiest level first,
      most repeated first within a level. Colors match the lyrics highlighting:</p>`));
    body.appendChild(el(`<div class="legend" style="margin-bottom:.5rem">
      <span><i style="background:var(--next-bg);border:1px solid var(--next)"></i>One level up</span>
      <span><i style="background:var(--hard-bg);border:1px solid var(--hard)"></i>Two+ levels up</span>
      <span><i style="background:var(--beyond-bg);border:1px solid var(--beyond)"></i>Beyond HSK</span></div>`));
    body.appendChild(wordTable(toLearn));
  }
  if (probable.length) {
    body.appendChild(el(`<h3>Probably known</h3>`));
    body.appendChild(el(`<p class="muted" style="margin:.1rem 0 .4rem;font-size:.85rem">
      Not in your list on their own, but they appear inside words you know
      (e.g. 没 inside 没有). If you know one by itself, click + and it'll
      count as known everywhere.</p>`));
    body.appendChild(wordTable(probable, !!(currentSong && currentSong.id)));
  }
  if (toLearn.length) {
    const exp = el(`<div style="margin:.6rem 0 0;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
      <span class="muted" style="font-size:.8rem">Export words to learn:</span>
      <button class="small ghost" id="expCsv">CSV</button>
      <button class="small ghost" id="expAnki">Anki</button>
      <span class="muted hidden" id="expNote" style="font-size:.8rem">Exports ship with the
        paid tier — coming soon. <a href="/about">Learn more →</a></span></div>`);
    // Clicks here are the clearest paid-tier demand signal the app has.
    const upsell = (fmt) => {
      exp.querySelector("#expNote").classList.remove("hidden");
      tlog("upsell", {fmt, words: toLearn.length});
    };
    exp.querySelector("#expCsv").addEventListener("click", () => upsell("csv"));
    exp.querySelector("#expAnki").addEventListener("click", () => upsell("anki"));
    body.appendChild(exp);
  }
  if (toLearn.length || probable.length)
    body.appendChild(el(`<p class="muted" style="font-size:.75rem;margin:.4rem 0 0">
      Definitions from <a href="https://cc-cedict.org" target="_blank" rel="noopener">CC-CEDICT</a> (CC BY-SA 4.0).</p>`));
}

function exportWords(entries, fmt) {
  const s = currentSong || {};
  const name = `${s.artist ? s.artist + " - " : ""}${s.title || "song"}`;
  let content, fname, type;
  if (fmt === "csv") {
    const q = (x) => `"${String(x).replace(/"/g, '""')}"`;
    content = "word,level,count,meaning,song\n" + entries.map(([w, v]) =>
      [q(w), q(LEVEL_NAMES[v.lvl]), v.count, q(v.g || ""), q(name)].join(",")).join("\n");
    fname = `${name} - words.csv`; type = "text/csv";
  } else {
    // Anki: tab-separated front/back, importable as Basic notes
    content = entries.map(([w, v]) =>
      `${w}\t${v.g || ""} (${LEVEL_NAMES[v.lvl]} · ${name})`).join("\n");
    fname = `${name} - anki.txt`; type = "text/plain";
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + content], {type: type + ";charset=utf-8"}));
  a.download = fname;
  a.click();
  URL.revokeObjectURL(a.href);
}

function showTokenPopover(e, t, a) {
  const v = a.vocab[t.n];
  popover.innerHTML = `<b>${esc(t.t)}</b>${t.t !== t.n ? ` <span class="muted">→ ${esc(t.n)}</span>` : ""}<br>
    <span class="pill l${t.lvl}">${LEVEL_NAMES[t.lvl]}</span>${t.i ? ' <span class="muted">idiom</span>' : ""}${v && v.known ? ' <span class="muted">✓ in your known words</span>' : ""}<br>
    ${v && v.g ? `<span class="gloss">${esc(v.g)}</span><br>` : ""}
    <span class="muted">${v ? v.count : 1}× in this song</span>`;
  popover.classList.remove("hidden");
  const r = e.target.getBoundingClientRect();
  popover.style.left = Math.max(window.scrollX, Math.min(window.scrollX + r.left, window.scrollX + window.innerWidth - 310)) + "px";
  popover.style.top = (window.scrollY + r.bottom + 6) + "px";
  e.stopPropagation();
}

/* ---------- routing ---------- */

function nav(page, id) {
  location.hash = id !== undefined ? `#${page}/${id}` : `#${page}`;
}

async function route() {
  const [page, id] = location.hash.replace("#", "").split("/");
  tlog("nav", {p: page || "home", lv: learnerLevel});
  try {
    if (page === "song" && id) await showSong(parseInt(id));
    else if (page === "playlist" && id) await showPlaylist(parseInt(id));
    else await showHome();
  } catch (e) { /* 401 already redirected to auth */ }
}

window.addEventListener("hashchange", route);

async function boot() {
  try {
    const user = await api("GET", "/api/me");
    document.getElementById("userEmail").textContent = user.email;
    knownCount = user.known_count || 0;
    tlog("load", {p: "app", in: 1, lv: learnerLevel, known: knownCount,
                  ua: navigator.userAgent.slice(0, 120)});
    syncLevelControls();
    route();
  } catch (e) {
    // 401 → showAuth already ran; this is the anonymous landing page.
    tlog("load", {p: "app", in: 0, lv: learnerLevel,
                  ua: navigator.userAgent.slice(0, 120)});
  }
}

boot();
