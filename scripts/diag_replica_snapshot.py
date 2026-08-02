#!/usr/bin/env python3
"""Read-only snapshot assembly diagnostic."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from chat.daily_replica_snapshot import (
    _default_assembly_builder,
    _default_context_loader,
    _default_current_context_loader,
    _default_message_context_loader,
    _truncate_snapshot_at_user_boundary,
    copy_sqlite_readonly,
)
from chat.system_builder import build_cc_daily_static_parts


def main() -> int:
    mid = int(sys.argv[1])
    tmp = Path(tempfile.mkdtemp(prefix='diag-replica-'))
    db = tmp / 'snap.sqlite3'
    copy_sqlite_readonly('/opt/frontend/memories.db', str(db))
    mapping = _default_message_context_loader(mid, db_path=str(db))
    _truncate_snapshot_at_user_boundary(db, mid)
    ctx = _default_context_loader(int(mapping['context_id']), db_path=str(db))
    cur = _default_current_context_loader(db_path=str(db))
    parts = build_cc_daily_static_parts()
    assembly = _default_assembly_builder(
        chat_id='default',
        daily_context=dict(ctx),
        current_user_message_id=mid,
        static_system=parts['full_system'],
        is_cold=True,
        is_respawn=False,
        last_state_snapshot=None,
        inject_handoff=True,
        inject_carryover=True,
        db_path=str(db),
    )
    hist = assembly.get('current_day_history') or []
    print('mapping', dict(mapping))
    print('ctx', ctx['id'], ctx['context_epoch'], ctx.get('boundary_message_id'))
    print('current', cur['id'], cur['context_epoch'])
    print('history_len', len(hist))
    for h in hist:
        print(h['message_id'], h['role'], str(h['content'])[:60])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
