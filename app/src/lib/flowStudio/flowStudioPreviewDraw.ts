import type { FlowStudioData, FlowStudioPool } from './flowStudioMock';

/**
 * Preview-only draw for the floating 试抽 widget.
 *
 * Reads only the in-memory editor draft. It never touches the runtime store,
 * Flow state, fixed draws, message snapshots or the model, and its seed is a
 * preview seed that is unrelated to the production deterministic seeds.
 */
export type PreviewDrawGroup = {
  poolId: string;
  poolName: string;
  mode: FlowStudioPool['mode'];
  /** All candidates, used by the reel animation. */
  candidates: string[];
  /** Final drawn entries, at most `pool.count`. */
  picks: string[];
};

export type PreviewDrawResult = {
  stageId: string | null;
  groups: PreviewDrawGroup[];
};

function hashSeed(seed: string): number {
  let hash = 2166136261;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

/** Small deterministic PRNG so a given preview seed is reproducible in tests. */
export function previewRandom(seed: string): () => number {
  let state = hashSeed(seed) || 1;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

export function enabledStagesOf(data: FlowStudioData) {
  return data.stages.filter((stage) => stage.enabled !== false);
}

/** Stage the draw dialog opens on: the running stage, else the initial stage. */
export function defaultPreviewStageId(
  data: FlowStudioData,
  activeStageId: string | null,
): string | null {
  const stages = enabledStagesOf(data);
  if (activeStageId && stages.some((stage) => stage.id === activeStageId)) {
    return activeStageId;
  }
  if (data.initialStage && stages.some((stage) => stage.id === data.initialStage)) {
    return data.initialStage;
  }
  return stages[0]?.id ?? null;
}

export function drawablePoolsForStage(data: FlowStudioData, stageId: string | null): FlowStudioPool[] {
  const stage = data.stages.find((item) => item.id === stageId);
  if (!stage) return [];
  const seen = new Set<string>();
  const pools: FlowStudioPool[] = [];
  for (const poolId of stage.poolIds) {
    if (seen.has(poolId)) continue;
    seen.add(poolId);
    const pool = data.pools.find((item) => item.id === poolId);
    // Disabled pools are skipped exactly like the runtime draw does.
    if (!pool || !pool.enabled) continue;
    if (!pool.entries.some((entry) => entry.enabled && entry.text.trim())) continue;
    pools.push(pool);
  }
  return pools;
}

export function previewDraw(
  data: FlowStudioData,
  stageId: string | null,
  seed: string,
): PreviewDrawResult {
  const random = previewRandom(seed);
  const groups = drawablePoolsForStage(data, stageId).map((pool) => {
    const candidates = pool.entries
      .filter((entry) => entry.enabled && entry.text.trim())
      .map((entry) => entry.text.trim());
    const remaining = [...candidates];
    const count = Math.max(1, Math.min(5, Math.floor(pool.count) || 1));
    const picks: string[] = [];
    while (picks.length < count && remaining.length) {
      const index = Math.floor(random() * remaining.length);
      picks.push(remaining.splice(index, 1)[0]);
    }
    return {
      poolId: pool.id,
      poolName: pool.name || '未命名灵感池',
      mode: pool.mode,
      candidates,
      picks,
    };
  });
  return { stageId, groups };
}

export type DockSide = 'left' | 'right';

export type DockPosition = {
  side: DockSide;
  /** Top edge in CSS px from the viewport top. */
  top: number;
};

export type DockBounds = {
  viewportWidth: number;
  viewportHeight: number;
  chipWidth: number;
  chipHeight: number;
  safeTop: number;
  safeBottom: number;
  /** Space reserved at the bottom, e.g. for the save bar. */
  reservedBottom: number;
  margin: number;
};

export const DRAW_DOCK_STORAGE_KEY = 'hayagarden.flowStudio.drawDock.v1';

export function clampDockTop(top: number, bounds: DockBounds): number {
  const minTop = bounds.safeTop + bounds.margin;
  const maxTop = bounds.viewportHeight
    - bounds.safeBottom
    - bounds.reservedBottom
    - bounds.margin
    - bounds.chipHeight;
  if (maxTop <= minTop) return minTop;
  return Math.min(maxTop, Math.max(minTop, top));
}

export function clampDragLeft(left: number, bounds: DockBounds): number {
  const maxLeft = bounds.viewportWidth - bounds.chipWidth - bounds.margin;
  return Math.min(Math.max(bounds.margin, maxLeft), Math.max(bounds.margin, left));
}

/** Snap to whichever vertical edge the chip centre is closer to. */
export function nearestDockSide(left: number, bounds: DockBounds): DockSide {
  return left + bounds.chipWidth / 2 < bounds.viewportWidth / 2 ? 'left' : 'right';
}

export function dockedLeft(side: DockSide, bounds: DockBounds): number {
  return side === 'left'
    ? bounds.margin
    : Math.max(bounds.margin, bounds.viewportWidth - bounds.chipWidth - bounds.margin);
}

/** Default: right edge, lower half of the screen. */
export function defaultDockPosition(bounds: DockBounds): DockPosition {
  return { side: 'right', top: clampDockTop(bounds.viewportHeight * 0.62, bounds) };
}

/** Stored as a ratio so it survives rotation and different screen heights. */
export function serializeDock(position: DockPosition, bounds: DockBounds): string {
  const ratio = bounds.viewportHeight > 0 ? position.top / bounds.viewportHeight : 0.62;
  return JSON.stringify({ side: position.side, topRatio: Math.round(ratio * 10000) / 10000 });
}

export function parseStoredDock(raw: string | null, bounds: DockBounds): DockPosition | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as { side?: unknown; topRatio?: unknown };
    if ((value.side !== 'left' && value.side !== 'right') || typeof value.topRatio !== 'number') {
      return null;
    }
    if (!Number.isFinite(value.topRatio) || value.topRatio < 0 || value.topRatio > 1) return null;
    return {
      side: value.side,
      top: clampDockTop(value.topRatio * bounds.viewportHeight, bounds),
    };
  } catch {
    return null;
  }
}
