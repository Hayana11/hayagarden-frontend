"""Start-up warm-up of the Continuity / memory caches (continuity.prewarm)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import sqlite3
import threading

import pytest

import config_store
from chat import daily_context as dc
from chat import daily_continuity_shadow as shadow
from chat.context_window import get_current_context_window
from continuity import prewarm, sources, store
from continuity.chunk_generation import (
    GENERATOR_POLICY_VERSION,
    MEASUREMENT_SEMANTICS,
    PROMPT_POLICY_VERSION,
)
from continuity.sealing import SealingPolicy
from continuity.sources import build_source_snapshot, derive_turns_and_events
from tools import memory_kernel_read as mkr
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1

COLUMNS = (
    "id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, "
    "created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, "
    "cache_info TEXT, source_kind TEXT NOT NULL DEFAULT 'chat', attachments TEXT, "
    "image_url TEXT, file_url TEXT, file_name TEXT"
)
WAKE_INFO = {
    "wake_mode": "normal", "canonical_chat_history": True,
    "unified_chat_resident": True, "b3_authority": True,
    "source": "wake", "provider": "claude_code", "wake_run_id": "run-1",
}


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    monkeypatch.delenv("CONTINUITY_STRICT_REVALIDATE", raising=False)
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    prewarm.reset_for_tests()
    store.clear_ready_surface_cache()
    sources.clear_source_derivation_cache()
    mkr.clear_norm_memo()
    yield
    prewarm.reset_for_tests()
    store.clear_ready_surface_cache()
    sources.clear_source_derivation_cache()
    mkr.clear_norm_memo()


@pytest.fixture
def switch_db(tmp_path, monkeypatch):
    """A config DB for the CONTINUITY_PREWARM_ENABLED switch."""
    path = str(tmp_path / "config.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config_store, "DB_PATH", path)
    monkeypatch.setattr(config_store, "ENV_PATH", str(tmp_path / ".env"))
    config_store._discard_thread_connection()
    yield path
    config_store._discard_thread_connection()


def set_switch(value):
    config_store.set(prewarm.SWITCH_KEY, value)


# ---- a production-shaped database ----------------------------------------------------

def message_rows(turns=12):
    rows = []
    for turn in range(turns):
        base = turn * 2
        rows.append({"id": base + 1, "author": "hayana", "content": f"question {turn} " + "x" * 40,
                     "created_at": f"2026-09-08 10:{base % 60:02d}:00",
                     "cache_info": json.dumps({"client": "web", "usage": {"in": turn}}),
                     "source_kind": "chat"})
        rows.append({"id": base + 2, "author": "assistant", "content": f"answer {turn} " + "y" * 60,
                     "created_at": f"2026-09-08 10:{(base + 1) % 60:02d}:30",
                     "cache_info": json.dumps({"usage": {"in": 100 + turn, "out": turn}, "trace": ["a"] * 30}),
                     "tool_calls": json.dumps([{"name": "lookup", "args": {"q": turn}, "result": "ok"}]),
                     "source_kind": "chat"})
    return rows


def build_world(tmp_path, *, turns=12, blocks=4, with_window=True):
    path = str(tmp_path / "memories.db")
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE chat_messages ({COLUMNS})")
    conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, type TEXT, content TEXT, resolved INTEGER, "
                 "tags TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE wake_log (id INTEGER PRIMARY KEY, wake_run_id TEXT, content TEXT, "
                 "context_id INTEGER, context_epoch INTEGER, chat_id TEXT)")
    conn.commit()
    conn.close()
    dc.ensure_schema(path)  # daily_contexts, daily_message_contexts, ...
    identity = None
    if with_window:
        window = get_current_context_window(db_path=path)  # writable bootstrap, done once here
        identity = (int(window["id"]), int(window["context_epoch"]))
    rows = message_rows(turns)
    wake_id = rows[-1]["id"] + 1
    rows.append({"id": wake_id, "author": "assistant", "content": "a wake note", "source_kind": "wake",
                 "created_at": "2026-09-08 11:00:00", "cache_info": json.dumps(WAKE_INFO)})
    conn = sqlite3.connect(path)
    for row in rows:
        full = {"thinking": "T" * 200, "tool_calls": "[]",
                "branches": "[]", "branch_idx": 0, "attachments": "[]", "image_url": "",
                "file_url": "", "file_name": "", **row}
        names = list(full)
        conn.execute(f"INSERT INTO chat_messages ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                     [full[n] for n in names])
        if identity and full["source_kind"] != "wake":
            conn.execute(
                "INSERT INTO daily_message_contexts (message_id, context_id, context_epoch, "
                "resident_generation, role) VALUES (?,?,?,1,'user')",
                (full["id"], identity[0], identity[1]))
    if identity:
        conn.execute("INSERT INTO wake_log (wake_run_id, content, context_id, context_epoch, chat_id) "
                     "VALUES ('run-1', 'a wake note', ?, ?, 'default')", identity)
    conn.execute("INSERT INTO posts (type, content, resolved, tags, created_at) VALUES "
                 "('note', 'A plain note about gardens', 0, '', '2026-09-01 00:00:00')")
    conn.commit()
    conn.close()

    ready = 0
    if blocks:
        ready = publish_ready_chunks(path, rows, blocks)
    return path, identity, rows, ready


def publish_ready_chunks(path, rows, blocks):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    store.ensure_schema(conn)
    turns, events = derive_turns_and_events(rows)
    snapshot = build_source_snapshot(
        turns=turns, events=events, local_day="2026-09-08",
        source_watermark=rows[-1]["id"], created_at="2026-09-08T11:00:00Z")
    store.save_source_snapshot(conn, snapshot)
    policy = SealingPolicy(version="test_split_policy", target_logical_size=1, max_completed_turns=1)
    job = store.enqueue_job(conn, snapshot, policy)
    store.materialize_job(conn, job.job_id, policy)
    candidates = store.load_candidates(conn, job.job_id)[:blocks]
    published = 0
    for index, candidate in enumerate(candidates):
        generation = store.enqueue_generation_job(
            conn, candidate, snapshot, generator_policy_version=GENERATOR_POLICY_VERSION,
            prompt_policy_version=PROMPT_POLICY_VERSION, measurement_semantics=MEASUREMENT_SEMANTICS)
        conn.commit()
        claimed = store.claim_generation_job(
            conn, generation.generation_job_id, frozen_provider="claude_code",
            frozen_model_identity="model-A")
        body = f"valid chunk {index}"
        store.publish_chunk_atomic(
            conn, job=claimed, candidate=candidate, body=body,
            body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            source_token_estimate=10,
            output_token_estimate=estimate_tokens_heuristic_cjk1_ascii4_v1(body),
            provider="claude_code", model_identity="model-A", actual_executor="test")
        conn.commit()
        published += 1
    conn.close()
    sources.clear_source_derivation_cache()
    store.clear_ready_surface_cache()
    return published


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_KERNEL_DB", str(tmp_path / "absent-kernel.db"))
    return build_world(tmp_path)


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    stat = os.stat(path)
    return digest.hexdigest(), stat.st_size, stat.st_mtime_ns


def scope_rows(path, identity):
    conn = store.open_continuity_read_only(path)
    try:
        return shadow.read_canonical_scope_rows(conn, context_id=identity[0], context_epoch=identity[1])
    finally:
        conn.close()


def derived_as_data(rows):
    turns, events = derive_turns_and_events(rows)
    return json.dumps(
        [[dataclasses.asdict(t) for t in turns], [dataclasses.asdict(e) for e in events]],
        sort_keys=True, ensure_ascii=False)


# ---- the switch, and when a thread may start --------------------------------------------

class ThreadRecorder:
    def __init__(self, monkeypatch):
        self.created = []
        real = threading.Thread

        def recording(*args, **kwargs):
            thread = real(*args, **kwargs)
            self.created.append(thread)
            return thread

        monkeypatch.setattr(prewarm.threading, "Thread", recording)


def test_switch_defaults_on(switch_db):
    assert prewarm.enabled() is True
    assert config_store._DEFAULTS["CONTINUITY_PREWARM_ENABLED"] == "1"


def test_switch_off_starts_no_thread(switch_db, monkeypatch):
    set_switch("0")
    threads = ThreadRecorder(monkeypatch)
    assert prewarm.enabled() is False
    assert prewarm.start("x.db", delay_seconds=0, require_gunicorn=False) is False
    assert threads.created == []
    assert prewarm._started_pid is None


def test_switch_off_also_blocks_under_gunicorn(switch_db, monkeypatch):
    set_switch("0")
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
    threads = ThreadRecorder(monkeypatch)
    assert prewarm.start("x.db", delay_seconds=0) is False
    assert threads.created == []


def test_switch_turned_off_during_the_delay_cancels_the_run(switch_db, monkeypatch):
    calls = []
    monkeypatch.setattr(prewarm, "warm", lambda path: calls.append(path))
    set_switch("0")
    prewarm._run("x.db", 0)
    assert calls == []


def test_not_started_outside_gunicorn(switch_db, monkeypatch):
    threads = ThreadRecorder(monkeypatch)
    assert prewarm.running_under_gunicorn() is False
    assert prewarm.start("x.db", delay_seconds=0) is False
    assert threads.created == []


def test_importing_gateway_does_not_start_it(switch_db):
    import importlib
    importlib.import_module("gateway")
    assert prewarm._started_pid is None
    assert not [t for t in threading.enumerate() if t.name == prewarm.THREAD_NAME]


def test_gateway_calls_start_with_its_db_path_and_survives_a_failure():
    source = open(os.path.join(os.path.dirname(__file__), "..", "gateway.py")).read()
    block = source[source.index("from continuity import prewarm as _continuity_prewarm"):]
    assert "_continuity_prewarm.start(db_path=DB_PATH)" in block.split("if __name__")[0]
    assert "except Exception" in block.split("if __name__")[0]


def test_under_gunicorn_one_daemon_thread_runs_once_per_process(switch_db, monkeypatch):
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
    ran = []
    done = threading.Event()
    monkeypatch.setattr(prewarm, "warm", lambda path: (ran.append(path), done.set()))
    threads = ThreadRecorder(monkeypatch)
    assert prewarm.start("/db/path.db", delay_seconds=0) is True
    assert prewarm.start("/db/path.db", delay_seconds=0) is False
    assert done.wait(5)
    assert ran == ["/db/path.db"]
    assert len(threads.created) == 1
    assert threads.created[0].daemon is True and threads.created[0].name == prewarm.THREAD_NAME


def test_default_delay_keeps_clear_of_the_health_check():
    assert 15 <= prewarm.DELAY_SECONDS <= 30


def test_a_forked_worker_starts_its_own_run_only_if_one_was_requested(switch_db, monkeypatch):
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
    started = []
    monkeypatch.setattr(prewarm, "_start_thread", lambda path, delay: started.append((path, delay)) or True)
    prewarm._after_fork_in_child()
    assert started == []  # nothing requested in the parent
    monkeypatch.setattr(prewarm, "_requested_db_path", "/db/x.db")
    prewarm._after_fork_in_child()
    assert started == [("/db/x.db", prewarm.DELAY_SECONDS)]
    monkeypatch.delenv("SERVER_SOFTWARE")
    prewarm._after_fork_in_child()
    assert len(started) == 1


# ---- what a warm-up does ---------------------------------------------------------------------

def test_warm_up_makes_the_first_turn_hit_the_caches_and_changes_no_result(world, monkeypatch):
    path, identity, rows, ready = world
    assert ready == 4

    # Reference: a cold process, nothing warmed.
    cold_rows = scope_rows(path, identity)
    cold_derived = derived_as_data(cold_rows)
    cold_surface = store.read_ready_surface(path)
    assert cold_surface.status == "ready" and len(cold_surface.artifacts) == ready
    cold_ids = [(a.chunk_id, a.artifact_revision, a.body_hash) for a in cold_surface.artifacts]

    store.clear_ready_surface_cache()
    sources.clear_source_derivation_cache()
    report = prewarm.warm(path)
    assert report["failures"] == []
    assert report["ready_surface"]["blocks"] == ready
    assert report["source_rows"]["rows"] == len(rows)
    assert report["source_rows"]["turns"] == 12
    assert report["source_rows"]["events"] == 1

    computed, validated = [], []
    original_facts = sources._compute_source_row_facts
    original_validate = store._validate_ready_chunk
    monkeypatch.setattr(sources, "_compute_source_row_facts",
                        lambda row, **kw: computed.append(row["id"]) or original_facts(row, **kw))
    monkeypatch.setattr(store, "_validate_ready_chunk",
                        lambda *a, **kw: validated.append(1) or original_validate(*a, **kw))

    warm_rows = scope_rows(path, identity)
    warm_derived = derived_as_data(warm_rows)
    warm_surface = store.read_ready_surface(path)

    assert computed == [], "source rows were derived again after the warm-up"
    assert validated == [], "READY chunks were re-validated after the warm-up"
    assert warm_derived == cold_derived
    assert warm_rows == cold_rows
    assert [(a.chunk_id, a.artifact_revision, a.body_hash) for a in warm_surface.artifacts] == cold_ids
    assert warm_surface.status == cold_surface.status


def test_strict_revalidation_still_bypasses_what_was_warmed(world, monkeypatch):
    path, identity, _rows, _ready = world
    prewarm.warm(path)
    monkeypatch.setenv("CONTINUITY_STRICT_REVALIDATE", "1")
    computed = []
    original = sources._compute_source_row_facts
    monkeypatch.setattr(sources, "_compute_source_row_facts",
                        lambda row, **kw: computed.append(row["id"]) or original(row, **kw))
    derive_turns_and_events(scope_rows(path, identity))
    assert len(computed) == len(message_rows()) + 1


def test_warm_up_fills_the_memory_normalisation_memo(world):
    path, *_ = world
    assert not mkr._NORM_MEMO
    report = prewarm.warm(path)
    assert report["memory"]["non_memory_norms"] == 1
    assert mkr.norm("A plain note about gardens") == "aplainnoteaboutgardens"
    assert "A plain note about gardens" in mkr._NORM_MEMO
    assert "jieba_ms" in report["memory"]


def test_the_report_log_line_has_timings_rows_and_blocks(world, caplog):
    path, *_ = world
    with caplog.at_level(logging.INFO, logger="continuity.prewarm"):
        report = prewarm.warm(path)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[PREWARM]")]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "ready_surface[" in line and "blocks=4" in line
    assert "source_rows[" in line and f"rows={len(message_rows()) + 1}" in line and "turns=12" in line
    assert "memory[" in line and "non_memory_norms=1" in line and "total=" in line
    assert report["total_ms"] >= 0 and all(report[k]["ms"] >= 0 for k in ("ready_surface", "source_rows", "memory"))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---- strictly read-only ----------------------------------------------------------------------------

WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP", "VACUUM", "REINDEX",
                  "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE", "SAVEPOINT", "ATTACH", "ANALYZE")


class SqliteSpy:
    def __init__(self, monkeypatch):
        self.connections = []
        self.statements = []
        real = sqlite3.connect

        def spying(database, *args, **kwargs):
            conn = real(database, *args, **kwargs)
            self.connections.append((str(database), kwargs, conn))
            conn.set_trace_callback(self.statements.append)
            return conn

        monkeypatch.setattr(sqlite3, "connect", spying)


def test_no_write_of_any_kind_happens_during_the_warm_up(world, monkeypatch):
    path, *_ = world
    before = file_digest(path)
    spy = SqliteSpy(monkeypatch)
    popen_calls = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: popen_calls.append(a) or (_ for _ in ()).throw(
        AssertionError("a process was started")))
    report = prewarm.warm(path)
    assert report["failures"] == []
    assert spy.connections, "the spy saw no connection"
    for database, _kwargs, conn in spy.connections:
        assert "mode=ro" in database, f"writable connection opened: {database}"
        try:
            assert conn.total_changes == 0
        except sqlite3.ProgrammingError:
            pass  # already closed; its statements are checked below
    writes = [s for s in spy.statements if s.lstrip().upper().startswith(WRITE_PREFIXES)]
    assert writes == []
    assert popen_calls == []
    assert file_digest(path) == before, "the database file changed"


def test_an_empty_chat_is_skipped_not_bootstrapped(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MEMORY_KERNEL_DB", str(tmp_path / "absent-kernel.db"))
    path, identity, _rows, _ready = build_world(tmp_path, with_window=False, blocks=0)
    assert identity is None
    before = file_digest(path)
    conn = sqlite3.connect(path)
    contexts_before = conn.execute("SELECT COUNT(*) FROM daily_contexts").fetchone()[0]
    conn.close()
    with caplog.at_level(logging.INFO, logger="continuity.prewarm"):
        report = prewarm.warm(path)
    assert report["source_rows"].get("skipped") and report["source_rows"]["rows"] == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM daily_contexts").fetchone()[0] == contexts_before == 0
    conn.close()
    assert file_digest(path) == before


def test_no_provider_request_and_no_claude_process(world, monkeypatch):
    path, *_ = world

    def forbidden(*args, **kwargs):
        raise AssertionError("network or process use during the warm-up")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    import cc_resident
    monkeypatch.setattr(cc_resident, "get_resident", forbidden, raising=False)
    assert prewarm.warm(path)["failures"] == []


# ---- failures are swallowed ---------------------------------------------------------------------------

def test_one_failing_step_logs_one_warning_and_the_others_still_run(world, caplog, monkeypatch):
    path, *_ = world
    calls = {"ready": 0}

    def boom(_path):
        calls["ready"] += 1
        raise RuntimeError("store exploded")

    monkeypatch.setattr(prewarm, "_step_ready_surface", boom)
    with caplog.at_level(logging.INFO, logger="continuity.prewarm"):
        report = prewarm.warm(path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "store exploded" in warnings[0].getMessage()
    assert calls["ready"] == 1, "a failed step must not be retried"
    assert report["failures"] == ["ready_surface: RuntimeError: store exploded"]
    assert report["source_rows"]["rows"] == len(message_rows()) + 1  # the others ran
    assert report["memory"]["non_memory_norms"] == 1


def test_every_step_failing_still_yields_exactly_one_warning(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="continuity.prewarm"):
        report = prewarm.warm(str(tmp_path / "does-not-exist.db"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert len(report["failures"]) >= 1


def test_an_exception_in_the_thread_is_logged_once_and_never_escapes(switch_db, monkeypatch, caplog):
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
    calls = []
    escaped = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args))

    def explode(path):
        calls.append(path)
        raise ValueError("unexpected")

    monkeypatch.setattr(prewarm, "warm", explode)
    with caplog.at_level(logging.INFO, logger="continuity.prewarm"):
        assert prewarm.start("x.db", delay_seconds=0) is True
        for thread in threading.enumerate():
            if thread.name == prewarm.THREAD_NAME:
                thread.join(5)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "unexpected" in warnings[0].getMessage()
    assert calls == ["x.db"] and escaped == []
    assert prewarm.start("x.db", delay_seconds=0) is False  # not retried either
    assert calls == ["x.db"]


def test_a_missing_window_or_store_never_raises(tmp_path):
    for target in (str(tmp_path / "nothing.db"), str(tmp_path)):
        report = prewarm.warm(target)
        assert "total_ms" in report
