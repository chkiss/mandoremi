from app import dictionary, hskdata


def test_classify_levels():
    assert hskdata.classify("啊")[1] == hskdata.LEVEL_FILLER
    assert hskdata.classify("baby")[1] == hskdata.LEVEL_UNKNOWN
    assert hskdata.classify("爱")[1] == 1
    # real Chinese word outside every HSK list
    assert hskdata.classify("轰轰烈烈")[1] == hskdata.LEVEL_BEYOND


def test_oov_compounds_decompose_to_hardest_part():
    """Segmenter compounds of HSK words must not land in "beyond HSK"."""
    for w in ("不是", "这就是", "没说", "真是"):
        _, lvl = hskdata.classify(w)
        assert lvl <= 7, f"{w} classified {lvl}"
    assert hskdata.classify("不是")[1] == 1


def test_idioms_never_decompose():
    # chengyu stay beyond-HSK even when their characters are HSK words
    for w in ("口是心非", "是是非非"):
        assert hskdata.classify(w)[1] == hskdata.LEVEL_BEYOND


def test_partial_oov_stays_beyond():
    # contains a non-HSK part -> still beyond HSK
    assert hskdata.classify("沧海")[1] == hskdata.LEVEL_BEYOND


def test_dictionary_loads_once_and_glosses():
    dictionary.load()
    n = len(dictionary._glosses)
    assert n > 100000
    dictionary.load()  # idempotent
    assert len(dictionary._glosses) == n
    assert "moon" in dictionary.gloss("月亮")
    assert dictionary.gloss("不存在的词xyz") is None


def test_gloss_length_capped():
    dictionary.load()
    assert all(len(g) <= dictionary._MAX_LEN for g in dictionary._glosses.values())


def test_surname_sense_not_first():
    dictionary.load()
    assert not dictionary.gloss("王").startswith("surname")


def test_compound_gloss_fallback():
    dictionary.load()
    g = dictionary._compound_gloss("多难")
    assert g.startswith("多 (") and "难 (" in g
    assert dictionary._compound_gloss("不存在的词xyz") is None
    from app import analyze
    a = dictionary.annotate(analyze.analyze("你知道我有多难过"))
    assert "难 (" in a["vocab"]["多难"]["g"]


def test_annotate_adds_g_only_where_found():
    from app import analyze
    a = analyze.analyze("月亮代表我的心 baby")
    dictionary.annotate(a)
    assert "g" in a["vocab"]["月亮"]
    assert "g" not in a["vocab"].get("baby", {})
    assert dictionary.annotate(None) is None
