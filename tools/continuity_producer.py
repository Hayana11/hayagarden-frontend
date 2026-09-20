"""Production CLI entrypoint for the explicit R4.5-C1 producer.

This module delegates all production behavior to
continuity.producer.run_continuity_producer. It does not install or invoke a
scheduler, select a provider, generate content, or write persistence directly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from continuity.producer import (
    ContinuityProducerResult,
    run_continuity_producer,
)


PRODUCTION_DB = REPOSITORY_ROOT / 'memories.db'
_SUCCESS_STATUSES = frozenset({'idle', 'ready'})


def _json_payload(result: ContinuityProducerResult) -> dict[str, object]:
    identity = result.window_identity or {}
    return {
        'status': result.status,
        'error_code': result.error_code,
        'context_id': identity.get('context_id'),
        'context_epoch': identity.get('context_epoch'),
        'source_member_count': result.source_member_count,
        'unclaimed_source_count': result.unclaimed_source_count,
        'sealed_candidate_count': result.sealed_candidate_count,
        'queued_generation_job_count': result.queued_generation_job_count,
        'generated_generation_job_id': result.generated_generation_job_id,
        'generated_chunk_id': result.generated_chunk_id,
        'model_call_count': result.model_call_count,
    }


def main() -> int:
    result = run_continuity_producer(
        source_db_path=PRODUCTION_DB,
        continuity_store_path=PRODUCTION_DB,
    )
    print(json.dumps(_json_payload(result), sort_keys=True))
    return 0 if result.status in _SUCCESS_STATUSES else 1


if __name__ == '__main__':
    raise SystemExit(main())
