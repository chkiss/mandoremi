#!/usr/bin/env python3
"""Mandoremi — telemetry dashboard generator.

Sibling of ~/nukes/nukes_dash.py (Ghosts in the Loop); same shape, same geo
cache format, mandoremi's events. Reads the nginx telemetry log, geolocates
unique visitor IPs (once each, cached), separates real users from bots, and
writes one self-contained HTML page. Stdlib only. Safe to re-run (cron).

Served on the VPN-only monitor vhost at http://10.8.0.1/mandoremi/ — never
publicly, because it carries visitor IPs.

Privacy: the beacons themselves never contain an email, a song title, or a
line of lyrics (see static/telemetry.js), so neither can this page.

Set GEO=0 to skip all external calls.
"""
import glob
import gzip
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape
from urllib.parse import parse_qs, unquote_plus
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
AWARE_MIN = datetime.min.replace(tzinfo=timezone.utc)

HOME = os.path.expanduser("~")
LOG_GLOB = os.environ.get("LOG_GLOB", "/var/log/nginx/mandoremi_telemetry.log*")
OUTDIR = os.environ.get("DASH_DIR", os.path.join(HOME, "mandoremi-monitor"))
OUT = os.environ.get("DASH_OUT", os.path.join(OUTDIR, "index.html"))
# Own cache, deliberately not shared with nukes: two cron jobs writing one file
# would race and lose entries.
CACHE = os.path.join(OUTDIR, "geo_cache.json")
GEO_ON = os.environ.get("GEO", "1") != "0"

LINE = re.compile(
    r'^(\S+) \S+ \S+ \[([^\]]+)\] "\S+ (\S+) [^"]*" (\d+) \S+ "([^"]*)" "([^"]*)"'
)

BOT_MARKERS = (
    "HeadlessChrome", "bot", "crawler", "spider", "Slackbot", "Discordbot",
    "Twitterbot", "facebookexternalhit", "WhatsApp", "TelegramBot", "LinkedInBot",
    "Googlebot", "bingbot", "Applebot", "PetalBot", "python-requests", "curl",
    "Preview", "Embedly", "redditbot",
)

LEVEL_NAMES = {"0": "Pre-HSK1", "1": "HSK1", "2": "HSK2", "3": "HSK3", "4": "HSK4",
               "5": "HSK5", "6": "HSK6", "7": "HSK7-9"}

UNREADABLE = []


def is_bot(ua):
    return any(m.lower() in ua.lower() for m in BOT_MARKERS)


def read_lines():
    for path in sorted(glob.glob(LOG_GLOB)):
        try:
            op = gzip.open if path.endswith(".gz") else open
            with op(path, "rt", errors="replace") as fh:
                yield from fh
        except (OSError, PermissionError) as e:
            UNREADABLE.append(path)
            print(f"warn: cannot read {path}: {e}", file=sys.stderr)


def parse_ts(s):
    try:
        return datetime.strptime(s, "%d/%b/%Y:%H:%M:%S %z").astimezone(NY)
    except ValueError:
        try:
            return datetime.strptime(s.split()[0], "%d/%b/%Y:%H:%M:%S").replace(
                tzinfo=timezone.utc).astimezone(NY)
        except ValueError:
            return None


def ua_device(ua):
    u = ua.lower()
    if "iphone" in u or "ipad" in u or "ios" in u:
        os_name = "iOS"
    elif "android" in u:
        os_name = "Android"
    elif "windows" in u:
        os_name = "Windows"
    elif "mac os x" in u or "macintosh" in u:
        os_name = "macOS"
    elif "linux" in u:
        os_name = "Linux"
    else:
        os_name = "Other"
    if "edg/" in u:
        browser = "Edge"
    elif "crios" in u:
        browser = "Chrome (iOS)"
    elif "firefox" in u or "fxios" in u:
        browser = "Firefox"
    elif "chrome" in u:
        browser = "Chrome"
    elif "safari" in u:
        browser = "Safari"
    else:
        browser = "Other"
    if "ipad" in u or "tablet" in u or ("android" in u and "mobile" not in u):
        form = "Tablet"
    elif "iphone" in u or "mobi" in u:
        form = "Mobile"
    else:
        form = "Desktop"
    return os_name, browser, form


def geo_lookup(ips):
    cache = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE) as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
    missing = [ip for ip in ips if ip not in cache]
    if GEO_ON and missing:
        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            body = json.dumps(
                [{"query": ip, "fields": "status,country,regionName,city,isp,hosting,query"}
                 for ip in batch]).encode()
            try:
                req = urllib.request.Request(
                    "http://ip-api.com/batch", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    for rec in json.load(r):
                        if rec.get("query"):
                            cache[rec["query"]] = rec
            except Exception as e:  # noqa: BLE001 - geo must never break the run
                print(f"warn: geo batch failed: {e}", file=sys.stderr)
                break
            time.sleep(1.5)
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            with open(CACHE, "w") as fh:
                json.dump(cache, fh)
        except OSError as e:
            print(f"warn: cannot write cache: {e}", file=sys.stderr)
    return cache


def mask(ip):
    if ":" in ip:
        return ip.split(":")[0] + ":…"
    parts = ip.split(".")
    return ".".join(parts[:3] + ["x"]) if len(parts) == 4 else ip


def bar(n, total, width=14):
    if total <= 0:
        return ""
    filled = round(width * n / total)
    return "█" * filled + "·" * (width - filled)


def one(qs, key, default="?"):
    return (qs.get(key) or [default])[0]


def main():
    events = []
    for ln in read_lines():
        m = LINE.match(ln)
        if not m:
            continue
        ip, tsr, path, status, referer, ua = m.groups()
        if "ev=" not in path:
            continue
        qs = parse_qs(path.split("?", 1)[1]) if "?" in path else {}
        events.append((parse_ts(tsr), ip, one(qs, "ev", ""), qs,
                       unquote_plus(ua), is_bot(ua)))

    real = [e for e in events if not e[5]]
    loads = [e for e in real if e[2] == "load"]
    anon = [e for e in real if e[2] == "anon"]
    auths = [e for e in real if e[2] == "auth"]
    pastes = [e for e in real if e[2] == "paste"]
    autos = [e for e in real if e[2] == "auto"]
    imports = [e for e in real if e[2] == "import"]
    upsells = [e for e in real if e[2] == "upsell"]
    errs = [e for e in real if e[2] in ("err", "rej")]
    apierrs = [e for e in real if e[2] == "apierr"]

    all_loads = [e for e in events if e[2] == "load"]
    all_load_ips = {e[1] for e in all_loads}
    ip_isbot, ip_ua = {}, {}
    for ts, ip, ev, qs, ua, bot in all_loads:
        ip_isbot[ip] = ip_isbot.get(ip, True) and bot
        if not bot or ip not in ip_ua:
            ip_ua[ip] = ua

    geo = geo_lookup(sorted(all_load_ips))

    def cat(ip):
        if ip_isbot.get(ip, False):
            return "bot"
        if geo.get(ip, {}).get("hosting"):
            return "dc"
        return "person"

    people_ips = {ip for ip in all_load_ips if cat(ip) == "person"}
    dc_ips = {ip for ip in all_load_ips if cat(ip) == "dc"}
    bot_ips = {ip for ip in all_load_ips if cat(ip) == "bot"}

    events_by_ip = defaultdict(list)
    for rec in events:
        events_by_ip[rec[1]].append(rec)
    for ip in events_by_ip:
        events_by_ip[ip].sort(key=lambda e: e[0] or AWARE_MIN)

    by_day = defaultdict(set)
    for ts, ip, *_ in loads:
        if ts and cat(ip) == "person":
            by_day[ts.strftime("%Y-%m-%d")].add(ip)

    def geo_split():
        return {"person": 0, "dc": 0, "bot": 0}
    countries, cities = defaultdict(geo_split), defaultdict(geo_split)
    for ip in all_load_ips:
        g = geo.get(ip, {})
        countries[g.get("country") or "Unknown"][cat(ip)] += 1
        if g.get("city"):
            cities[f"{g['city']}, {g.get('country','')}"][cat(ip)] += 1

    os_c, br_c, form_c = Counter(), Counter(), Counter()
    for e in loads:
        o, b, f = ua_device(e[4])
        os_c[o] += 1
        br_c[b] += 1
        form_c[f] += 1

    # Sessions (sid = one page load) are the funnel unit; bots excluded.
    sess = defaultdict(set)
    for ts, ip, ev, qs, ua, bot in real:
        sid = one(qs, "sid", "")
        if sid:
            sess[sid].add(ev)
    nsess = len(sess) or 1

    signed_in = sum(1 for e in loads if one(e[3], "in", "0") == "1")
    levels = Counter(one(e[3], "lv", "?") for e in loads + [x for x in real if x[2] == "level"])
    platforms = Counter(one(e[3], "src", "?") for e in imports)
    imported_tracks = sum(int(one(e[3], "n", "0") or 0) for e in imports)
    upsell_fmt = Counter(one(e[3], "fmt", "?") for e in upsells)
    auto_ok = sum(1 for e in autos if one(e[3], "ok", "0") == "1")
    paste_ok = sum(1 for e in pastes if one(e[3], "ok", "0") == "1")
    reg = sum(1 for e in auths if one(e[3], "k") == "register" and one(e[3], "ok") == "1")
    logins = sum(1 for e in auths if one(e[3], "k") == "login" and one(e[3], "ok") == "1")
    authfail = sum(1 for e in auths if one(e[3], "ok") == "0")

    visitors = {}
    for ts, ip, ev, qs, ua, bot in real:
        v = visitors.setdefault(ip, {"loads": 0, "last": ts, "ua": ua,
                                     "songs": 0, "acts": Counter()})
        v["acts"][ev] += 1
        if ev == "load":
            v["loads"] += 1
            v["ua"] = ua
        if ev in ("paste", "auto") and one(qs, "ok", "0") == "1":
            v["songs"] += 1
        if ts and (v["last"] is None or ts > v["last"]):
            v["last"] = ts
    visitor_rows = sorted(visitors.items(),
                          key=lambda kv: kv[1]["last"] or AWARE_MIN, reverse=True)

    gen = datetime.now(NY).strftime("%Y-%m-%d %H:%M %Z")
    H = []
    A = H.append
    A("<title>MANDOREMI — telemetry</title>")
    A("""<style>
:root{--a:#7ad0a0;--d:#3f7a5c;--bg:#06100b}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--a);font:14px/1.5 ui-monospace,Menlo,monospace;margin:0 auto;padding:24px;max-width:1100px}
h1{font-size:1.4em;letter-spacing:.1em;border-bottom:1px solid var(--d);padding-bottom:8px}
h2{font-size:1em;letter-spacing:.15em;margin:28px 0 8px;text-transform:uppercase}
.big{display:flex;gap:28px;flex-wrap:wrap;margin:16px 0}
.kpi{border:1px solid var(--d);padding:12px 18px;min-width:120px}
.kpi .n{font-size:2.2em;line-height:1}
.kpi .l{color:var(--d);font-size:.8em;letter-spacing:.1em;text-transform:uppercase}
table{border-collapse:collapse;width:100%;margin:6px 0}
td,th{border:1px solid #17402c;padding:3px 10px;text-align:left;font-size:.9em}
th{color:var(--d);font-weight:normal;letter-spacing:.08em}
.bar{color:var(--d);letter-spacing:-1px}
.dim{color:var(--d)}
.cols{display:flex;gap:24px;flex-wrap:wrap}
.cols>div{flex:1;min-width:280px}
.foot{color:var(--d);font-size:.8em;margin-top:32px;border-top:1px solid var(--d);padding-top:8px}
.nav{font-size:.82em;margin:-4px 0 10px}
.nav a{color:#ffb000;text-decoration:none;border:1px solid #b07c10;padding:2px 8px}
.nav a:hover{background:#1a1206}
tr.vis{cursor:pointer}
tr.vis:hover td{background:#0d2419}
.det td{background:#08170f}
.tl{white-space:pre-wrap;font-size:.82em;color:var(--d);margin:6px 0 0;line-height:1.55}
</style>""")
    A("<h1>MANDOREMI — WHO'S USING IT</h1>")
    A('<p class="nav">sibling dashboard: <a href="/">▸ GHOSTS IN THE LOOP</a></p>')
    A(f'<p class="dim" style="font-size:.82em;margin:-4px 0 0">Last updated '
      f'{datetime.now(NY).strftime("%d %b %Y %H:%M")} ET · all dates &amp; times in New York (ET)</p>')
    if UNREADABLE:
        A('<p style="color:#f66;font-size:.82em;margin:2px 0 4px">⚠ STALE DATA — could not read: '
          + ", ".join(UNREADABLE) +
          ' (after logrotate the fresh logs are group <code>adm</code>)</p>')

    A('<div class="big">')
    for n, lbl in ((len(people_ips), "people"), (len(dc_ips), "datacenter / VPN"),
                   (len(bot_ips), "bots"), (len(loads), "page opens"),
                   (paste_ok + auto_ok, "songs analyzed"), (reg, "accounts created"),
                   (len(upsells), "export clicks")):
        A(f'<div class="kpi"><div class="n">{n}</div><div class="l">{lbl}</div></div>')
    A('</div>')

    A("<h2>Funnel (sessions reaching each step)</h2><table>")
    for lbl, ev in (("opened the site", "load"), ("tried it anonymously", "anon"),
                    ("logged in / signed up", "auth"), ("moved around the app", "nav"),
                    ("pasted lyrics", "paste"), ("auto-analyzed", "auto"),
                    ("imported a playlist", "import"), ("clicked an export", "upsell")):
        n = sum(1 for evs in sess.values() if ev in evs)
        A(f'<tr><td>{lbl}</td><td>{n}</td><td>{100*n/nsess:.0f}%</td>'
          f'<td class="bar">{bar(n, nsess)}</td></tr>')
    A("</table>")
    A(f'<p class="dim" style="font-size:.82em;margin:2px 0 0">{nsess} sessions · '
      f'{signed_in} opens were already signed in · {logins} logins, {reg} registrations, '
      f'{authfail} failed attempts.</p>')

    A("<h2>Opens by day, ET (unique people)</h2><table>")
    mx = max((len(v) for v in by_day.values()), default=1)
    for day in sorted(by_day, reverse=True):
        n = len(by_day[day])
        A(f'<tr><td>{day}</td><td>{n}</td><td class="bar">{bar(n, mx)}</td></tr>')
    if not by_day:
        A('<tr><td class="dim">no human opens yet</td></tr>')
    A("</table>")

    def split_table(title, counter):
        A(f"<div><h2>{title}</h2><table>")
        A('<tr><th>place</th><th>people</th><th>dc/vpn</th><th>bots</th></tr>')
        ranked = sorted(counter.items(),
                        key=lambda kv: (kv[1]["person"], kv[1]["dc"] + kv[1]["bot"]),
                        reverse=True)[:15]
        for place, c in ranked:
            A(f'<tr><td>{escape(place)}</td><td>{c["person"] or ""}</td>'
              f'<td class="dim">{c["dc"] or ""}</td><td class="dim">{c["bot"] or ""}</td></tr>')
        if not ranked:
            A('<tr><td class="dim">no geo data yet</td></tr>')
        A("</table></div>")

    A('<div class="cols">')
    split_table("Top countries", countries)
    split_table("Top cities", cities)
    A("</div>")

    A('<div class="cols">')
    for title, ctr in (("OS", os_c), ("Browser", br_c), ("Form factor", form_c)):
        A(f"<div><h2>{title}</h2><table>")
        for k, n in ctr.most_common():
            A(f'<tr><td>{escape(k)}</td><td>{n}</td><td class="bar">{bar(n, len(loads))}</td></tr>')
        if not ctr:
            A('<tr><td class="dim">—</td></tr>')
        A("</table></div>")
    A("</div>")

    A('<div class="cols">')
    A("<div><h2>HSK level in use</h2><table>")
    tot_lv = sum(levels.values())
    for k, n in sorted(levels.items(), key=lambda kv: kv[0]):
        A(f'<tr><td>{LEVEL_NAMES.get(k, k)}</td><td>{n}</td>'
          f'<td class="bar">{bar(n, tot_lv)}</td></tr>')
    if not levels:
        A('<tr><td class="dim">—</td></tr>')
    A("</table></div>")

    A("<div><h2>Playlist imports</h2><table>")
    for k, n in platforms.most_common():
        A(f'<tr><td>{escape(k)}</td><td>{n}</td></tr>')
    if not imports:
        A('<tr><td class="dim">no imports yet</td></tr>')
    else:
        A(f'<tr><td class="dim">tracks imported</td><td class="dim">{imported_tracks}</td></tr>')
    A("</table></div>")

    A("<div><h2>Export demand (paid signal)</h2><table>")
    for k, n in upsell_fmt.most_common():
        A(f'<tr><td>{escape(k)}</td><td>{n}</td></tr>')
    if not upsells:
        A('<tr><td class="dim">nobody has clicked export yet</td></tr>')
    A("</table></div>")
    A("</div>")

    A("<h2>Analysis outcomes</h2><table>")
    A('<tr><th>path</th><th>ok</th><th>failed</th></tr>')
    A(f'<tr><td>pasted lyrics</td><td>{paste_ok}</td><td>{len(pastes)-paste_ok}</td></tr>')
    A(f'<tr><td>auto-analyze (single)</td><td>{auto_ok}</td><td>{len(autos)-auto_ok}</td></tr>')
    anon_ok = sum(1 for e in anon if one(e[3], "ok", "0") == "1")
    A(f'<tr><td>anonymous try-it</td><td>{anon_ok}</td><td>{len(anon)-anon_ok}</td></tr>')
    A("</table>")

    def describe(ts, ev, qs):
        t = ts.strftime("%m-%d %H:%M:%S") if ts else "—"
        if ev == "load":
            o, b, fm = ua_device(one(qs, "ua", "") or "")
            who = "signed in" if one(qs, "in", "0") == "1" else "anonymous"
            return f"{t}  OPENED ({who}) — {o} · {b} · {fm}"
        if ev == "nav":
            return f"{t}  VIEWED {one(qs, 'p')}"
        if ev == "level":
            return f"{t}  SET LEVEL → {LEVEL_NAMES.get(one(qs,'lv'), one(qs,'lv'))}"
        if ev == "auth":
            ok = "ok" if one(qs, "ok") == "1" else f"FAILED ({one(qs,'m','')[:50]})"
            return f"{t}  {one(qs,'k').upper()} — {ok}"
        if ev == "anon":
            return (f"{t}  ANONYMOUS ANALYZE — {one(qs,'chars')} chars, "
                    f"{'ok' if one(qs,'ok')=='1' else 'failed'} ({one(qs,'ms','?')}ms)")
        if ev == "paste":
            return (f"{t}  PASTED LYRICS — {one(qs,'chars')} chars, "
                    f"{'analyzed' if one(qs,'ok')=='1' else 'FAILED'}")
        if ev == "auto":
            return (f"{t}  AUTO-ANALYZE — "
                    f"{'ok' if one(qs,'ok')=='1' else 'no confident match'} ({one(qs,'ms','?')}ms)")
        if ev == "autoall":
            return f"{t}  AUTO-ANALYZE ALL — {one(qs,'ok')} ok / {one(qs,'fail')} failed of {one(qs,'n')}"
        if ev == "import":
            return f"{t}  IMPORTED PLAYLIST — {one(qs,'src')}, {one(qs,'n')} tracks"
        if ev == "upsell":
            return f"{t}  ★ CLICKED EXPORT ({one(qs,'fmt')}) — {one(qs,'words')} words to learn"
        if ev == "apierr":
            return f"{t}  ⚠ API {one(qs,'st')} on {one(qs,'ep')} — {one(qs,'m','')[:70]}"
        if ev in ("err", "rej"):
            return f"{t}  ⚠ CLIENT ERROR — {one(qs,'m','')[:90]} @ {one(qs,'at','')}"
        return f"{t}  {ev}"

    CATLBL = {"person": "\U0001f464 person", "dc": "\U0001f3e2 dc/vpn", "bot": "\U0001f916 bot"}
    A("<h2>Visitors — click a row for full detail</h2>")
    A('<p class="dim" style="font-size:.82em;margin:0 0 6px">'
      'Datacenter/VPN and bot IPs geolocate to where the <i>server</i> is, not the person. '
      'No email or lyric text is ever collected, so rows identify a network, not a user.</p>')
    A("<table><tr><th>last seen</th><th>where</th><th>type</th><th>device</th>"
      "<th>opens</th><th>songs</th><th>ip</th></tr>")
    for i, (ip, v) in enumerate(visitor_rows):
        g = geo.get(ip, {})
        where = ", ".join(x for x in (g.get("city"), g.get("regionName"), g.get("country")) if x) or "—"
        o, b, form = ua_device(v["ua"])
        last = v["last"].strftime("%m-%d %H:%M") if v["last"] else "—"
        c = cat(ip)
        A(f'<tr class="vis" onclick="tg(\'d{i}\')"><td>{last}</td><td>{escape(where)}</td>'
          f'<td>{CATLBL[c]}</td><td class="dim">{o} · {b} · {form}</td>'
          f'<td>{v["loads"]}</td><td>{v["songs"]}</td>'
          f'<td class="dim">{escape(mask(ip))}</td></tr>')
        tl = "\n".join(escape(describe(e[0], e[2], e[3])) for e in events_by_ip.get(ip, []))
        A(f'<tr class="det" id="d{i}" style="display:none"><td colspan="7">'
          f'<div><b>{escape(where)}</b> · {escape(g.get("isp","—"))} · {CATLBL[c]}'
          f'{" · hosting/VPN" if g.get("hosting") else ""}</div>'
          f'<div class="dim">full user-agent: {escape(v["ua"])}</div>'
          f'<pre class="tl">{tl}</pre></td></tr>')
    if not visitor_rows:
        A('<tr><td colspan="7" class="dim">no visitors recorded yet</td></tr>')
    A("</table>")

    if apierrs or errs:
        A(f"<h2>Errors ({len(errs)} client, {len(apierrs)} API)</h2>")
        agg = {}
        for ts, ip, ev, qs, ua, bot in errs + apierrs:
            k = (one(qs, "m", ""), one(qs, "ep", "") or one(qs, "at", ""), ev)
            a = agg.setdefault(k, {"n": 0, "last": ts})
            a["n"] += 1
            if ts and (a["last"] is None or ts > a["last"]):
                a["last"] = ts
        A("<table><tr><th>count</th><th>last seen</th><th>kind</th><th>where</th><th>message</th></tr>")
        for (msg, where, ev), a in sorted(agg.items(),
                                          key=lambda x: x[1]["last"] or AWARE_MIN, reverse=True):
            last = a["last"].strftime("%m-%d %H:%M") if a["last"] else "—"
            A(f'<tr><td>{a["n"]}</td><td>{last}</td><td class="dim">{ev}</td>'
              f'<td class="dim">{escape(where)}</td><td class="dim">{escape(msg[:100])}</td></tr>')
        A("</table>")

    A(f'<div class="foot">generated {gen} · {len(events)} telemetry events parsed · '
      f'geo via ip-api.com (cached) · IPs masked · sibling: '
      f'<a href="/" style="color:#ffb000">Ghosts in the Loop</a></div>')
    A("<script>function tg(id){var d=document.getElementById(id);"
      "d.style.display=d.style.display==='table-row'?'none':'table-row'}</script>")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        fh.write("<!doctype html><meta charset=utf-8>" + "".join(H))
    print(f"wrote {OUT}: {len(people_ips)} people, {len(loads)} opens, "
          f"{paste_ok + auto_ok} songs analyzed")


if __name__ == "__main__":
    main()
