"""独立于 Shadow 模块的 capture evidence alert writer。

在 `internal_state_shadow` 本身无法 import 时，聊天入口仍可把不可恢复的
outbox capture 失败交给预配的独立持久卷。该模块刻意不 import SQLite/Shadow。
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Optional


CAPTURE_ALERT_PATH_ENV = 'INTERNAL_STATE_V3_CAPTURE_ALERT_PATH'


def write_capture_alert(*, db_path: Optional[str], detail: str) -> bool:
    raw = str(os.environ.get(CAPTURE_ALERT_PATH_ENV, '')).strip()
    if not raw:
        return False
    alert = Path(raw)
    try:
        alert.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f'{alert}.tmp.{uuid.uuid4().hex}')
        payload = {
            'capture_evidence_failed': True,
            'db_path': db_path,
            'detail': str(detail)[:1024],
            'detected_at': (
                datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        }
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, alert)
        fd = os.open(str(alert.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False
