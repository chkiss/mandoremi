# Mandoremi

Mandoremi is a web app for Chinese language learners that analyzes the linguistic difficulty of Chinese song lyrics.

- **Standard:** HSK 3.0 — levels 1–6 plus a combined 7–9 band (word lists from krmanik/HSK-3.0; idioms from THUOCL 成语 lexicon)
- **Segmentation:** spacy-pkuseg with an HSK+idiom user dictionary (longest match, never per-character); model auto-downloads to `~/.pkuseg` on first run
- **Traditional input:** OpenCC t2s plus lyric-specific fixups (妳→你, particle 著→着)
- **Inputs:** manual `Artist - Title` lists, single songs, Spotify public playlist scrape (embed page, no OAuth — fails soft if Spotify changes markup)
- **Privacy:** lyrics are stored only on the uploading user's own song rows and served only to them; the shared analysis cache holds statistics only due to lyrics licensing issues

## Run

```
virtualenv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8787
```

`HSKLYRICS_DB` overrides the SQLite path; `HSKLYRICS_SECURE_COOKIES=0` for plain-HTTP local testing.

## Layout

- `app/` — FastAPI backend + analysis pipeline (normalize → segment → classify → grammar → stats)
- `data/` — HSK word lists, chengyu, grammar patterns, normalization rules, learning-value weights (`config.json`)
- `static/` — single-page vanilla-JS frontend

> **Note:** the public crawlable pages (`/artists`, `/artist/{slug}`, `/song/...`, `/chengyu/...`, the sitemap) are wired in `app/public_pages.py`, which is intentionally **gitignored** from this repository. `app/main.py` registers those routes only when the file is present (it is, on the production deploy at mandoremi.com); a fresh clone of this repo runs as the analysis tool alone, with those routes simply absent. User data, the SQLite DB, the virtualenv, and generated data files are also gitignored.

Level codes in stored analyses: 1–6 = HSK1–6, 7 = HSK7–9, 8 = beyond HSK, 9 = unknown/non-Chinese, 0 = filler.

## Deployment

The app is intended to run as a `systemd --user` service bound to
`127.0.0.1:8790`, fronted by an nginx vhost. Deploy tooling and host-specific
config are kept out of this repository (gitignored) — see your local
`deploy.sh` and `tools/mandoremi-limits.conf`. The nightly DB snapshot script
is `tools/backup_db.py`.
