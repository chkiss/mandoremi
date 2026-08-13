from app import analyze, hskdata

from conftest import SONG


def test_basic_analysis_shape():
    a = analyze.analyze(SONG)
    assert a["version"] == hskdata.config()["analysis_version"]
    assert a["lines"] and a["vocab"]
    st = a["stats"]
    assert st["chinese_tokens"] > 0
    assert 0 < st["richness"] <= 1
    assert set(st["per_level"]) == set(analyze.LEARNER_LEVELS)


def test_latin_tokens_excluded_from_scoring():
    """Latin/digits are level 9: shown in the distribution but never counted
    in coverage, richness, unique vocab, or unknown-word stats."""
    base = analyze.analyze("月亮代表我的心")
    noisy = analyze.analyze("月亮代表我的心\nbaby oh yeah 12345")
    assert noisy["stats"]["counts_by_level"][9] > 0
    for key in ("chinese_tokens", "unique_vocab", "richness"):
        assert noisy["stats"][key] == base["stats"][key]
    for lvl in analyze.LEARNER_LEVELS:
        assert noisy["stats"]["per_level"][lvl] == base["stats"]["per_level"][lvl]


def test_coverage_monotonic_in_level():
    a = analyze.analyze(SONG)
    per = a["stats"]["per_level"]
    covs = [per[l]["coverage"] for l in analyze.LEARNER_LEVELS]
    assert covs == sorted(covs)
    assert covs[0] == 0.0  # pre-HSK1 learner knows nothing


def test_learning_value_bounds():
    a = analyze.analyze(SONG)
    for l in analyze.LEARNER_LEVELS:
        assert 0 <= a["stats"]["per_level"][l]["learning_value"] <= 100


def test_fillers_not_counted():
    a = analyze.analyze("啊 啊 啊 月亮")
    assert "啊" not in a["vocab"]
    assert a["stats"]["chinese_tokens"] == 1


def test_idiom_detected():
    a = analyze.analyze("轰轰烈烈的爱情")
    assert any(i["word"] == "轰轰烈烈" for i in a["idioms"])


def test_personalize_merges_split_oov_word():
    """A personal known word the segmenter split (滄海 -> 沧/海) is re-merged
    and counts as known at every level."""
    a = analyze.analyze("滄海一聲笑")
    known = frozenset(["沧海"])
    p = analyze.personalize(a, known)
    assert "沧海" in p["vocab"] and p["vocab"]["沧海"]["known"] == 1
    assert p["stats"]["per_level"][0]["coverage"] > 0


def test_personalize_flags_substrings_of_known_words():
    """没 with 没有 on the list: not known, but flagged "p" (probably known)
    so the UI can group it apart from real words-to-learn."""
    a = analyze.analyze("我没说 我学")
    p = analyze.personalize(a, frozenset(["没有", "学习"]))
    assert p["vocab"]["没"].get("p") == 1
    assert p["vocab"]["学"].get("p") == 1
    assert "p" not in p["vocab"]["我"]          # not inside any known word
    a2 = analyze.analyze("我没有")
    p2 = analyze.personalize(a2, frozenset(["没有"]))
    assert p2["vocab"]["没有"]["known"] == 1    # known words never get "p"
    assert "p" not in p2["vocab"]["没有"]


def test_strip_text_removes_all_lyric_text():
    a = analyze.analyze("轰轰烈烈的爱情\n你问我爱你有多深")
    g = analyze.strip_text(a)
    assert "lines" not in g and g["ghost"] == 1
    for pat in g["grammar"]:
        assert "examples" not in pat
    dumped = str(g)
    assert "轰轰烈烈的爱情" not in dumped     # no line survives
    assert g["stats"] == a["stats"]
    # ghost analyses still personalize (vocab flags + stats), sans line merge
    p = analyze.personalize(g, frozenset(["爱情"]))
    assert p["vocab"]["爱情"]["known"] == 1
    assert p["stats"]["per_level"][0]["coverage"] > 0


def test_personalize_empty_known_is_noop():
    a = analyze.analyze(SONG)
    assert analyze.personalize(a, frozenset()) is a


def test_lyrics_hash_ignores_junk():
    h1 = analyze.lyrics_hash("[00:01.00]月亮代表我的心！")
    h2 = analyze.lyrics_hash("月亮代表我的心")
    assert h1 == h2


def test_long_input_performance():
    # ~6k chars of lyric-like text should analyze in interactive time
    import time
    text = "\n".join(["月亮代表我的心 你问我爱你有多深 轰轰烈烈的爱情"] * 300)
    t0 = time.time()
    analyze.analyze(text)
    assert time.time() - t0 < 10
