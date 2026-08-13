"""The chengyu table is the one place the app teaches rather than measures, so
these tests are about whether a learner would be misled, not just whether the
code runs."""
import pytest

from app import chengyu, dictionary


@pytest.fixture(scope="module", autouse=True)
def loaded():
    dictionary.load()


def test_pinyin_uses_tone_marks_not_digits():
    e = chengyu.entry("不知不觉")
    assert e["pinyin"] == "bù zhī bù jué"
    assert e["pinyin_exact"]


def test_tone_mark_placement_follows_orthography():
    # a and e win; 'ou' marks the o; otherwise the last vowel. Getting this
    # wrong produces pinyin that looks right at a glance and is not.
    assert dictionary.to_tone_marks("hao3") == "hǎo"
    assert dictionary.to_tone_marks("gou3") == "gǒu"
    assert dictionary.to_tone_marks("hui4") == "huì"
    assert dictionary.to_tone_marks("lv4") == "lǜ"
    assert dictionary.to_tone_marks("nu:3") == "nǚ"
    assert dictionary.to_tone_marks("de5") == "de"


def test_literal_uses_the_reading_the_chengyu_actually_has():
    # 觉 is "a nap" as jiao4 and "to feel" as jue2. 不知不觉 is jue2, and the
    # per-character breakdown must not reach for the other reading.
    lit = chengyu.entry("不知不觉")["literal"]
    assert "feel" in lit and "nap" not in lit


def test_literal_prefers_the_common_word_over_the_surname():
    # CC-CEDICT lists 顾/Gu4 "surname Gu" alongside 顾/gu4 "to look after",
    # and 山/Shan1 before 山/shan1. A breakdown full of surnames is noise.
    for word in ("奋不顾身", "千山万水", "莫名其妙"):
        assert "surname" not in chengyu.entry(word)["literal"]


def test_classical_particles_are_not_glossed_as_modern_words():
    # 所 leads with "actually" in the dictionary; inside 一无所有 it nominalises.
    assert "that which" in chengyu.entry("一无所有")["literal"]


def test_explicit_literal_is_kept_and_split_from_the_figurative():
    e = chengyu.entry("画蛇添足")
    assert e["literal_exact"]
    assert "snake" in e["literal"]
    # The "fig." half belongs in the meaning column, not the literal one.
    assert "fig." not in e["literal"].lower()
    assert "superfluous" in e["meaning"]


def test_idiom_marker_is_stripped_without_leaving_stray_punctuation():
    m = chengyu.entry("一无所有")["meaning"]
    assert "(idiom)" not in m
    assert " ;" not in m


def test_word_without_its_own_entry_still_gets_a_flagged_reading():
    # 日日夜夜 is not in CC-CEDICT; assembling it per character is right here
    # (reduplication), but the caller must be able to tell it was assembled.
    e = chengyu.entry("日日夜夜")
    assert e["pinyin"] == "rì rì yè yè"
    assert not e["pinyin_exact"]


def test_every_chengyu_in_the_lexicon_renders_without_crashing():
    from app import hskdata
    for word in list(hskdata.idiom_set())[:1500]:
        e = chengyu.entry(word)
        assert e["word"] == word
        for key in ("pinyin", "literal", "meaning"):
            assert e[key] is None or isinstance(e[key], str)
