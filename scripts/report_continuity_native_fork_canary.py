#!/usr/bin/env python3
"""R5 — one-shot canary readout for a single Continuity Native-Fork chunk.

Read-only. Does not generate anything, does not flip
``CC_CONTINUITY_NATIVE_FORK_ENABLED``, does not select which job runs. It
only reads back one already-published ``ContinuityChunk`` and prints the
exact evidence R5 asks the operator to eyeball before trusting Native Fork
for anything beyond one candidate:

  * ``actual_executor`` really is ``claude_code_continuity_native_fork``
    (a caller who points this at an ordinary isolated-one-shot chunk gets an
    explicit ``not_a_native_fork_chunk`` verdict, not a false pass).
  * ``cache_read_input_tokens`` is large relative to the new input this run
    actually paid for — evidence the forked child reused the parent's cached
    prefix instead of re-paying for it.

Usage (against the real production store, after one real job has already
run with the flag on for real):

    python scripts/report_continuity_native_fork_canary.py \
        --store-db /opt/frontend/memories.db \
        --generation-job-id <job-id>

This is the only step of R0-R6 that needs a real model call against real
production data; nothing here fabricates or assumes what that call reports.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from continuity.native_fork_executor import NATIVE_FORK_EXECUTOR
from continuity.store import load_ready_chunk_for_job, open_continuity_read_only

# Cache-read tokens at or above this floor are the "clearly working" bar the
# R5 spec calls out (its own example: cache_read >= 80k). Below it, the run
# still executed via native fork, but the caching benefit is not proven yet.
CACHE_READ_CONVINCING_FLOOR = 80_000


def _verdict(chunk) -> dict[str, object]:
    if chunk is None:
        return {'ok': False, 'verdict': 'chunk_not_found'}
    if chunk.actual_executor != NATIVE_FORK_EXECUTOR:
        return {
            'ok': False,
            'verdict': 'not_a_native_fork_chunk',
            'actual_executor': chunk.actual_executor,
        }
    cache_read = chunk.cache_read_input_tokens
    cache_creation = chunk.cache_creation_input_tokens
    input_tokens = chunk.input_tokens
    output_tokens = chunk.provider_output_tokens
    convincing = isinstance(cache_read, int) and cache_read >= CACHE_READ_CONVINCING_FLOOR
    return {
        'ok': True,
        'verdict': 'cache_reuse_convincing' if convincing else 'cache_reuse_not_yet_shown',
        'actual_executor': chunk.actual_executor,
        'cache_usage_status': chunk.cache_usage_status,
        'cache_hit': chunk.cache_hit,
        'cache_read_input_tokens': cache_read,
        'cache_creation_input_tokens': cache_creation,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'convincing_floor': CACHE_READ_CONVINCING_FLOOR,
        'chunk_id': chunk.chunk_id,
        'generation_job_id': chunk.generation_job_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store-db', required=True, help='continuity store (read-only open)')
    parser.add_argument('--generation-job-id', required=True)
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    args = parser.parse_args(argv)

    conn = open_continuity_read_only(args.store_db)
    try:
        chunk = load_ready_chunk_for_job(conn, args.generation_job_id)
        report = _verdict(chunk)
    finally:
        conn.close()

    if args.format == 'json':
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    else:
        for key in (
            'verdict', 'actual_executor', 'cache_usage_status', 'cache_hit',
            'cache_read_input_tokens', 'cache_creation_input_tokens',
            'input_tokens', 'output_tokens', 'convincing_floor',
            'chunk_id', 'generation_job_id',
        ):
            if key in report:
                print(f'{key}: {report[key]}')
    return 0 if report.get('verdict') == 'cache_reuse_convincing' else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
