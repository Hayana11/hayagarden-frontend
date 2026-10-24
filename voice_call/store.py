"""Calls and their lines in SQLite.

A call moves ringing -> active -> ended, or ends straight from ringing as
missed / declined / canceled. Ring timeout is applied lazily whenever a call
is read, so no timer thread is needed.
"""
import contextlib
import json
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

HER = 'her'          # she dialed (or the page's test ring)
FYODOR = 'fyodor'    # he called her from a Wake (see CallService.ring_from_him)

_TRANSITIONS = {
    RINGING: (ACTIVE, MISSED, DECLINED, CANCELED),
    ACTIVE: (ENDED,),
}


class CallStateError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


_CALL_COLUMNS = 'id, state, created_at, answered_at, ended_at, language, language_spec, caller'


def _load_spec(raw):
    try:
        spec = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return spec if isinstance(spec, dict) else None


def _call(row):
    """A call as the API shows it: the private language snapshot reduced to its speech locale."""
    if row is None:
        return None
    call = dict(row)
    spec = _load_spec(call.pop('language_spec', None))
    call['speech'] = spec.get('speech') if spec else None
    return call


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
            # At most one ringing/active call, enforced by the database itself
            # (every open row indexes the same value, so a second one violates it).
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS voice_calls_one_open "
                    "ON voice_calls((state IN ('ringing','active'))) "
                    "WHERE state IN ('ringing','active')"
                )
            except sqlite3.IntegrityError:
                pass  # pre-existing duplicate open calls; create_if_none_open still serializes
            cols = {row[1] for row in conn.execute('PRAGMA table_info(voice_calls)')}
            if 'recorded' not in cols:
                # 1 once the call's chat record is confirmed written (set after, never before).
                conn.execute("ALTER TABLE voice_calls ADD COLUMN recorded INTEGER NOT NULL DEFAULT 0")
            if 'language' not in cols:
                # The call's language code (see voice_call.languages); NULL is the default call.
                conn.execute("ALTER TABLE voice_calls ADD COLUMN language TEXT")
            if 'caller' not in cols:
                # Who placed the call: 'her' or 'fyodor'. Only his calls are pushed as incoming.
                conn.execute("ALTER TABLE voice_calls ADD COLUMN caller TEXT NOT NULL DEFAULT 'her'")
            if 'opening' not in cols:
                # His first line for a call he placed; written as a line only when she answers.
                conn.execute("ALTER TABLE voice_calls ADD COLUMN opening TEXT")
            line_cols = {row[1] for row in conn.execute('PRAGMA table_info(voice_call_lines)')}
            if 'gloss' not in line_cols:
                # A bilingual call's subtitle for his spoken line (shown, never voiced).
                conn.execute("ALTER TABLE voice_call_lines ADD COLUMN gloss TEXT")
            if 'language_spec' not in cols:
                # That language's config as it was at ring (JSON, holds the voice id: never served),
                # so a runtime-config edit only changes the next call.
                conn.execute("ALTER TABLE voice_calls ADD COLUMN language_spec TEXT")

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_if_none_open(self, call_id, language=None, spec=None, caller=HER, opening=None,
                            caller_cooldown_s=None):
        """Atomically: expire stale rings, refuse if a call is open, else create one ringing.

        With ``caller_cooldown_s`` the same transaction also refuses (429) while the
        caller's previous call was placed less than that long ago, answered or not,
        so two concurrent Wakes cannot both pass.
        """
        with self._lock:
            conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    self._expire(conn)
                    if conn.execute(
                        "SELECT 1 FROM voice_calls WHERE state IN (?, ?) LIMIT 1", (RINGING, ACTIVE)
                    ).fetchone():
                        raise CallStateError('a call is already ringing or active')
                    if caller_cooldown_s:
                        last = conn.execute(
                            "SELECT MAX(created_at) FROM voice_calls WHERE caller=?", (caller,)
                        ).fetchone()[0]
                        if last is not None and self._clock() - last < caller_cooldown_s:
                            raise CallStateError('called too recently', 429)
                    conn.execute(
                        "INSERT INTO voice_calls (id, state, created_at, language, language_spec, caller, opening) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (call_id, RINGING, self._clock(), language,
                         json.dumps(spec, ensure_ascii=False) if spec else None, caller, opening),
                    )
                    conn.execute('COMMIT')
                except sqlite3.IntegrityError:
                    conn.execute('ROLLBACK')
                    raise CallStateError('a call is already ringing or active')
                except BaseException:
                    conn.execute('ROLLBACK')
                    raise
            finally:
                conn.close()
        return self.get(call_id)

    def get(self, call_id):
        with self._lock, self._connect() as conn:
            self._expire(conn)
            row = conn.execute("SELECT %s FROM voice_calls WHERE id=?" % _CALL_COLUMNS, (call_id,)).fetchone()
        return _call(row)

    def language_spec(self, call_id):
        """The call's frozen language config (with its voice id), or None for the default call."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT language_spec FROM voice_calls WHERE id=?", (call_id,)).fetchone()
        return _load_spec(row[0]) if row else None

    def latest_open(self):
        with self._lock, self._connect() as conn:
            self._expire(conn)
            row = conn.execute(
                "SELECT %s FROM voice_calls WHERE state IN (?, ?) ORDER BY created_at DESC LIMIT 1" % _CALL_COLUMNS,
                (RINGING, ACTIVE),
            ).fetchone()
        return _call(row)

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
                # His opening becomes his first line in the same transaction that answers:
                # ringing -> active happens once, so it is said exactly once.
                opening = conn.execute(
                    "SELECT opening FROM voice_calls WHERE id=? AND caller=?", (call_id, FYODOR)
                ).fetchone()
                if opening and opening[0]:
                    seq = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM voice_call_lines WHERE call_id=?", (call_id,)
                    ).fetchone()[0]
                    conn.execute(
                        "INSERT INTO voice_call_lines (call_id, seq, speaker, text, created_at) "
                        "VALUES (?, ?, 'him', ?, ?)", (call_id, seq, opening[0], now))
            else:
                conn.execute("UPDATE voice_calls SET state=?, ended_at=? WHERE id=?",
                             (new_state, now, call_id))
        return self.get(call_id)

    def opening(self, call_id):
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT opening FROM voice_calls WHERE id=?", (call_id,)).fetchone()
        return row[0] if row else None

    def incoming_ringing_id(self):
        """The id of a call he placed that is still ringing, or None. Her own dialing never counts."""
        with self._lock, self._connect() as conn:
            self._expire(conn)
            row = conn.execute(
                "SELECT id FROM voice_calls WHERE caller=? AND state=? ORDER BY created_at DESC LIMIT 1",
                (FYODOR, RINGING),
            ).fetchone()
        return row[0] if row else None

    def unrecorded_final(self):
        """Finished calls whose chat record has not been confirmed written yet (no claim)."""
        marks = ','.join('?' for _ in FINAL_STATES)
        with self._lock, self._connect() as conn:
            self._expire(conn)
            rows = conn.execute(
                "SELECT %s FROM voice_calls WHERE recorded=0 AND state IN (%s) ORDER BY created_at"
                % (_CALL_COLUMNS, marks),
                FINAL_STATES,
            ).fetchall()
        return [_call(row) for row in rows]

    def mark_recorded(self, call_id):
        """Only after the chat record for call_id is durably written."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE voice_calls SET recorded=1 WHERE id=?", (call_id,))

    def add_line(self, call_id, speaker, text, gloss=None):
        with self._lock, self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM voice_call_lines WHERE call_id=?",
                (call_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO voice_call_lines (call_id, seq, speaker, text, gloss, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (call_id, seq, speaker, text, gloss, self._clock()),
            )
        line = {'seq': seq, 'speaker': speaker, 'text': text}
        if gloss:
            line['gloss'] = gloss
        return line

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
                "SELECT seq, speaker, text, audio_key, gloss FROM voice_call_lines "
                "WHERE call_id=? AND seq>? ORDER BY seq",
                (call_id, after_seq),
            ).fetchall()
        lines = []
        for row in rows:
            line = dict(row)
            if not line.get('gloss'):
                line.pop('gloss', None)  # only bilingual lines carry one
            lines.append(line)
        return lines

    def _expire(self, conn):
        conn.execute(
            "UPDATE voice_calls SET state=?, ended_at=created_at+? WHERE state=? AND created_at<=?",
            (MISSED, self._ring_timeout_s, RINGING, self._clock() - self._ring_timeout_s),
        )
