"""Period tracker data semantics, cycle derivation, and legacy-table compat.

period_days is the detailed source of truth. period_records remains a
compatibility surface for the old calendar and chat tools that still read
type='period' as cycle starts (and write type='start'/'end').
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

YMD_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CYCLE_MIN, CYCLE_MAX = 18, 45
DEFAULT_CYCLE = 28
DEFAULT_PERIOD_LEN = 5
PERIOD_LEN_MIN, PERIOD_LEN_MAX = 2, 10
BLEEDING_RECORD_TYPES = frozenset({'period', 'start'})
PRE_PERIOD_WINDOW = 5

ALLOWED_DAY_KEYS = frozenset({'came', 'flow', 'pain', 'states', 'extras', 'sex', 'note'})


def is_valid_ymd(value: str | None) -> bool:
    if not value or not isinstance(value, str) or not YMD_RE.match(value):
        return False
    try:
        datetime.strptime(value, '%Y-%m-%d')
        return True
    except ValueError:
        return False


def parse_ymd(value: str) -> date | None:
    if not is_valid_ymd(value):
        return None
    return datetime.strptime(value, '%Y-%m-%d').date()


def add_days(ymd: str, n: int) -> str:
    d = parse_ymd(ymd)
    if d is None:
        raise ValueError(f'invalid date: {ymd!r}')
    return (d + timedelta(days=n)).strftime('%Y-%m-%d')


def diff_days(a: str, b: str) -> int:
    da, db = parse_ymd(a), parse_ymd(b)
    if da is None or db is None:
        raise ValueError(f'invalid date(s): {a!r}, {b!r}')
    return (da - db).days


def ensure_period_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS period_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        type TEXT NOT NULL,
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','+8 hours'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS period_days (
        date TEXT PRIMARY KEY,
        data TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT DEFAULT (datetime('now','+8 hours'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS period_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS period_compat_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")


def install_compat_triggers(conn) -> None:
    """When chat tools insert type='start', also mirror a type='period' start marker.

    Chat reminders still query WHERE type='period' and must not wait for an API
    round-trip. Rebuild later collapses consecutive period markers to group starts.
    """
    conn.execute("DROP TRIGGER IF EXISTS period_start_mirrors_period")
    conn.execute("""
        CREATE TRIGGER period_start_mirrors_period
        AFTER INSERT ON period_records
        WHEN NEW.type = 'start'
        BEGIN
            INSERT INTO period_records (date, type, note)
            SELECT NEW.date, 'period', COALESCE(NEW.note, '')
            WHERE NOT EXISTS (
                SELECT 1 FROM period_records
                WHERE date = NEW.date AND type = 'period'
            );
        END
    """)


def _load_day_data(raw) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_bleeding_dates(conn) -> list[str]:
    """Unique sorted bleeding dates from period_days + legacy period/start rows.

    period_days is authoritative when present: came=false excludes the date even
    if a legacy period/start row still exists (until rebuild removes the marker).
    """
    dates: set[str] = set()
    explicit_false: set[str] = set()
    for row in conn.execute("SELECT date, data FROM period_days").fetchall():
        d = row['date'] if hasattr(row, 'keys') else row[0]
        raw = row['data'] if hasattr(row, 'keys') else row[1]
        if not is_valid_ymd(d):
            log.warning('skipping dirty period_days date: %r', d)
            continue
        came = _load_day_data(raw).get('came')
        if came is True:
            dates.add(d)
        elif came is False:
            explicit_false.add(d)

    for row in conn.execute(
        "SELECT date, type FROM period_records WHERE type IN ('period', 'start')"
    ).fetchall():
        d = row['date'] if hasattr(row, 'keys') else row[0]
        if not is_valid_ymd(d):
            log.warning('skipping dirty period_records date: %r', d)
            continue
        if d in explicit_false:
            continue
        dates.add(d)

    return sorted(dates)


def group_consecutive_dates(dates: list[str]) -> list[tuple[str, str]]:
    """Merge consecutive YYYY-MM-DD values into (start, end) runs."""
    groups: list[tuple[str, str]] = []
    for d in dates:
        if not is_valid_ymd(d):
            log.warning('skipping invalid date in grouping: %r', d)
            continue
        if groups and diff_days(d, groups[-1][1]) <= 1:
            start, _ = groups[-1]
            groups[-1] = (start, d)
        else:
            groups.append((d, d))
    return groups


def cycle_starts(groups: list[tuple[str, str]]) -> list[str]:
    return [start for start, _ in groups]


def average_cycle_length(starts: list[str], default: int = DEFAULT_CYCLE) -> int:
    if len(starts) < 2:
        return default
    diffs: list[int] = []
    for i in range(1, len(starts)):
        try:
            gap = diff_days(starts[i], starts[i - 1])
        except ValueError:
            continue
        if CYCLE_MIN <= gap <= CYCLE_MAX:
            diffs.append(gap)
    if not diffs:
        return default
    return int(round(sum(diffs) / len(diffs)))


def average_period_length(groups: list[tuple[str, str]], default: int = DEFAULT_PERIOD_LEN) -> int:
    lengths: list[int] = []
    for start, end in groups:
        try:
            length = diff_days(end, start) + 1
        except ValueError:
            continue
        if PERIOD_LEN_MIN <= length <= PERIOD_LEN_MAX:
            lengths.append(length)
    if not lengths:
        return default
    return int(round(sum(lengths) / len(lengths)))


def compute_stats_from_starts(
    starts: list[str],
    *,
    period_length: int | None = None,
) -> dict:
    if not starts:
        return {
            'last_period': None,
            'cycle_length': None,
            'period_length': period_length,
            'next_period': None,
            'ovulation': None,
            'starts': [],
        }
    cycle_length = average_cycle_length(starts)
    last = starts[-1]
    try:
        next_period = add_days(last, cycle_length)
        ovulation = add_days(next_period, -14)
    except ValueError:
        return {
            'last_period': None,
            'cycle_length': cycle_length,
            'period_length': period_length,
            'next_period': None,
            'ovulation': None,
            'starts': starts,
        }
    return {
        'last_period': last,
        'cycle_length': cycle_length,
        'period_length': period_length,
        'next_period': next_period,
        'ovulation': ovulation,
        'starts': starts,
    }


def derive_cycle_stats(conn) -> dict:
    bleeding = collect_bleeding_dates(conn)
    groups = group_consecutive_dates(bleeding)
    starts = cycle_starts(groups)
    period_len = average_period_length(groups)
    return compute_stats_from_starts(starts, period_length=period_len)


def _upsert_came_true(conn, date_str: str) -> None:
    row = conn.execute("SELECT data FROM period_days WHERE date=?", (date_str,)).fetchone()
    if row:
        raw = row['data'] if hasattr(row, 'keys') else row[0]
        data = _load_day_data(raw)
        if data.get('came') is True:
            return
        data['came'] = True
        conn.execute(
            """UPDATE period_days SET data=?, updated_at=datetime('now','+8 hours')
               WHERE date=?""",
            (json.dumps(data, ensure_ascii=False), date_str),
        )
    else:
        conn.execute(
            """INSERT INTO period_days (date, data, updated_at)
               VALUES (?, ?, datetime('now','+8 hours'))""",
            (date_str, json.dumps({'came': True}, ensure_ascii=False)),
        )


def _period_note_for_date(conn, date_str: str) -> str:
    rows = conn.execute(
        "SELECT note FROM period_records WHERE date=? AND type IN ('period','start') ORDER BY id",
        (date_str,),
    ).fetchall()
    notes = []
    for row in rows:
        note = row['note'] if hasattr(row, 'keys') else row[0]
        if note and str(note).strip():
            notes.append(str(note).strip())
    # Prefer the first non-empty note; keep it stable across rebuilds.
    return notes[0] if notes else ''


def rebuild_period_start_markers(conn) -> dict:
    """Idempotent: period_records.type='period' becomes one row per cycle start.

    Steps:
      1. Map bleeding dates (period_days.came + period/start rows) into period_days.
      2. Group consecutive bleeding days; keep only each group's first day as type='period'.
      3. Preserve sex/end/start rows; never wipe the tables.
    """
    bleeding = collect_bleeding_dates(conn)
    for d in bleeding:
        _upsert_came_true(conn, d)

    bleeding = collect_bleeding_dates(conn)
    groups = group_consecutive_dates(bleeding)
    starts = set(cycle_starts(groups))

    existing = conn.execute(
        "SELECT id, date, note FROM period_records WHERE type='period' ORDER BY id"
    ).fetchall()

    keep_ids: set[int] = set()
    seen_starts: set[str] = set()
    for row in existing:
        rid = row['id'] if hasattr(row, 'keys') else row[0]
        d = row['date'] if hasattr(row, 'keys') else row[1]
        if d in starts and d not in seen_starts:
            keep_ids.add(rid)
            seen_starts.add(d)
        # duplicate / non-start period rows are removed below

    for row in existing:
        rid = row['id'] if hasattr(row, 'keys') else row[0]
        if rid not in keep_ids:
            conn.execute("DELETE FROM period_records WHERE id=?", (rid,))

    for start in sorted(starts):
        if start in seen_starts:
            continue
        note = _period_note_for_date(conn, start)
        conn.execute(
            "INSERT INTO period_records (date, type, note) VALUES (?, 'period', ?)",
            (start, note),
        )

    # Deduplicate sex rows per date (keep lowest id).
    sex_rows = conn.execute(
        "SELECT id, date FROM period_records WHERE type='sex' ORDER BY id"
    ).fetchall()
    seen_sex: set[str] = set()
    for row in sex_rows:
        rid = row['id'] if hasattr(row, 'keys') else row[0]
        d = row['date'] if hasattr(row, 'keys') else row[1]
        if d in seen_sex:
            conn.execute("DELETE FROM period_records WHERE id=?", (rid,))
        else:
            seen_sex.add(d)

    return {
        'bleeding_days': len(bleeding),
        'groups': len(groups),
        'starts': sorted(starts),
    }


def migrate_period_compat(conn) -> dict:
    """Run table ensure + trigger install + marker rebuild inside the caller's txn."""
    ensure_period_tables(conn)
    install_compat_triggers(conn)
    result = rebuild_period_start_markers(conn)
    conn.execute(
        """INSERT INTO period_compat_meta (key, value) VALUES ('last_migrate', ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),),
    )
    return result


def sync_sex_record(conn, date_str: str, present: bool) -> None:
    exists = conn.execute(
        "SELECT id FROM period_records WHERE date=? AND type='sex'", (date_str,)
    ).fetchone()
    if present and not exists:
        conn.execute("INSERT INTO period_records (date, type) VALUES (?, 'sex')", (date_str,))
    elif not present and exists:
        conn.execute("DELETE FROM period_records WHERE date=? AND type='sex'", (date_str,))


def put_period_day(conn, date_str: str, record: dict) -> dict:
    if not is_valid_ymd(date_str):
        raise ValueError('invalid date')
    if not isinstance(record, dict):
        raise ValueError('invalid record')
    cleaned = {k: v for k, v in record.items() if k in ALLOWED_DAY_KEYS and v is not None}
    if cleaned:
        conn.execute(
            """INSERT INTO period_days (date, data, updated_at)
               VALUES (?, ?, datetime('now','+8 hours'))
               ON CONFLICT(date) DO UPDATE SET
                 data=excluded.data, updated_at=excluded.updated_at""",
            (date_str, json.dumps(cleaned, ensure_ascii=False)),
        )
    else:
        conn.execute("DELETE FROM period_days WHERE date=?", (date_str,))
    sync_sex_record(conn, date_str, cleaned.get('sex') is True)
    rebuild_period_start_markers(conn)
    return cleaned


def merge_legacy_into_days(day_rows, legacy_rows) -> dict:
    """Build the days map: legacy period/start/sex first, period_days overrides."""
    days: dict = {}
    for r in legacy_rows:
        d = r['date'] if hasattr(r, 'keys') else r[0]
        rtype = r['type'] if hasattr(r, 'keys') else r[1]
        if not is_valid_ymd(d):
            log.warning('skipping dirty legacy date in days merge: %r', d)
            continue
        if rtype in BLEEDING_RECORD_TYPES:
            days.setdefault(d, {})['came'] = True
        elif rtype == 'sex':
            days.setdefault(d, {})['sex'] = True
        # end / unknown: never treat as intimacy or bleeding
    for r in day_rows:
        d = r['date'] if hasattr(r, 'keys') else r[0]
        raw = r['data'] if hasattr(r, 'keys') else r[1]
        if not is_valid_ymd(d):
            log.warning('skipping dirty period_days date in days merge: %r', d)
            continue
        rec = _load_day_data(raw)
        if rec:
            days[d] = rec
    return days


def record_type_label(rtype: str) -> str:
    if rtype in ('period', 'start'):
        return '经期'
    if rtype == 'end':
        return '经期结束'
    if rtype == 'sex':
        return '亲密'
    return '记录'
