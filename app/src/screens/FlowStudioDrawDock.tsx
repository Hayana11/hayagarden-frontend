import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react';
import type { FlowStudioData } from '../lib/flowStudio/flowStudioMock';
import {
  clampDockTop,
  clampDragLeft,
  defaultDockPosition,
  defaultPreviewStageId,
  buildReelStrip,
  dockedLeft,
  DRAW_DOCK_STORAGE_KEY,
  enabledStagesOf,
  nearestDockSide,
  parseStoredDock,
  previewDraw,
  serializeDock,
  type DockBounds,
  type DockPosition,
  type PreviewDrawResult,
} from '../lib/flowStudio/flowStudioPreviewDraw';

const CHIP_WIDTH = 48;
const CHIP_HEIGHT = 48;
const DRAG_THRESHOLD = 6;
const SAVE_BAR_RESERVE = 92;

function readSafeArea(): { top: number; bottom: number } {
  if (typeof document === 'undefined') return { top: 0, bottom: 0 };
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;'
    + 'padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)';
  document.body.appendChild(probe);
  const style = window.getComputedStyle(probe);
  const result = {
    top: parseFloat(style.paddingTop) || 0,
    bottom: parseFloat(style.paddingBottom) || 0,
  };
  probe.remove();
  return result;
}

/** Space below the save bar's top edge, so the chip never covers it. */
function measuredSaveBarReserve(safeBottom: number): number {
  if (typeof document === 'undefined') return SAVE_BAR_RESERVE;
  const bar = document.querySelector('.flow-save-bar');
  if (!bar) return SAVE_BAR_RESERVE;
  const top = bar.getBoundingClientRect().top;
  const reserve = window.innerHeight - top - safeBottom + 8;
  return Math.max(SAVE_BAR_RESERVE, Math.min(window.innerHeight / 2, reserve));
}

function currentBounds(saveBarVisible: boolean): DockBounds {
  const safe = readSafeArea();
  return {
    viewportWidth: typeof window === 'undefined' ? 390 : window.innerWidth,
    viewportHeight: typeof window === 'undefined' ? 800 : window.innerHeight,
    chipWidth: CHIP_WIDTH,
    chipHeight: CHIP_HEIGHT,
    safeTop: safe.top,
    safeBottom: safe.bottom,
    reservedBottom: saveBarVisible ? measuredSaveBarReserve(safe.bottom) : 16,
    margin: 8,
  };
}

function readStoredDock(bounds: DockBounds): DockPosition {
  try {
    return parseStoredDock(window.localStorage.getItem(DRAW_DOCK_STORAGE_KEY), bounds)
      ?? defaultDockPosition(bounds);
  } catch {
    return defaultDockPosition(bounds);
  }
}

function storeDock(position: DockPosition, bounds: DockBounds): void {
  try {
    window.localStorage.setItem(DRAW_DOCK_STORAGE_KEY, serializeDock(position, bounds));
  } catch {
    // Position is a per-device convenience; failing to store it is harmless.
  }
}

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

let previewSeedCounter = 0;
function nextPreviewSeed(): string {
  previewSeedCounter += 1;
  return `preview-only:${Date.now().toString(36)}:${previewSeedCounter}:${Math.random().toString(36).slice(2, 8)}`;
}

type DrawDockProps = {
  draft: FlowStudioData;
  activeStageId: string | null;
  saveBarVisible: boolean;
};

export function FlowStudioDrawDock({ draft, activeStageId, saveBarVisible }: DrawDockProps) {
  const [bounds, setBounds] = useState<DockBounds>(() => currentBounds(saveBarVisible));
  const [dock, setDock] = useState<DockPosition>(() => readStoredDock(currentBounds(saveBarVisible)));
  const [drag, setDrag] = useState<{ left: number; top: number } | null>(null);
  const pointerRef = useRef<{ id: number; startX: number; startY: number; originLeft: number; originTop: number; moved: boolean } | null>(null);
  const [open, setOpen] = useState(false);
  const [rolling, setRolling] = useState(false);

  // A short tumble before the dialog opens; skipped when motion is reduced.
  const openWithRoll = () => {
    if (rolling) return;
    if (prefersReducedMotion()) {
      setOpen(true);
      return;
    }
    setRolling(true);
    window.setTimeout(() => {
      setRolling(false);
      setOpen(true);
    }, 420);
  };

  useEffect(() => {
    const update = () => {
      const next = currentBounds(saveBarVisible);
      setBounds(next);
      setDock((current) => ({ ...current, top: clampDockTop(current.top, next) }));
    };
    update();
    window.addEventListener('resize', update);
    window.addEventListener('orientationchange', update);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('orientationchange', update);
    };
  }, [saveBarVisible]);

  const restingLeft = dockedLeft(dock.side, bounds);
  const restingTop = clampDockTop(dock.top, bounds);

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    event.currentTarget.setPointerCapture(event.pointerId);
    pointerRef.current = {
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originLeft: restingLeft,
      originTop: restingTop,
      moved: false,
    };
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const pointer = pointerRef.current;
    if (!pointer || pointer.id !== event.pointerId) return;
    const dx = event.clientX - pointer.startX;
    const dy = event.clientY - pointer.startY;
    if (!pointer.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    pointer.moved = true;
    setDrag({
      left: clampDragLeft(pointer.originLeft + dx, bounds),
      top: clampDockTop(pointer.originTop + dy, bounds),
    });
  };

  const finishPointer = (event: ReactPointerEvent<HTMLButtonElement>, cancelled: boolean) => {
    const pointer = pointerRef.current;
    if (!pointer || pointer.id !== event.pointerId) return;
    pointerRef.current = null;
    if (!pointer.moved) {
      setDrag(null);
      if (!cancelled) openWithRoll();
      return;
    }
    const left = clampDragLeft(pointer.originLeft + (event.clientX - pointer.startX), bounds);
    const top = clampDockTop(pointer.originTop + (event.clientY - pointer.startY), bounds);
    const next: DockPosition = { side: nearestDockSide(left, bounds), top };
    setDock(next);
    setDrag(null);
    storeDock(next, bounds);
  };

  const chipStyle = drag
    ? { left: drag.left, top: drag.top }
    : { left: restingLeft, top: restingTop };

  return (
    <>
      <button
        type="button"
        className={`flow-draw-chip is-${dock.side} ${drag ? 'is-dragging' : ''} ${rolling ? 'is-rolling' : ''}`}
        style={chipStyle}
        aria-label="试抽一次（可拖动）"
        aria-haspopup="dialog"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={(event) => finishPointer(event, false)}
        onPointerCancel={(event) => finishPointer(event, true)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openWithRoll();
          }
        }}
      >
        <svg className="flow-dice" viewBox="0 0 32 32" aria-hidden="true">
          <rect x="3.5" y="3.5" width="25" height="25" rx="7" />
          <circle cx="9.5" cy="9.5" r="2.5" />
          <circle cx="22.5" cy="9.5" r="2.5" />
          <circle cx="16" cy="16" r="2.5" />
          <circle cx="9.5" cy="22.5" r="2.5" />
          <circle cx="22.5" cy="22.5" r="2.5" />
        </svg>
      </button>
      {open ? <FlowStudioDrawModal draft={draft} activeStageId={activeStageId} onClose={() => setOpen(false)} /> : null}
    </>
  );
}

const TILE_WIDTH = 78;
const TILE_GAP = 8;
const TILE_STEP = TILE_WIDTH + TILE_GAP;

type Reel = {
  key: string;
  items: string[];
  target: number;
  phase: 'start' | 'run' | 'done';
  duration: number;
};

function reelOffset(index: number, windowWidth: number): number {
  return windowWidth / 2 - (index * TILE_STEP + TILE_WIDTH / 2);
}

function FlowStudioDrawModal({ draft, activeStageId, onClose }: { draft: FlowStudioData; activeStageId: string | null; onClose: () => void }) {
  const stages = useMemo(() => enabledStagesOf(draft), [draft]);
  const [stageId, setStageId] = useState<string | null>(() => defaultPreviewStageId(draft, activeStageId));
  const [result, setResult] = useState<PreviewDrawResult>({ stageId: null, groups: [] });
  const [reels, setReels] = useState<Reel[]>([]);
  const [windowWidth, setWindowWidth] = useState(300);
  const timersRef = useRef<number[]>([]);
  const dialogRef = useRef<HTMLElement | null>(null);
  const measureRef = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    const measure = () => {
      const width = measureRef.current?.clientWidth;
      if (width) setWindowWidth(width);
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);

  const clearTimers = () => {
    timersRef.current.forEach((timer) => window.clearTimeout(timer));
    timersRef.current = [];
  };

  const roll = useCallback((targetStageId: string | null) => {
    clearTimers();
    const seed = nextPreviewSeed();
    const next = previewDraw(draft, targetStageId, seed);
    setResult(next);
    const reduced = prefersReducedMotion();
    let order = 0;
    const built: Reel[] = next.groups.flatMap((group) => group.picks.map((pick, pickIndex) => {
      const strip = buildReelStrip(group.candidates, pick, `${seed}:${group.poolId}:${pickIndex}`, 16 + order * 3);
      const reel: Reel = {
        key: `${group.poolId}:${pickIndex}`,
        items: strip.items,
        target: strip.targetIndex,
        phase: reduced ? 'done' : 'start',
        duration: 1100 + order * 260,
      };
      order += 1;
      return reel;
    }));
    setReels(built);
    if (reduced || !built.length) return;
    // Two frames so the start offset paints before the transition begins.
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      setReels((current) => current.map((reel) => ({ ...reel, phase: 'run' })));
    }));
    built.forEach((reel) => {
      timersRef.current.push(window.setTimeout(() => {
        setReels((current) => current.map((item) => (item.key === reel.key ? { ...item, phase: 'done' } : item)));
      }, reel.duration + 40));
    });
  }, [draft]);

  // Roll on open and whenever the stage changes; cleanup cancels pending stops.
  useEffect(() => {
    roll(stageId);
    const timers = timersRef;
    return () => {
      timers.current.forEach((timer) => window.clearTimeout(timer));
      timers.current = [];
    };
  }, [roll, stageId]);

  useEffect(() => {
    dialogRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const selectStage = (id: string) => {
    if (id !== stageId) setStageId(id);
    else roll(id);
  };

  const spinning = reels.some((reel) => reel.phase !== 'done');
  let reelCursor = 0;

  return (
    <div className="flow-slot-layer">
      <button type="button" className="flow-slot-backdrop" aria-label="关闭试抽" onClick={onClose} />
      <section className="flow-slot-modal" role="dialog" aria-modal="true" aria-labelledby="flow-slot-title" tabIndex={-1} ref={dialogRef}>
        <header className="flow-slot-header">
          <div>
            <h2 id="flow-slot-title">试抽一次</h2>
            <p>仅预览 · 不影响真实 Flow</p>
          </div>
          <button type="button" className="flow-slot-close" aria-label="关闭" onClick={onClose}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        </header>
        <div className="flow-slot-stages" role="radiogroup" aria-label="选择阶段">
          {stages.map((stage) => (
            <button
              type="button"
              role="radio"
              aria-checked={stage.id === stageId}
              className={stage.id === stageId ? 'is-active' : ''}
              key={stage.id}
              onClick={() => selectStage(stage.id)}
              title={stage.name}
            >
              {stage.id === activeStageId ? <i aria-hidden="true" /> : null}
              <span>{stage.name || '未命名'}</span>
            </button>
          ))}
        </div>
        <div className="flow-slot-scroll">
          <div className="flow-slot-measure" ref={measureRef} aria-hidden="true" />
          {result.groups.length ? result.groups.map((group) => (
            <section className="flow-slot-group" key={group.poolId} aria-label={group.poolName}>
              <div className="flow-slot-group-head">
                <strong>{group.poolName}</strong>
                <small>{group.mode === 'perStage' ? '整段固定' : '每轮重抽'}</small>
              </div>
              {group.picks.map((pick) => {
                const reel = reels[reelCursor];
                reelCursor += 1;
                if (!reel) return null;
                const index = reel.phase === 'start' ? 2 : reel.target;
                const style: CSSProperties = {
                  transform: `translate3d(${reelOffset(index, windowWidth)}px,0,0)`,
                  transition: reel.phase === 'run' ? `transform ${reel.duration}ms cubic-bezier(.12,.72,.16,1)` : 'none',
                };
                return (
                  <div className={`flow-reel is-${reel.phase}`} key={reel.key}>
                    <div className="flow-reel-frame" aria-hidden="true" />
                    <div className="flow-reel-strip" style={style} aria-hidden="true">
                      {reel.items.map((text, itemIndex) => (
                        <span className={`flow-reel-tile ${reel.phase === 'done' && itemIndex === reel.target ? 'is-hit' : ''}`} key={itemIndex}><em>{text}</em></span>
                      ))}
                    </div>
                    <span className="flow-reel-ticks" aria-hidden="true" />
                    <span className="flow-sr-only">{reel.phase === 'done' ? pick : ''}</span>
                  </div>
                );
              })}
            </section>
          )) : (
            <div className="flow-slot-empty">这个阶段目前没有可抽取的灵感</div>
          )}
        </div>
        <footer className="flow-slot-footer">
          <span>使用当前编辑稿</span>
          <button type="button" className="flow-slot-roll" onClick={() => roll(stageId)} disabled={spinning || !stageId}>
            <svg viewBox="0 0 32 32" aria-hidden="true"><rect x="4" y="4" width="24" height="24" rx="7" /><circle cx="11" cy="11" r="2" /><circle cx="21" cy="21" r="2" /><circle cx="16" cy="16" r="2" /></svg>
            {spinning ? '转动中…' : '再抽一次'}
          </button>
        </footer>
      </section>
    </div>
  );
}
