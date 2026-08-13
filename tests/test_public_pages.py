"""The public difficulty pages.

The load-bearing test here is test_public_pages_never_show_lyrics: these pages
are only publishable because the seed corpus carries counts and no text. If a
refactor ever puts lyric text back into a seed analysis, or points a public
page at the `songs` table, that test is what catches it.

The whole module needs the two local-only files: app/public_pages.py wires the
routes and app/article.py supplies the prose, and both are gitignored. On a
clean clone there is nothing here to test, so the module skips rather than
fails -- same rule as tests/test_seo.py.
"""
import importlib.util
import json
import re

import pytest

from app import db, public

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("app.public_pages") is None
    or importlib.util.find_spec("app.article") is None,
    reason="app/public_pages.py and app/article.py are gitignored/local-only")


ARTISTS = [
    # (artist_id, display, latin alias, n songs, level mix)
    (100, "简单乐团", "easyband", 20, {"1": 70, "2": 15, "3": 10, "7": 5}),
    (200, "银临", "yinlin", 18, {"1": 30, "2": 10, "3": 10, "7": 30, "8": 20}),
    (300, "小众", "tiny", 3, {"1": 60, "2": 20, "3": 10, "7": 10}),
    (400, "中等乐队", "midband", 16, {"1": 50, "2": 15, "3": 10, "4": 10, "7": 15}),
]


def _analysis(mix, title, scale=10):
    # Scaled so a fixture artist clears MIN_TOKENS the way a real one does;
    # at 100 tokens/song they fell under the eligibility floor and quietly
    # dropped out of the ranking the tests were meant to exercise.
    counts = {str(i): 0 for i in range(1, 10)}
    counts.update({k: v * scale for k, v in mix.items()})
    zh = sum(counts[str(i)] for i in range(1, 9))
    return {
        "version": 6,
        "ghost": 1,
        "vocab": {"寂寞": {"lvl": 7, "count": 3, "idiom": 0, "forms": []},
                  "星": {"lvl": 8, "count": 2, "idiom": 0, "forms": []},
                  "我": {"lvl": 1, "count": 9, "idiom": 0, "forms": []}},
        "stats": {
            "total_tokens": zh, "chinese_tokens": zh, "unique_vocab": 40,
            "richness": 0.3, "counts_by_level": counts,
            "unique_by_level": counts,
            "per_level": {str(l): {"coverage": min(0.35 + 0.1 * l, 0.98),
                                   "unique_unknown": 20 - l,
                                   "repeated_unknown": 5,
                                   "avg_reps_unknown": 2.0,
                                   "learning_value": 50.0 + l}
                          for l in range(0, 8)},
        },
        "idioms": [{"word": "不知不觉", "count": 1, "lvl": 8}],
        "grammar": [{"key": "le", "name": "了 (aspect/change)",
                     "level": 1, "count": 4}],
    }


@pytest.fixture
def corpus(client, monkeypatch, tmp_path):
    """Seed a corpus into the client's temp DB (conftest patches db.DB_PATH).

    /artists normally renders from the frozen snapshot in data/, which knows
    nothing about these fixture artists. Point it at a path that does not exist
    so the page falls back to live data and these tests measure the pipeline
    rather than a checked-in file. test_snapshot.py covers the frozen path.
    """
    monkeypatch.setattr(public, "SNAPSHOT_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(public, "_SNAPSHOT", {"loaded": False, "data": None})
    with db.connect() as conn:
        for aid, name, alias, n, mix in ARTISTS:
            conn.execute("INSERT INTO artist_alias (alias_key, artist_id, display,"
                         " confidence) VALUES (?,?,?,?)", (alias, aid, name, "exact"))
            conn.execute("INSERT INTO artist_alias (alias_key, artist_id, display,"
                         " confidence) VALUES (?,?,?,?)", (name, aid, name, "exact"))
            for i in range(n):
                conn.execute(
                    "INSERT INTO seed_analysis (artist_key, title_key, version,"
                    " lyrics_hash, analysis, artist_id) VALUES (?,?,?,?,?,?)",
                    (alias, f"song{i}", 6, f"h{aid}{i}",
                     json.dumps(_analysis(mix, f"song{i}")), aid))
    public._cache.update({"built": 0.0, "data": None})
    yield client
    public._cache.update({"built": 0.0, "data": None})


def test_leaderboard_ranks_easy_above_hard(corpus, client):
    r = client.get("/artists")
    assert r.status_code == 200
    assert r.text.index("简单乐团") < r.text.index("银临")


def test_only_artists_with_enough_songs_are_ranked(corpus):
    d = public.data(force=True)
    ranked = {a["name"] for a in d["ranked"]}
    assert "简单乐团" in ranked
    assert "小众" not in ranked          # 3 songs, below MIN_SONGS


def test_unranked_artist_still_reachable(corpus, client):
    d = public.data(force=True)
    tiny = next(a for a in d["artists"] if a["name"] == "小众")
    assert client.get(f"/artist/{tiny['slug']}").status_code == 200
    assert "too few songs to rank" in client.get(f"/artist/{tiny['slug']}").text


def test_percentages_use_chinese_tokens_and_never_exceed_100(corpus):
    for a in public.data(force=True)["artists"]:
        total = sum(a["levels"].values())
        assert 99.0 <= total <= 101.0, (a["name"], total)
        assert 0 <= a["easy_pct"] <= 100


def test_non_chinese_tokens_are_excluded(client):
    """Level 9 is 'not Chinese', not 'HSK 9' -- English hooks must not count."""
    a = _analysis({"1": 50, "2": 50}, "x")
    a["stats"]["counts_by_level"]["9"] = 400      # a chorus of English
    with db.connect() as conn:
        conn.execute("INSERT INTO artist_alias (alias_key, artist_id, display,"
                     " confidence) VALUES (?,?,?,?)", ("rap", 900, "说唱", "exact"))
        for i in range(20):
            conn.execute(
                "INSERT INTO seed_analysis (artist_key, title_key, version,"
                " lyrics_hash, analysis, artist_id) VALUES (?,?,?,?,?,?)",
                ("rap", f"s{i}", 6, f"z{i}", json.dumps(a), 900))
    public._cache.update({"built": 0.0, "data": None})
    art = next(x for x in public.data(force=True)["artists"] if x["name"] == "说唱")
    assert art["easy_pct"] == pytest.approx(100.0)   # not 20%
    public._cache.update({"built": 0.0, "data": None})


def test_one_long_outlier_cannot_redefine_an_artist(client):
    """王菲's corpus holds a 4,761-token sutra: 31% of her words, one track.

    Token-weighted, that single song moves her score materially. The headline
    is the median song precisely so it cannot.
    """
    normal = _analysis({"1": 200, "2": 50, "3": 50}, "pop")     # 100% HSK 1-3
    outlier = _analysis({"1": 100, "7": 2000, "8": 2000}, "sutra")
    with db.connect() as conn:
        conn.execute("INSERT INTO artist_alias (alias_key, artist_id, display,"
                     " confidence) VALUES (?,?,?,?)", ("diva", 700, "天后", "exact"))
        for i in range(19):
            conn.execute(
                "INSERT INTO seed_analysis (artist_key, title_key, version,"
                " lyrics_hash, analysis, artist_id) VALUES (?,?,?,?,?,?)",
                ("diva", f"p{i}", 6, f"p{i}", json.dumps(normal), 700))
        conn.execute(
            "INSERT INTO seed_analysis (artist_key, title_key, version,"
            " lyrics_hash, analysis, artist_id) VALUES (?,?,?,?,?,?)",
            ("diva", "sutra", 6, "sut", json.dumps(outlier), 700))
    public._cache.update({"built": 0.0, "data": None})
    a = next(x for x in public.data(force=True)["artists"] if x["name"] == "天后")
    assert a["easy_pct"] == pytest.approx(100.0)      # median song, unaffected
    assert a["easy_weighted"] < 60                    # what pooling would say
    public._cache.update({"built": 0.0, "data": None})


def test_public_pages_never_show_lyrics(corpus, client):
    """A seed analysis has no lines and no lyric text; neither may a page."""
    pages = [client.get("/artists").text]
    for a in public.data()["artists"]:
        pages.append(client.get(f"/artist/{a['slug']}").text)
    for html in pages:
        # No analysis blob, and none of the fields that would carry text.
        assert '"per_level"' not in html
        assert '"lines"' not in html
        assert '"vocab"' not in html
        assert "<pre" not in html


def test_no_session_required(corpus, client):
    for path in ("/artists", "/artist/easyband"):
        r = client.get(path)
        assert r.status_code == 200
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_unknown_artist_404s(corpus, client):
    assert client.get("/artist/nobody-here").status_code == 404


def test_slug_is_url_safe(corpus):
    for a in public.data(force=True)["artists"]:
        assert re.fullmatch(r"[a-z0-9-]+", a["slug"]), a["slug"]


def test_chinese_only_artist_with_no_canonical_id_gets_a_usable_slug(client):
    """伍佰 in production: no artist_id and a name that slugifies to nothing."""
    a = json.dumps(_analysis({"1": 60, "2": 40}, "x"))
    with db.connect() as conn:
        for i in range(20):
            conn.execute(
                "INSERT INTO seed_analysis (artist_key, title_key, version,"
                " lyrics_hash, analysis, artist_id) VALUES (?,?,?,?,?,NULL)",
                ("伍佰", f"s{i}", 6, f"w{i}", a))
    public._cache.update({"built": 0.0, "data": None})
    art = next(x for x in public.data(force=True)["artists"] if x["name"] == "伍佰")
    assert re.fullmatch(r"[a-z0-9-]+", art["slug"])
    assert client.get(f"/artist/{art['slug']}").status_code == 200
    public._cache.update({"built": 0.0, "data": None})


def test_featuring_follows_popularity_not_corpus_depth(client):
    """The 周杰伦 / deca joins inversion: a famous artist we could seed only
    lightly must still be featured above a niche one we seeded deeply."""
    famous = _analysis({"1": 60, "2": 40}, "x")
    niche = _analysis({"1": 60, "2": 40}, "y")
    with db.connect() as conn:
        conn.execute("INSERT INTO artist_alias (alias_key, artist_id, display,"
                     " confidence, popularity) VALUES (?,?,?,?,?)",
                     ("famous", 800, "大明星", "exact", 54_000_000))
        conn.execute("INSERT INTO artist_alias (alias_key, artist_id, display,"
                     " confidence, popularity) VALUES (?,?,?,?,?)",
                     ("niche", 801, "小乐队", "exact", 0))
        for i in range(16):                     # lightly seeded, very famous
            conn.execute("INSERT INTO seed_analysis (artist_key, title_key,"
                         " version, lyrics_hash, analysis, artist_id)"
                         " VALUES (?,?,?,?,?,?)",
                         ("famous", f"f{i}", 6, f"f{i}", json.dumps(famous), 800))
        for i in range(60):                     # deeply seeded, unknown
            conn.execute("INSERT INTO seed_analysis (artist_key, title_key,"
                         " version, lyrics_hash, analysis, artist_id)"
                         " VALUES (?,?,?,?,?,?)",
                         ("niche", f"n{i}", 6, f"n{i}", json.dumps(niche), 801))
    public._cache.update({"built": 0.0, "data": None})
    old_f, old_o = public.FEATURED_N, public.OUTLIERS_N
    # Outliers off, one slot: the slot must go to fame, not to song count.
    public.FEATURED_N, public.OUTLIERS_N = 1, 0
    try:
        d = public.data(force=True)
        assert [a["name"] for a in d["featured"]] == ["大明星"]
        assert "小乐队" in [a["name"] for a in d["rest"]]
    finally:
        public.FEATURED_N, public.OUTLIERS_N = old_f, old_o
        public._cache.update({"built": 0.0, "data": None})


def test_leaderboard_caps_the_table(corpus, monkeypatch):
    """Featured list is capped, but every ranked artist stays linked."""
    monkeypatch.setattr(public, "FEATURED_N", 1)
    monkeypatch.setattr(public, "OUTLIERS_N", 1)
    d = public.data(force=True)
    assert len(d["featured"]) <= len(d["ranked"])
    assert {id(x) for x in d["featured"]} | {id(x) for x in d["rest"]} == \
           {id(x) for x in d["ranked"]}


def test_slug_for_resolves_aliases_and_traditional_characters(corpus):
    """The reason this is server-side: folding 銀臨 -> 银临 needs OpenCC."""
    assert public.slug_for("yinlin") == public.slug_for("银临")
    assert public.slug_for("银临") is not None
    assert public.slug_for("銀臨") == public.slug_for("银临")
    # First credited artist wins, so a featuring credit still links.
    assert public.slug_for("银临, 河图") == public.slug_for("银临")


def test_slug_for_returns_none_when_no_public_page(corpus):
    assert public.slug_for("Some Unseeded Band") is None
    assert public.slug_for("") is None
    assert public.slug_for(None) is None


def test_song_payloads_carry_artist_slug(corpus, client):
    from tests.conftest import register
    register(client)
    sid = client.post("/api/songs", json={"artist": "银临", "title": "锦鲤抄"}).json()["id"]
    assert client.get(f"/api/songs/{sid}").json()["artist_slug"] == \
        public.slug_for("银临")
    listed = {s["id"]: s for s in client.get("/api/songs").json()}
    assert listed[sid]["artist_slug"] == public.slug_for("银临")


def test_unknown_artist_song_gets_null_slug_not_a_broken_link(corpus, client):
    from tests.conftest import register
    register(client)
    sid = client.post("/api/songs",
                      json={"artist": "Nobody At All", "title": "x"}).json()["id"]
    assert client.get(f"/api/songs/{sid}").json()["artist_slug"] is None


def test_song_page_renders_without_lyrics(corpus, client):
    d = public.data(force=True)
    a = next(x for x in d["artists"] if x["name"] == "简单乐团")
    s = a["songs"][0]
    r = client.get(f"/song/{a['slug']}/{s['slug']}")
    assert r.status_code == 200
    assert '"per_level"' not in r.text and '"lines"' not in r.text
    assert "Paste the lyrics" in r.text


def test_song_page_404s_for_unknown_song(corpus, client):
    d = public.data(force=True)
    a = d["artists"][0]
    assert client.get(f"/song/{a['slug']}/no-such-song").status_code == 404
    assert client.get("/song/no-such-artist/x").status_code == 404


def test_song_slugs_are_unique_within_an_artist(corpus):
    for a in public.data(force=True)["artists"]:
        got = [s["slug"] for s in a["songs"]]
        assert len(got) == len(set(got)), a["name"]
        assert all(re.fullmatch(r"[a-z0-9-]+", g) for g in got)


def test_song_path_for_only_resolves_seeded_songs(corpus):
    d = public.data(force=True)
    a = next(x for x in d["artists"] if x["name"] == "简单乐团")
    title = a["songs"][0]["title"]
    assert public.song_path_for("简单乐团", title) == \
        f"/song/{a['slug']}/{a['songs'][0]['slug']}"
    assert public.song_path_for("简单乐团", "a song they never recorded") is None
    assert public.song_path_for("Unknown Band", title) is None


def test_full_name_pairs_chinese_with_english(corpus):
    assert public.full_name({"name": "周杰伦", "english": "Jay Chou"}) == \
        "周杰伦 (Jay Chou)"
    # never "Beyond (Beyond)"
    assert public.full_name({"name": "Beyond", "english": "Beyond"}) == "Beyond"
    assert public.full_name({"name": "银临", "english": None}) == "银临"
    # NetEase glues an act's two scripts into one name so both are findable;
    # these are one artist and should read like everyone else.
    assert public.full_name({"name": "艾志恒Asen"}) == "艾志恒 (Asen)"
    assert public.full_name({"name": "理想混蛋Bestards"}) == "理想混蛋 (Bestards)"
    assert public.full_name({"name": "李大奔BENZO"}) == "李大奔 (BENZO)"
    assert public.full_name({"name": "G.E.M.邓紫棋"}) == "邓紫棋 (G.E.M.)"
    # …but a label suffix is not an English name, and a band is not two artists
    assert public.full_name({"name": "洛天依Official"}) == "洛天依Official"
    assert public.full_name({"name": "伍佰 & China Blue"}) == "伍佰 & China Blue"


def test_distinctive_words_exclude_one_off_and_ultra_rare(corpus):
    for a in public.data(force=True)["artists"]:
        for w, ratio, docs in a["distinctive"]:
            assert docs >= 3          # not a proper noun from a single track
            assert ratio > 1.5


def test_leaderboard_has_the_scatter(corpus, client):
    html = client.get("/artists").text
    assert 'class="scatter"' in html
    assert "role=\"img\"" in html      # labelled for screen readers


def test_pinyin_fallback_writes_names_the_way_english_does(corpus):
    """Surname then given name, joined. "Huang Xiao Yun" is not how anyone
    writes a Chinese name in English."""
    got = public._en_pinyin_fallback(
        ["黄霄雲", "单依纯", "周深", "鹿晗", "欧阳靖"])
    assert got["黄霄雲"] == "Huang Xiaoyun"
    assert got["单依纯"] == "Dan Yichun"
    assert got["周深"] == "Zhou Shen"
    assert got["鹿晗"] == "Lu Han"
    assert got["欧阳靖"] == "Ouyang Jing"      # two-character surname


def test_pinyin_fallback_spells_u_umlaut_not_v(corpus):
    """pypinyin's ASCII default renders ü as the letter v — a keyboard input
    convention, not a spelling. 旅 must not come out "Lv"."""
    got = public._en_pinyin_fallback(["吕布", "女娲"])
    assert "v" not in "".join(got.values()).lower(), got
    assert got["吕布"].startswith("Lü")


def test_pinyin_fallback_leaves_bands_and_mixed_names_alone(corpus):
    """A band romanised syllable by syllable tells an English reader less than
    the Chinese does: 万能青年旅店 is Omnipotent Youth Society, and only a real
    name source (Wikidata) can know that."""
    got = public._en_pinyin_fallback(
        ["万能青年旅店", "透明教室与平行女孩", "艾志恒Asen", "deca joins", "Beyond",
         # short enough to look like a personal name, but a band: 好乐团 is
         # GoodBand, not "Hao Yuetuan"
         "好乐团", "黑屋乐队", "信乐团"])
    assert got == {}


def test_curated_names_beat_the_romanisation(corpus, monkeypatch):
    monkeypatch.setattr(public, "_EN_OVERRIDE", {"银临": "Yin Lin"})
    assert public.full_name({"name": "银临", "english": "Yin Lin"}) == \
        "银临 (Yin Lin)"


def test_genre_legend_only_lists_genres_present(corpus, client):
    html = client.get("/artists").text
    shown = set(re.findall(r'class="glitem" data-genre="([^"]+)"', html))
    assert shown <= set(public.GENRE_COLORS), "legend shows an unstyled genre"


def test_hsk_control_is_a_slider(corpus, client):
    html = client.get("/artists").text
    assert 'type="range"' in html
    assert "<select" not in html
    assert 'id="lvselect"' in html


def test_every_point_has_a_label_element_for_every_level(corpus, client):
    """Labels were rendered once from HSK 3 and never moved, so at every other
    level they sat at the wrong height and named the wrong artists."""
    html = client.get("/artists").text
    pts = re.findall(r'<circle class="pt"', html)
    labels = re.findall(r'<text class="ptlabel[^"]*" data-slug="([^"]+)"', html)
    assert len(labels) == len(pts), "a label per point, hidden until it is named"
    blob = json.loads(re.search(
        r'id="scatter-data"[^>]*>(.*?)</script>', html, re.S).group(1))
    # each point carries a coverage figure for every level to move to
    for p in blob["points"]:
        assert set(p["cov"]) == {"1", "2", "3", "4", "5", "6", "7"}


def test_label_rule_names_the_extremes_of_both_axes(corpus):
    pts = [{"slug": "s%02d" % i, "cov": float(i), "learn": float(50 - i)}
           for i in range(30)]
    got = public._label_slugs(pts)
    # lowest and highest coverage
    assert {"s00", "s01", "s02", "s27", "s28", "s29"} <= got
    # learn runs the other way, so its extremes are the same end points here
    assert len(got) <= 12


def test_label_rule_names_everything_in_a_small_selection(corpus):
    """Filter to a four-artist genre and all four get named: there is room,
    and a filtered view showing one name of four reads as broken."""
    pts = [{"slug": "a", "cov": 1, "learn": 9},
           {"slug": "b", "cov": 2, "learn": 8},
           {"slug": "c", "cov": 3, "learn": 7},
           {"slug": "d", "cov": 4, "learn": 6}]
    assert public._label_slugs(pts) == {"a", "b", "c", "d"}


def test_label_rule_breaks_ties_deterministically(corpus):
    """Near-ties are everywhere at the extremes; the pick must not wobble
    between identical redraws."""
    pts = [{"slug": s, "cov": 50.0, "learn": 50.0}
           for s in "abcdefghijklmno"]
    assert public._label_slugs(pts) == public._label_slugs(list(reversed(pts)))


def test_labels_track_both_axes_and_the_genre_filter(corpus, client):
    """Two failures this guards against, both seen live:

    * updating only `y` stranded names up to 197px from their dot once the
      x axis became level-dependent
    * a dimmed point keeping its name, so a filtered view was captioned with
      artists it was no longer showing
    """
    html = client.get("/artists").text
    script = re.search(r"<script>\n\(function\(\).*?</script>", html, re.S).group(0)
    assert 'setAttribute("x"' in script and 'setAttribute("y"' in script, \
        "labels must be repositioned on BOTH axes"
    # the label set is recomputed from what is on screen
    assert "function labelSet(" in script
    assert "function visible()" in script
    assert "p.genre" in script, "the filter must feed the label choice"
    # and recomputed when either input changes
    assert script.count("apply()") >= 3, "level and filter must both re-apply"


def test_sitemap_includes_artist_pages(corpus, client):
    xml = client.get("/sitemap.xml").text
    assert "/artists<" in xml
    assert "/artist/easyband" in xml
