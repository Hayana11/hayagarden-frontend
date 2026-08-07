import assert from 'node:assert/strict';
import {
  LEGACY_WINDOW_SIZE,
  LEGACY_WINDOW_STEP,
  latestTranscriptWindow,
  shiftTranscriptWindowOlder,
  shiftTranscriptWindowNewer,
  transcriptWindowAfterPrepend,
  transcriptWindowAroundIndex,
  isTranscriptWindowAtLatest,
  clampTranscriptWindow,
  windowSize,
} from '../src/lib/legacyTranscriptWindow.ts';

assert.equal(LEGACY_WINDOW_SIZE, 20);
assert.equal(LEGACY_WINDOW_STEP, 12);

// 1. modern-equivalent: small lists render fully
assert.deepEqual(latestTranscriptWindow(5), { start: 0, end: 5 });
assert.deepEqual(latestTranscriptWindow(0), { start: 0, end: 0 });

// 2. legacy initial → latest bounded window
assert.deepEqual(latestTranscriptWindow(80), { start: 60, end: 80 });
assert.equal(windowSize(latestTranscriptWindow(80)), 20);
assert.ok(isTranscriptWindowAtLatest(latestTranscriptWindow(80), 80));

// 3. older paging → still bounded, with overlap
{
  const latest = latestTranscriptWindow(80);
  const older = shiftTranscriptWindowOlder(latest, 80);
  assert.deepEqual(older, { start: 48, end: 68 });
  assert.equal(windowSize(older), 20);
  // overlap with previous: [60,68) = 8
  assert.equal(older.end - latest.start, LEGACY_WINDOW_SIZE - LEGACY_WINDOW_STEP);
  const older2 = shiftTranscriptWindowOlder(older, 80);
  assert.deepEqual(older2, { start: 36, end: 56 });
  assert.equal(windowSize(older2), 20);
}

// 4. newer / latest navigation → bounded
{
  const mid = { start: 36, end: 56 };
  const newer = shiftTranscriptWindowNewer(mid, 80);
  assert.deepEqual(newer, { start: 48, end: 68 });
  assert.equal(windowSize(newer), 20);
  const pin = latestTranscriptWindow(80);
  assert.ok(isTranscriptWindowAtLatest(pin, 80));
}

// 5. loadEarlier prepend → msgs data preserved conceptually; DOM window bounded + continuity
{
  // Was at start of loaded msgs [0,20); server prepends 80 → total 160
  const win = transcriptWindowAfterPrepend(80, 160);
  assert.equal(windowSize(win), 20);
  // includes some new (before index 80) and some previous earliest (from 80)
  assert.ok(win.start < 80);
  assert.ok(win.end > 80);
  // overlap = SIZE-STEP = 8 newly loaded + 12 previously-earliest
  assert.deepEqual(win, { start: 72, end: 92 });
}

// 6. search jump to unmounted → window relocates around target
{
  const around = transcriptWindowAroundIndex(10, 80);
  assert.ok(around.start <= 10 && 10 < around.end);
  assert.equal(windowSize(around), 20);
  const nearEnd = transcriptWindowAroundIndex(78, 80);
  assert.ok(nearEnd.start <= 78 && 78 < nearEnd.end);
  assert.equal(windowSize(nearEnd), 20);
  assert.equal(nearEnd.end, 80);
}

// 7. append while at latest → follow latest grows end
{
  const before = latestTranscriptWindow(80);
  assert.ok(isTranscriptWindowAtLatest(before, 80));
  const after = latestTranscriptWindow(82);
  assert.deepEqual(after, { start: 62, end: 82 });
  assert.ok(isTranscriptWindowAtLatest(after, 82));
}

// 8. browsing older + total grows (background) → clamp keeps start, does not force latest
{
  const browsing = { start: 20, end: 40 };
  assert.equal(isTranscriptWindowAtLatest(browsing, 80), false);
  const clamped = clampTranscriptWindow(browsing.start, browsing.end, 85);
  assert.deepEqual(clamped, { start: 20, end: 40 });
  assert.equal(isTranscriptWindowAtLatest(clamped, 85), false);
}

// 9. shift older at start stays bounded at head
{
  const head = shiftTranscriptWindowOlder({ start: 0, end: 20 }, 80);
  assert.deepEqual(head, { start: 0, end: 20 });
}

console.log('test-legacy-transcript-window: ok');
