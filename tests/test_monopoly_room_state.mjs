import assert from 'node:assert/strict';

const TERMINAL = new Set(['paused', 'engine_down', 'finished']);

function pendingStatus(pending) {
  if (!pending) return 'idle';
  if (pending.kind === 'task' || pending.kind === 'truth') return 'task_pending';
  if (pending.kind === 'duel') return 'duel_pending';
  if (pending.kind === 'toll') return 'toll_pending';
  return 'super_pending';
}

function statusAfterEmptyPending(current) {
  return TERMINAL.has(current) ? current : 'idle';
}

function resolveRoomStatus(current, explicit, pending) {
  if (explicit) return explicit;
  if (pending) return pendingStatus(pending);
  return statusAfterEmptyPending(current);
}

function reduceGameEventStatus(current, eventType, payload) {
  if (eventType === 'game_paused') return 'paused';
  if (eventType === 'engine_down' || eventType === 'roll_outcome_unknown') return 'engine_down';
  if (eventType === 'game_over') return 'finished';
  if (eventType === 'game_resumed' && typeof payload.status === 'string') return payload.status;
  return current;
}

assert.equal(resolveRoomStatus('engine_down', undefined, null), 'engine_down');
assert.equal(resolveRoomStatus('engine_down', 'engine_down', null), 'engine_down');
assert.equal(resolveRoomStatus('paused', undefined, null), 'paused');
assert.equal(resolveRoomStatus('idle', undefined, null), 'idle');
assert.equal(
  resolveRoomStatus('engine_down', 'engine_down', null),
  'engine_down',
);
assert.equal(
  resolveRoomStatus('idle', 'task_pending', { kind: 'task' }),
  'task_pending',
);
assert.equal(reduceGameEventStatus('idle', 'roll_outcome_unknown', {}), 'engine_down');
assert.equal(reduceGameEventStatus('engine_down', 'game.pending', {}), 'engine_down');
assert.equal(
  reduceGameEventStatus('idle', 'game_resumed', { status: 'task_pending' }),
  'task_pending',
);

console.log('monopoly room state tests ok');
