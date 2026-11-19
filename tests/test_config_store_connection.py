"""config_store.get: reused per-thread connection and cached .env fallback.

Neither may change what get() returns: every call still queries the DB, a
committed write is visible on the very next call, and the .env file is re-read
whenever it changes.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import config_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = str(tmp_path / "config.db")
    env = str(tmp_path / ".env")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config_store, "DB_PATH", db)
    monkeypatch.setattr(config_store, "ENV_PATH", env)
    monkeypatch.setattr(config_store, "_ENV_RACY_NS", 0)
    monkeypatch.setattr(config_store, "_env_cache", None)
    config_store._discard_thread_connection()
    yield SimpleStore(db, env)
    config_store._discard_thread_connection()


class SimpleStore:
    def __init__(self, db, env):
        self.db, self.env = db, env

    def write(self, key, value):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO runtime_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()
        conn.close()

    def remove(self, key):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM runtime_config WHERE key=?", (key,))
        conn.commit()
        conn.close()

    def write_env(self, text):
        with open(self.env, "w") as handle:
            handle.write(text)
        time.sleep(0.01)


# ---- the implementation as it was before this change -------------------------

def reference_read_env_fallback(key):
    env_key = config_store._ENV_FALLBACK_KEYS.get(key)
    if not env_key:
        return None
    result = None
    try:
        for ln in open(config_store.ENV_PATH):
            ln = ln.strip()
            if ln.startswith(env_key + "="):
                result = ln.split("=", 1)[1].strip()
    except Exception:
        pass
    return result


def reference_get(key, default=None):
    try:
        conn = sqlite3.connect(config_store.DB_PATH, timeout=3)
        row = conn.execute("SELECT value FROM runtime_config WHERE key=?", (key,)).fetchone()
        conn.close()
        if row is not None:
            return row[0]
    except Exception:
        pass
    env_val = reference_read_env_fallback(key)
    if env_val:
        return env_val
    if default is not None:
        return default
    return config_store._DEFAULTS.get(key, "")


def count_connects(monkeypatch):
    counter = {"n": 0}
    real = sqlite3.connect

    def counted(*args, **kwargs):
        counter["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(config_store.sqlite3, "connect", counted)
    return counter


# ---- connection reuse ----------------------------------------------------------

def test_many_reads_open_one_connection_per_thread(store, monkeypatch):
    store.write("MODEL", "m1")
    counter = count_connects(monkeypatch)
    for _ in range(50):
        assert config_store.get("MODEL") == "m1"
    assert counter["n"] == 1


def test_each_thread_gets_its_own_connection_and_never_shares_one(store, monkeypatch):
    store.write("MODEL", "m1")
    counter = count_connects(monkeypatch)
    errors, results = [], []

    def worker():
        try:
            for _ in range(20):
                results.append(config_store.get("MODEL"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results == ["m1"] * 80
    assert counter["n"] == 4


def test_changing_db_path_switches_connection(store, tmp_path, monkeypatch):
    store.write("MODEL", "first")
    assert config_store.get("MODEL") == "first"
    other = str(tmp_path / "other.db")
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at)")
    conn.execute("INSERT INTO runtime_config (key, value) VALUES ('MODEL', 'second')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config_store, "DB_PATH", other)
    assert config_store.get("MODEL") == "second"


def test_replaced_database_file_is_followed(store, tmp_path):
    store.write("MODEL", "old file")
    assert config_store.get("MODEL") == "old file"
    replacement = str(tmp_path / "replacement.db")
    conn = sqlite3.connect(replacement)
    conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at)")
    conn.execute("INSERT INTO runtime_config (key, value) VALUES ('MODEL', 'new file')")
    conn.commit()
    conn.close()
    os.replace(replacement, store.db)
    assert config_store.get("MODEL") == "new file"


def test_deleted_database_falls_back_exactly_like_before(store, tmp_path):
    store.write("MODEL", "present")
    assert config_store.get("MODEL") == "present"
    os.remove(store.db)
    # Before: connect() recreated an empty file, the SELECT failed, defaults won.
    assert config_store.get("MODEL", "dflt") == reference_get("MODEL", "dflt") == "dflt"


def test_forked_child_state_is_never_closed_or_reused(store, monkeypatch):
    store.write("MODEL", "m")
    assert config_store.get("MODEL") == "m"
    parent_conn = config_store._thread_state.held[1]
    real_pid = os.getpid()
    monkeypatch.setattr(config_store.os, "getpid", lambda: real_pid + 1)
    assert config_store.get("MODEL") == "m"
    assert config_store._thread_state.held[1] is not parent_conn
    assert parent_conn in config_store._forked_orphans
    parent_conn.execute("SELECT 1").fetchall()  # still open: not closed on discard
    monkeypatch.setattr(config_store.os, "getpid", lambda: real_pid)
    config_store._forked_orphans.remove(parent_conn)


def test_connection_is_query_only(store):
    store.write("MODEL", "m")
    config_store.get("MODEL")
    conn = config_store._thread_state.held[1]
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO runtime_config (key, value) VALUES ('X', 'y')")
    assert config_store.get("MODEL") == "m"


# ---- every call still reflects the DB ---------------------------------------------

@pytest.mark.parametrize("wal", [False, True], ids=["rollback-journal", "wal"])
def test_a_committed_write_is_visible_on_the_very_next_get(store, wal):
    if wal:
        conn = sqlite3.connect(store.db)
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.close()
    assert config_store.get("MODEL", "none") == "none"
    for index in range(30):
        store.write("MODEL", f"v{index}")
        assert config_store.get("MODEL") == f"v{index}"
    store.remove("MODEL")
    assert config_store.get("MODEL", "gone") == "gone"


def test_set_delete_and_mutate_through_config_store_are_visible_immediately(store):
    assert config_store.get("CC_CHAT_MODEL") == ""
    config_store.set("CC_CHAT_MODEL", "opus")
    assert config_store.get("CC_CHAT_MODEL") == "opus"
    config_store.mutate("CC_CHAT_MODEL", "", lambda current: current + "-x")
    assert config_store.get("CC_CHAT_MODEL") == "opus-x"
    config_store.delete("CC_CHAT_MODEL")
    assert config_store.get("CC_CHAT_MODEL") == ""
    config_store.set("DESIRE_DRIVEN", "1")
    assert config_store.get_bool("DESIRE_DRIVEN") is True
    config_store.set("CC_COLD_REBUILD_GUARD", "123")
    assert config_store.get_int("CC_COLD_REBUILD_GUARD") == 123


def test_write_from_another_process_is_visible_immediately(store):
    assert config_store.get("MODEL", "none") == "none"
    code = (
        "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
        "c.execute(\"INSERT INTO runtime_config (key,value) VALUES ('MODEL',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value\",(sys.argv[2],));c.commit()"
    )
    for value in ("from-child-1", "from-child-2"):
        subprocess.run([sys.executable, "-c", code, store.db, value], check=True)
        assert config_store.get("MODEL") == value


def test_write_from_another_thread_is_visible_immediately(store):
    assert config_store.get("MODEL", "none") == "none"
    writer = threading.Thread(target=lambda: store.write("MODEL", "from-thread"))
    writer.start()
    writer.join()
    assert config_store.get("MODEL") == "from-thread"


def test_reads_do_not_pin_a_wal_snapshot_so_checkpoint_can_truncate(store):
    conn = sqlite3.connect(store.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    store.write("MODEL", "m")
    assert config_store.get("MODEL") == "m"
    writer = sqlite3.connect(store.db)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    store.write("OTHER", "x")
    busy, _log, _checkpointed = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    writer.close()
    assert busy == 0  # an open read transaction on the config connection would block this


def test_no_transaction_is_left_open_after_a_read(store):
    store.write("MODEL", "m")
    config_store.get("MODEL")
    assert config_store._thread_state.held[1].in_transaction is False


def test_lock_timeout_falls_back_like_before_without_a_second_wait(store, monkeypatch):
    store.write("MODEL", "in-db")
    assert config_store.get("MODEL") == "in-db"
    monkeypatch.setattr(config_store, "_CONNECT_TIMEOUT", 0.2)
    config_store._discard_thread_connection()
    blocker = sqlite3.connect(store.db, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.perf_counter()
        assert config_store.get("MODEL", "dflt") == "dflt"
        elapsed = time.perf_counter() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert 0.15 <= elapsed < 0.35  # waited out one timeout, not two
    assert config_store.get("MODEL") == "in-db"  # recovered once the lock is gone


def test_broken_table_falls_back_and_recovers(store):
    store.write("MODEL", "in-db")
    assert config_store.get("MODEL") == "in-db"
    conn = sqlite3.connect(store.db)
    conn.execute("ALTER TABLE runtime_config RENAME TO runtime_config_moved")
    conn.commit()
    assert config_store.get("MODEL", "dflt") == reference_get("MODEL", "dflt") == "dflt"
    conn.execute("ALTER TABLE runtime_config_moved RENAME TO runtime_config")
    conn.commit()
    conn.close()
    assert config_store.get("MODEL") == "in-db"


def test_get_matches_the_pre_change_implementation_over_random_histories(store):
    import random
    rng = random.Random(20261111)
    keys = ["MODEL", "WS_MODEL", "DESIRE_DRIVEN", "CC_CHAT_MODEL", "NOT_A_KEY", "CODEX_CHAT_MODEL"]
    store.write_env("MODEL=env-model\nWS_MODEL = spaced\nDESIRE_DRIVEN=1\n")
    for _ in range(300):
        action = rng.choice(["write", "remove", "env", "get", "get", "get"])
        key = rng.choice(keys)
        if action == "write":
            store.write(key, rng.choice(["a", "", "0", "1", "x y"]))
        elif action == "remove":
            store.remove(key)
        elif action == "env":
            store.write_env("".join(
                f"{rng.choice(keys)}={rng.choice(['e1', '', ' e2 ', 'e=3'])}\n"
                for _ in range(rng.randint(0, 5))))
        default = rng.choice([None, "", "dflt"])
        assert config_store.get(key, default) == reference_get(key, default), (action, key)


# ---- .env fallback cache -----------------------------------------------------------

def test_env_file_is_parsed_once_while_unchanged(store, monkeypatch):
    store.write_env("MODEL=from-env\n")
    opens = {"n": 0}
    real_open = open

    def counting_open(path, *args, **kwargs):
        if str(path) == store.env:
            opens["n"] += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    for _ in range(20):
        assert config_store.get("MODEL") == "from-env"
    assert opens["n"] == 1


@pytest.mark.parametrize("edit", [
    lambda s: s.write_env("MODEL=changed-env\n"),
    lambda s: s.write_env("MODEL=from-env\nMODEL=last-wins\n"),
    lambda s: s.write_env("# MODEL=commented\nMODEL=after-comment\n"),
    lambda s: open(s.env, "a").write("MODEL=appended\n"),
    lambda s: os.remove(s.env),
])
def test_env_edits_are_visible_on_the_next_get(store, edit):
    store.write_env("MODEL=from-env\n")
    assert config_store.get("MODEL") == "from-env"
    time.sleep(0.01)
    edit(store)
    assert config_store.get("MODEL") == reference_get("MODEL")


def test_env_rewrite_with_same_size_and_forged_mtime_is_caught_by_ctime(store):
    store.write_env("MODEL=aaaa\n")
    assert config_store.get("MODEL") == "aaaa"
    stat = os.stat(store.env)
    time.sleep(0.01)
    with open(store.env, "w") as handle:
        handle.write("MODEL=bbbb\n")
    os.utime(store.env, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert os.stat(store.env).st_size == stat.st_size
    assert os.stat(store.env).st_mtime_ns == stat.st_mtime_ns
    assert config_store.get("MODEL") == "bbbb"


def test_recently_modified_env_is_not_cached(store, monkeypatch):
    monkeypatch.setattr(config_store, "_ENV_RACY_NS", 3_600 * 10**9)
    store.write_env("MODEL=v1\n")
    opens = {"n": 0}
    real_open = open

    def counting_open(path, *args, **kwargs):
        if str(path) == store.env:
            opens["n"] += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    assert config_store.get("MODEL") == "v1"
    assert config_store.get("MODEL") == "v1"
    assert opens["n"] == 2


def test_env_path_change_is_followed(store, tmp_path, monkeypatch):
    store.write_env("MODEL=one\n")
    assert config_store.get("MODEL") == "one"
    other = tmp_path / "other.env"
    other.write_text("MODEL=two\n")
    time.sleep(0.01)
    monkeypatch.setattr(config_store, "ENV_PATH", str(other))
    assert config_store.get("MODEL") == "two"


ENV_CASES = [
    "", "\n\n", "MODEL=a", "MODEL=a\n", "  MODEL=a  \n", "MODEL = a\n", "MODEL=a=b\n",
    "MODEL==x\n", "MODEL=\n", "MODEL=a\nMODEL=\n", "MODEL=a\nMODEL=b\nMODEL=c\n",
    "WAKE_PROVIDER=w\nWAKE_PROVIDER_X=no\n", "# MODEL=c\n", "export MODEL=a\n",
    "MODEL=a\r\nWS_MODEL=b\r\n", "﻿MODEL=bom\nWS_MODEL=ok\n", "MODEL=中文\n",
    "WS_MODEL=ok\nMODEL=\xe9\n", "no equals line\nMODEL=a\n",
]


@pytest.mark.parametrize("text", ENV_CASES, ids=lambda t: repr(t)[:24])
def test_env_parsing_matches_the_per_key_scan(store, text):
    store.write_env(text)
    for key in sorted(config_store._ENV_FALLBACK_KEYS):
        assert config_store._read_env_fallback(key) == reference_read_env_fallback(key), key
    assert config_store._read_env_fallback("NOT_A_FALLBACK_KEY") is None


def test_partially_unreadable_env_yields_what_was_read_before_the_error(store):
    with open(store.env, "wb") as handle:
        handle.write(b"WS_MODEL=before\nMODEL=ok\n\xff\xfe broken line\nWAKE_PROVIDER=after\n")
    time.sleep(0.01)
    for key in ("WS_MODEL", "MODEL", "WAKE_PROVIDER"):
        assert config_store._read_env_fallback(key) == reference_read_env_fallback(key), key
    # not cached when incomplete: still re-read, still the same answer
    assert config_store._env_cache is None


def test_db_value_still_beats_env_and_env_beats_default(store):
    store.write_env("MODEL=env\n")
    assert config_store.get("MODEL", "dflt") == "env"
    store.write("MODEL", "db")
    assert config_store.get("MODEL", "dflt") == "db"
    store.remove("MODEL")
    assert config_store.get("MODEL", "dflt") == "env"
    store.write_env("")
    assert config_store.get("MODEL", "dflt") == "dflt"
    assert config_store.get("MODEL") == ""
