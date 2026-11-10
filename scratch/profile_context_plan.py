#!/usr/bin/env python3
"""Read-only profile of production ContextPlan work against a copied SQLite DB."""
from __future__ import annotations
import argparse, json, sqlite3, time
from datetime import datetime
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--db', required=True)
args = parser.parse_args()
db_path = str(Path(args.db).expanduser().resolve())
import continuity.store as store
from continuity.store import open_continuity_read_only
from chat import daily_runtime
from chat import daily_continuity_shadow as shadow
def elapsed(call):
    start = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - start) * 1000
conn = open_continuity_read_only(db_path)
conn.row_factory = sqlite3.Row
try:
    row = conn.execute("SELECT m.id, m.content, d.context_id, d.context_epoch FROM chat_messages AS m JOIN daily_message_contexts AS d ON d.message_id=m.id WHERE d.role='user' ORDER BY m.id DESC LIMIT 1").fetchone()
finally:
    conn.close()
if row is None:
    raise SystemExit('No mapped user message available for profiling')
message_id, context_id, context_epoch = int(row['id']), int(row['context_id']), int(row['context_epoch'])
user_content = str(row['content'] or '')
surface, read_ready_surface_ms = elapsed(lambda: store.read_ready_surface(db_path))
if surface.status != 'ready':
    raise SystemExit(f'read_ready_surface status={surface.status} error={surface.error_code}')
artifacts = tuple(surface.artifacts or ())
validated = tuple({'chunk_id': str(c.chunk_id), 'artifact_revision': str(c.artifact_revision), 'body_hash': str(c.body_hash)} for c in artifacts)
conn = open_continuity_read_only(db_path)
try:
    bindings, bindings_ms = elapsed(lambda: shadow._bindings(conn, validated_ready_artifacts=validated))
finally:
    conn.close()
metrics = {'read_ready_surface_ms': [], 'source_rows_ms': [], 'bindings_ms': [], 'context_plan_validation_ms': [], 'shadow_total_ms': []}
def install_timer(obj, name, metric):
    original = getattr(obj, name)
    def timed(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            metrics[metric].append((time.perf_counter() - started) * 1000)
    setattr(obj, name, timed)
install_timer(store, 'read_ready_surface', 'read_ready_surface_ms')
install_timer(shadow, '_source_rows', 'source_rows_ms')
install_timer(shadow, '_bindings', 'bindings_ms')
install_timer(shadow, 'build_context_plan', 'context_plan_validation_ms')
install_timer(shadow, 'build_daily_continuity_shadow_plan', 'shadow_total_ms')
plan = daily_runtime.DailyTurnPlan(request_id='profile-read-only', chat_id='profile-read-only', local_day=datetime.now().date().isoformat(), context_id=context_id, context_epoch=context_epoch, resident_generation=0, resident_key='profile-read-only', user_message_id=message_id, epoch_token={}, lease_owner='profile-read-only', is_cold=False, is_respawn=False, cursor_before=None, assembly={}, manifest={}, user_content=user_content, db_path=db_path)
pipeline_started = time.perf_counter()
pipeline_status, pipeline_error, chunk_body_count = 'ready', None, 0
try:
    context_plan, chunk_bodies = daily_runtime._build_production_context_plan(plan, resident=None, static_system='', mode='hot')
    chunk_body_count = len(chunk_bodies)
    if not context_plan.valid:
        pipeline_status = 'invalid'
except Exception as exc:
    pipeline_status = 'failed'
    pipeline_error = str(getattr(exc, 'error_code', '') or type(exc).__name__)
pipeline_ms = (time.perf_counter() - pipeline_started) * 1000
def first(values):
    return round(values[0], 2) if values else None
source_ms, bindings_inside_ms = first(metrics['source_rows_ms']), first(metrics['bindings_ms'])
context_ms, shadow_total_ms = first(metrics['context_plan_validation_ms']), first(metrics['shadow_total_ms'])
shadow_construction_ms = None if shadow_total_ms is None else round(max(0.0, shadow_total_ms - (source_ms or 0.0) - (bindings_inside_ms or 0.0) - (context_ms or 0.0)), 2)
report = {'database_copy': db_path, 'ready_chunk_count': len(artifacts), 'direct_read_ready_surface_ms': round(read_ready_surface_ms, 2), 'direct_bindings_ms': round(bindings_ms, 2), 'production_plan_read_ready_surface_ms': first(metrics['read_ready_surface_ms']), 'production_plan_source_rows_ms': source_ms, 'production_plan_bindings_ms': bindings_inside_ms, 'production_plan_context_plan_validation_ms': context_ms, 'production_plan_shadow_construction_ms': shadow_construction_ms, 'production_plan_shadow_total_ms': shadow_total_ms, 'production_plan_total_ms': round(pipeline_ms, 2), 'production_plan_status': pipeline_status, 'production_plan_error': pipeline_error, 'production_plan_chunk_body_count': chunk_body_count}
print(json.dumps(report, ensure_ascii=False, sort_keys=True))
