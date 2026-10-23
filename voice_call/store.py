"""Calls and their lines in SQLite.

A call moves ringing -> active -> ended, or ends straight from ringing as
missed / declined / canceled. Ring timeout is applied lazily whenever a call
is read, so no timer thread is needed.
"""
import contextlib
import sqlite3
import threading

RING_TIMEOUT_S = 60

RINGING = 'ringing'
ACTIVE = 'active'
ENDED = 'ended'
MISSED = 'missed'
DECLINED = 'declined'
CANCELED = 'canceled'
FINAL_STATES = (ENDED, MISSED, DECLINED, CANCELED)

_TRANSITIONS = {
    RINGING: (ACTIVE, MISSED, DECLINED, CANCELED),
    ACTIVE: (ENDED,),
}


class CallStateError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class CallStore:
    def __init__(self, db_path, clock, ring_timeout_s=RING_TIMEOUT_S):
        self._db_path = db_path
        self._clock = clock
        self._ring_timeout_s = ring_timeout_s
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS voice_calls (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    answered_at REAL,
                    ended_at REAL
                );
                CREATE TABLE IF NOT EXISTS voice_call_lines (
                    call_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    speaker TEXT NOT NULL,
                    text TEXT NOT NULL,
                    audio_key TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (call_id, seq)
                );
            """)

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, call_id):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO voice_calls (id, state, created_at) VALUES (?, ?, ?)",
                (call_id, RINGING, self._clock()),
            )
        return self.get(call_id)

    def get(self, call_id):
        with self._lock, self._connect() as conn:
            self._expire(conn)
            row = conn.execute("SELECT * FROM voice_calls WHERE id=?", (call_id,)).fetchone()
        return dict(row) if row else None

    def latest_open(self):
        with self._lock, self._connect() as conn:
            self._expire(conn)
            row = conn.execute(
                "SELECT * FROM voice_calls WHERE state IN (?, ?) ORDER BY created_at DESC LIMIT 1",
                (RINGING, ACTIVE),
            ).fetchone()
        return dict(row) if row else None

    def transition(self, call_id, new_state):
        now = self._clock()
        with self._lock, self._connect() as conn:
            self._expire(conn)
            row = conn.execute("SELECT state FROM voice_calls WHERE id=?", (call_id,)).fetchone()
            if not row:
                raise CallStateError('no such call', 404)
            if new_state not in _TRANSITIONS.get(row['state'], ()):
                raise CallStateError('call is %s, cannot become %s' % (row['state'], new_state))
            if new_state == ACTIVE:
                conn.execute("UPDATE voice_calls SET state=?, answered_at=? WHERE id=?",
                             (new_state, now, call_id))
            else:
                conn.execute("UPDATE voice_calls SET state=?, ended_at=? WHERE id=?",
                             (new_state, now, call_id))
        return self.get(call_id)

    def add_line(self, call_id, speaker, text):
        with self._lock, self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM voice_call_lines WHERE call_id=?",
                (call_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO voice_call_lines (call_id, seq, speaker, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (call_id, seq, speaker, text, self._clock()),
            )
        return {'seq': seq, 'speaker': speaker, 'text': text}

    def set_audio_key(self, call_id, seq, audio_key):
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE voice_call_lines SET audio_key=? WHERE call_id=? AND seq=?",
                         (audio_key, call_id, seq))

    def line(self, call_id, seq):
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM voice_call_lines WHERE call_id=? AND seq=?",
                               (call_id, seq)).fetchone()
        return dict(row) if row else None

    def lines_after(self, call_id, after_seq):
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, speaker, text, audio_key FROM voice_call_lines "
                "WHERE call_id=? AND seq>? ORDER BY seq",
                (call_id, after_seq),
            ).fetchall()
        return [dict(row) for row in rows]

    def _expire(self, conn):
        conn.execute(
            "UPDATE voice_calls SET state=?, ended_at=created_at+? WHERE state=? AND created_at<=?",
            (MISSED, self._ring_timeout_s, RINGING, self._clock() - self._ring_timeout_s),
        )
