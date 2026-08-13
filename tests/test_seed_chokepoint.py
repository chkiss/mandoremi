"""Structural guards: keep the seeding invariants enforceable, not just documented.

Docs tell the next author what to do; these tests fail the build when a new
caller does something else. Each maps to a specific way this went wrong.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# resolve_text costs ~9s of third-party HTTP. Everything must reach it through
# seed.acquire(), which applies the corpus read, the negative cache and the
# write-back. These are the only files allowed to name it directly.
RESOLVE_ALLOWED = {"app/lyrics_fetch.py", "app/seed.py"}

# Writing the corpus must go through seed.store(), which refuses payloads
# carrying lyric text.
CORPUS_WRITE_ALLOWED = {"app/seed.py", "app/db.py", "tools/dedupe_corpus.py",
                        "tools/resolve_artists.py", "tools/qa_corpus.py",
                        # Repairs the artist_key of rows already written; it
                        # moves and deletes rows but never creates an analysis,
                        # so it cannot introduce lyric text into the corpus.
                        "tools/unmerge_artist_keys.py"}


def _sources():
    for p in list(ROOT.glob("app/*.py")) + list(ROOT.glob("tools/*.py")):
        yield p, p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8")


def test_resolve_text_only_called_through_the_chokepoint():
    offenders = [rel for _p, rel, src in _sources()
                 if rel not in RESOLVE_ALLOWED and re.search(r"\bresolve_text\s*\(", src)]
    assert not offenders, (
        f"{offenders} call lyrics_fetch.resolve_text directly. Use "
        f"seed.acquire(artist, title) instead — it applies the shared-corpus "
        f"read, the seed_miss negative cache and the write-back together. "
        f"See tools/SEEDING.md.")


def test_corpus_is_not_written_by_raw_sql():
    pat = re.compile(r"(INSERT|REPLACE|UPDATE)[^;\"']*seed_analysis", re.I)
    offenders = [rel for _p, rel, src in _sources()
                 if rel not in CORPUS_WRITE_ALLOWED and pat.search(src)]
    assert not offenders, (
        f"{offenders} write seed_analysis with raw SQL, bypassing seed.store() "
        f"and its no-lyric-text check. See tools/SEEDING.md.")


def test_seeding_doc_exists_and_covers_the_invariants():
    doc = (ROOT / "tools" / "SEEDING.md").read_text(encoding="utf-8")
    for token in ("seed_miss", "seed.store", "never stored", "stub"):
        assert token.lower() in doc.lower(), f"SEEDING.md no longer documents {token!r}"


@pytest.mark.parametrize("reason", ["hit", "fetched", "nomatch", "nochinese",
                                    "miss-cached"])
def test_acquire_reasons_are_stable(reason):
    """Callers branch on these strings; renaming one silently breaks them."""
    src = (ROOT / "app" / "seed.py").read_text(encoding="utf-8")
    assert f'"{reason}"' in src
