"""norm() memoisation: exact-text key, bounded, never stale."""
from __future__ import annotations

import random
import re
import sqlite3
import unicodedata

import pytest

from tools import memory_kernel_read as mkr
from tools.memory_kernel_store import MEMORY_TYPES


def reference_norm(text):
    """The function exactly as it was before memoisation."""
    folded = unicodedata.normalize("NFKC", re.sub(r"\[\[|\]\]", "", str(text or ""))).lower()
    return "".join(ch for ch in folded if ch.isalnum())


@pytest.fixture(autouse=True)
def fresh_memo():
    mkr.clear_norm_memo()
    yield
    mkr.clear_norm_memo()


CORPUS = [
    "", " ", None, 0, 1, 12.5, False, True, [], ["a"], {}, "plain ASCII text",
    "MiXeD Case 123", "[[SAVE:note]] tagged [[x]]", "[[", "]]", "[[]]",
    "ＦＵＬＬＷＩＤＴＨ ｆｕｌｌ １２３", "ﬁnance ﬂow ① ② ㈱ ㎏", "İSTANBUL ıı ß ẞ Σς",
    "中文，标点！和 English mix 123", "한국어 テキスト ｶﾀｶﾅ", "emoji 😀 text 👨‍👩‍👧",
    "é combining é", "tab\tnew\nline\r\n", "​ zero­width ﻿",
    "x" * 25_000, "漢" * 21_000, "a [[b]] c " * 2_500,
]


def random_text(rng):
    pool = (
        "abcXYZ 012 ，。！？[[]]中文한ＡＢＣﬁİß́😀\t\n-_"
    )
    return "".join(rng.choice(pool) for _ in range(rng.randint(0, 60)))


@pytest.mark.parametrize("value", CORPUS, ids=lambda v: repr(v)[:30])
def test_norm_equals_the_pre_memo_function_cold_and_warm(value):
    expected = reference_norm(value)
    assert mkr.norm(value) == expected  # miss
    assert mkr.norm(value) == expected  # hit


def test_norm_equals_the_pre_memo_function_on_random_texts():
    rng = random.Random(20261111)
    texts = [random_text(rng) for _ in range(3000)]
    for _ in range(2):  # second pass is served from the memo
        for text in texts:
            assert mkr.norm(text) == reference_norm(text)


def test_memo_is_keyed_by_exact_text_so_a_changed_text_is_recomputed():
    assert mkr.norm("Hello World") == "helloworld"
    assert mkr.norm("Hello Worlds") == "helloworlds"
    assert mkr.norm("hello world") == "helloworld"  # distinct key, same value


def test_memo_is_bounded_with_fifo_eviction(monkeypatch):
    monkeypatch.setattr(mkr, "_NORM_MEMO_MAX_ENTRIES", 5)
    for index in range(50):
        assert mkr.norm(f"text {index}") == f"text{index}"
        assert len(mkr._NORM_MEMO) <= 5
    assert set(mkr._NORM_MEMO) == {f"text {i}" for i in range(45, 50)}
    # an evicted text is simply recomputed, correctly
    assert mkr.norm("text 0") == "text0"


def test_very_long_texts_bypass_the_memo():
    long_text = "a" * (mkr._NORM_MEMO_MAX_TEXT_CHARS + 1)
    assert mkr.norm(long_text) == reference_norm(long_text)
    assert long_text not in mkr._NORM_MEMO
    short = "a" * mkr._NORM_MEMO_MAX_TEXT_CHARS
    assert mkr.norm(short) == reference_norm(short)
    assert short in mkr._NORM_MEMO


def test_a_hit_does_not_recompute(monkeypatch):
    calls = []
    original = mkr._norm_uncached
    monkeypatch.setattr(mkr, "_norm_uncached", lambda t: calls.append(t) or original(t))
    mkr.norm("Some post body")
    mkr.norm("Some post body")
    mkr.norm("Some post body")
    assert calls == ["Some post body"]


# ---- non_memory_norms ------------------------------------------------------

def make_posts_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, type TEXT, content TEXT)")
    conn.executemany("INSERT INTO posts (type, content) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def reference_non_memory_norms(path):
    conn = sqlite3.connect(path)
    try:
        return {reference_norm(r[0]) for r in conn.execute(
            f"SELECT content FROM posts WHERE {mkr.non_memory_clause()}") if r[0]} - {""}
    finally:
        conn.close()


def test_non_memory_norms_matches_the_reference_and_never_goes_stale(tmp_path):
    db = tmp_path / "posts.db"
    memory_type = sorted(MEMORY_TYPES)[0]
    make_posts_db(db, [
        ("note", "First [[post]] Body"), ("diary", "Second ＰＯＳＴ"),
        (memory_type, "a memory, not counted"), (None, "typeless counts"),
        ("note", ""), ("note", None), ("note", "[[ ]]"),
    ])
    assert mkr.non_memory_norms(db) == reference_non_memory_norms(db)
    assert mkr.non_memory_norms(db) == reference_non_memory_norms(db)  # warm

    conn = sqlite3.connect(db)
    conn.execute("UPDATE posts SET content='First post body EDITED' WHERE id=1")
    conn.commit()
    got = mkr.non_memory_norms(db)
    assert got == reference_non_memory_norms(db)
    assert "firstpostbodyedited" in got and "firstpostbody" not in got

    conn.execute("DELETE FROM posts WHERE id=2")
    conn.execute("INSERT INTO posts (type, content) VALUES ('note', 'Brand new')")
    conn.commit()
    conn.close()
    got = mkr.non_memory_norms(db)
    assert got == reference_non_memory_norms(db)
    assert "secondpost" not in got and "brandnew" in got


def test_non_memory_norms_still_handles_a_missing_database(tmp_path):
    assert mkr.non_memory_norms(None) == set()
    assert mkr.non_memory_norms(tmp_path / "absent.db") == set()


def test_second_non_memory_norms_call_recomputes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "posts.db"
    make_posts_db(db, [("note", f"post number {i} body") for i in range(200)])
    mkr.non_memory_norms(db)
    calls = []
    original = mkr._norm_uncached
    monkeypatch.setattr(mkr, "_norm_uncached", lambda t: calls.append(t) or original(t))
    mkr.non_memory_norms(db)
    assert calls == []
