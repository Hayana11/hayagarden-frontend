"""chat_frequency: one window read, results identical to the per-word SQL form."""
from __future__ import annotations

import random
import sqlite3

import pytest

from tools import memory_surface as ms


def make_conn(contents, *, row_factory=None, typed=False):
    conn = sqlite3.connect(":memory:")
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content)"
                 if typed else
                 "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT)")
    conn.executemany("INSERT INTO chat_messages (author, content) VALUES ('hayana', ?)",
                     [(c,) for c in contents])
    conn.commit()
    return conn


def oracle(conn, words, *, exclude_message_id, window=ms.CHAT_DF_WINDOW):
    """The original implementation: one COUNT per word over the same window."""
    return ms._chat_frequency_sql(conn, words, (int(exclude_message_id), int(window)))


def statements(conn):
    log = []
    conn.set_trace_callback(log.append)
    return log


CONTENTS = [
    "Hello World", "hello world", "HELLO", "Mixed ÄÖÜ äöü ß ẞ", "İSTANBUL istanbul",
    "Σίσυφος ΣΊΣΥΦΟΣ σίσυφος", "中文和English混合 ABC", "한국어 テキスト", "emoji 😀 DONE",
    "é combining É", "", " ", "tab\tand\nnewline", "50% off_sale [x] (y)",
    "UPPER ONLY", "lower only", "ＦＵＬＬＷＩＤＴＨ ａｂｃ", "ﬁnance ﬂow", None, None,
    "The quick brown fox", "the QUICK BROWN fox again", "x" * 5000 + "NEEDLE",
]
WORDS = [
    "hello", "HELLO", "world", "ÄÖÜ", "äöü", "ß", "istanbul", "İstanbul", "σίσυφος",
    "ΣΊΣΥΦΟΣ", "中文", "english", "ENGLISH", "abc", "ABC", "한국어", "😀", "done",
    "é", "É", "", " ", "\t", "50%", "_sale", "[x]", "fox", "needle", "NEEDLE", "ａｂｃ",
    "absent-word", "ﬁ", "fi",
]


@pytest.mark.parametrize("row_factory", [None, sqlite3.Row])
def test_matches_the_sql_form_for_a_mixed_corpus(row_factory):
    conn = make_conn(CONTENTS, row_factory=row_factory)
    for exclude in (0, 1, 5, len(CONTENTS), len(CONTENTS) + 10):
        for window in (0, 1, 3, 7, len(CONTENTS), 4000):
            got = ms.chat_frequency(conn, WORDS, exclude_message_id=exclude, window=window)
            assert got == oracle(conn, WORDS, exclude_message_id=exclude, window=window), (exclude, window)


def test_default_window_matches():
    conn = make_conn(CONTENTS)
    assert ms.chat_frequency(conn, WORDS, exclude_message_id=3) == oracle(
        conn, WORDS, exclude_message_id=3)


def test_lower_is_ascii_only_exactly_like_sqlite():
    conn = make_conn(["İSTANBUL", "ΣΊΣΥΦΟΣ", "ÀB"])
    # SQLite's lower() leaves non-ASCII letters alone; str.lower() would not.
    got, total = ms.chat_frequency(conn, ["istanbul", "i̇stanbul", "σίσυφος", "àb", "Àb", "Àb".lower()],
                                   exclude_message_id=0)
    assert (got, total) == oracle(conn, ["istanbul", "i̇stanbul", "σίσυφος", "àb", "Àb", "Àb".lower()],
                                  exclude_message_id=0)
    assert got["istanbul"] == 0 and got["σίσυφος"] == 0 and got["àb"] == 0


def test_empty_word_matches_every_non_null_row_and_null_content_never_matches():
    conn = make_conn(["a", "", None, "b"])
    got, total = ms.chat_frequency(conn, [""], exclude_message_id=0)
    assert (got, total) == oracle(conn, [""], exclude_message_id=0) == ({"": 3}, 4)


def test_words_are_not_lowered_so_uppercase_words_only_match_non_letters():
    conn = make_conn(["Hello hello", "HELLO"])
    assert ms.chat_frequency(conn, ["Hello", "hello"], exclude_message_id=0) == (
        {"Hello": 0, "hello": 2}, 2)


def test_duplicate_words_and_no_words():
    conn = make_conn(["alpha beta", "beta"])
    assert ms.chat_frequency(conn, ["beta", "beta", "alpha"], exclude_message_id=0) == (
        {"beta": 2, "alpha": 1}, 2)
    assert ms.chat_frequency(conn, [], exclude_message_id=0) == ({}, 2)
    empty = make_conn([])
    assert ms.chat_frequency(empty, ["x"], exclude_message_id=0) == ({"x": 0}, 0)


def test_excluded_message_is_not_counted():
    conn = make_conn(["needle one", "needle two", "needle three"])
    got, total = ms.chat_frequency(conn, ["needle"], exclude_message_id=2)
    assert (got, total) == ({"needle": 2}, 2)


def test_reads_the_window_once_instead_of_once_per_word():
    conn = make_conn(CONTENTS)
    log = statements(conn)
    ms.chat_frequency(conn, WORDS[:12], exclude_message_id=3)
    selects = [s for s in log if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    log.clear()
    oracle(conn, WORDS[:12], exclude_message_id=3)
    assert len([s for s in log if s.lstrip().upper().startswith("SELECT")]) == 13


@pytest.mark.parametrize("odd", [
    ("embedded NUL \x00 byte", "nul"),
    (b"blob bytes NEEDLE", "needle"),
    (12345, "234"),
    (1.5, "1.5"),
])
def test_non_text_or_nul_content_is_answered_by_the_sql_form(odd):
    content, word = odd
    conn = make_conn(["plain needle", content], typed=True)
    got = ms.chat_frequency(conn, [word, "needle"], exclude_message_id=0)
    assert got == oracle(conn, [word, "needle"], exclude_message_id=0)


def test_word_with_nul_or_non_str_word_uses_the_sql_form():
    conn = make_conn(["abc", "a\x00b"], typed=True)
    for words in (["a\x00b"], ["abc", "a\x00"]):
        assert ms.chat_frequency(conn, words, exclude_message_id=0) == oracle(
            conn, words, exclude_message_id=0)


def test_undecodable_text_falls_back_without_raising():
    conn = make_conn(["fine needle"])
    conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', CAST(x'6e6565646c65ff' AS TEXT))")
    conn.commit()
    got = ms.chat_frequency(conn, ["needle"], exclude_message_id=0)
    assert got == oracle(conn, ["needle"], exclude_message_id=0)


def test_randomised_corpora_match_the_sql_form():
    rng = random.Random(20261111)
    alphabet = "abcXYZ 012 ，。中文한ＡＢＣﬁİßΣσ😀\t\n%_[]é́"
    for _ in range(60):
        contents = [
            None if rng.random() < 0.05 else
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            for _ in range(rng.randint(0, 80))
        ]
        words = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4)))
            for _ in range(rng.randint(0, 12))
        ]
        conn = make_conn(contents)
        exclude = rng.randint(0, len(contents) + 1)
        window = rng.choice([0, 1, 5, 40, 4000])
        assert ms.chat_frequency(conn, words, exclude_message_id=exclude, window=window) == oracle(
            conn, words, exclude_message_id=exclude, window=window)
