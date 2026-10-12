import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import type { FlowStudioData } from '../lib/flowStudio/flowStudioMock';
import {
  clampDockTop,
  clampDragLeft,
  defaultDockPosition,
  defaultPreviewStageId,
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

const CHIP_WIDTH = 64;
const CHIP_HEIGHT = 40;
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
    margin: 10,
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
      if (!cancelled) setOpen(true);
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
        className={`flow-draw-chip is-${dock.side} ${drag ? 'is-dragging' : ''}`}
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
            setOpen(true);
          }
        }}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" /></svg>
        <span>试抽</span>
      </button>
      {open ? <FlowStudioDrawModal draft={draft} activeStageId={activeStageId} onClose={() => setOpen(false)} /> : null}
    </>
  );
}

type ReelState = { spinning: boolean; frame: string[] };

/** Neighbour lines shown faded above/below the result, like a slot window. */
function ghostAround(candidates: string[], frame: string[], slot: 0 | 1): string | null {
  const others = candidates.filter((text) => !frame.includes(text));
  if (!others.length) return null;
  const anchor = candidates.indexOf(frame[0] ?? '');
  const base = anchor < 0 ? 0 : anchor;
  const above = others[base % others.length] ?? null;
  if (slot === 0) return above;
  const below = others[(base + 1) % others.length] ?? null;
  return below === above ? null : below;
}

function FlowStudioDrawModal({ draft, activeStageId, onClose }: { draft: FlowStudioData; activeStageId: string | null; onClose: () => void }) {
  const stages = useMemo(() => enabledStagesOf(draft), [draft]);
  const [stageId, setStageId] = useState<string | null>(() => defaultPreviewStageId(draft, activeStageId));
  const [result, setResult] = useState<PreviewDrawResult>(() => previewDraw(draft, defaultPreviewStageId(draft, activeStageId), nextPreviewSeed()));
  const [reels, setReels] = useState<ReelState[]>([]);
  const timersRef = useRef<number[]>([]);
  const dialogRef = useRef<HTMLElement | null>(null);

  const clearTimers = () => {
    timersRef.current.forEach((timer) => window.clearTimeout(timer));
    timersRef.current.forEach((timer) => window.clearInterval(timer));
    timersRef.current = [];
  };

  const roll = useCallback((targetStageId: string | null) => {
    clearTimers();
    const next = previewDraw(draft, targetStageId, nextPreviewSeed());
    setResult(next);
    if (prefersReducedMotion() || !next.groups.length) {
      setReels(next.groups.map((group) => ({ spinning: false, frame: group.picks })));
      return;
    }
    setReels(next.groups.map((group) => ({ spinning: true, frame: group.picks })));
    next.groups.forEach((group, index) => {
      const ticker = window.setInterval(() => {
        setReels((current) => current.map((reel, reelIndex) => (
          reelIndex === index && reel.spinning
            ? {
                spinning: true,
                frame: group.picks.map(() => group.candidates[Math.floor(Math.random() * group.candidates.length)]),
              }
            : reel
        )));
      }, 70);
      const stop = window.setTimeout(() => {
        window.clearInterval(ticker);
        setReels((current) => current.map((reel, reelIndex) => (
          reelIndex === index ? { spinning: false, frame: group.picks } : reel
        )));
      }, Math.min(1000, 620 + index * 120));
      timersRef.current.push(ticker, stop);
    });
  }, [draft]);

  // Roll once when the dialog opens; later rolls are explicit user actions.
  const openedRef = useRef(false);
  useEffect(() => {
    if (openedRef.current) return;
    openedRef.current = true;
    roll(stageId);
  }, [roll, stageId]);

  useEffect(() => {
    const timers = timersRef;
    return () => {
      timers.current.forEach((timer) => { window.clearTimeout(timer); window.clearInterval(timer); });
      timers.current = [];
    };
  }, []);

  useEffect(() => {
    dialogRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const selectStage = (id: string) => {
    setStageId(id);
    roll(id);
  };

  const stageName = stages.find((stage) => stage.id === stageId)?.name || '未选择阶段';

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
        <div className="flow-slot-window" aria-live="polite">
          {result.groups.length ? result.groups.map((group, index) => {
            const reel = reels[index] ?? { spinning: false, frame: group.picks };
            return (
              <article className={`flow-slot-reel ${reel.spinning ? 'is-spinning' : 'is-settled'}`} key={group.poolId}>
                <div className="flow-slot-reel-head">
                  <strong>{group.poolName}</strong>
                  <small>{group.mode === 'perStage' ? '整段固定' : '每轮重抽'}</small>
                </div>
                <div className="flow-slot-reel-body">
                  {ghostAround(group.candidates, reel.frame, 0) ? <p className="flow-slot-ghost" aria-hidden="true">{ghostAround(group.candidates, reel.frame, 0)}</p> : null}
                  {reel.frame.map((text, itemIndex) => (
                    <p className="flow-slot-pick" key={`${group.poolId}-${itemIndex}`}>{text}</p>
                  ))}
                  {ghostAround(group.candidates, reel.frame, 1) ? <p className="flow-slot-ghost" aria-hidden="true">{ghostAround(group.candidates, reel.frame, 1)}</p> : null}
                </div>
              </article>
            );
          }) : (
            <div className="flow-slot-empty">这个阶段目前没有可抽取的灵感</div>
          )}
        </div>
        <footer className="flow-slot-footer">
          <span>{stageName} · 使用当前编辑稿</span>
          <button type="button" className="flow-slot-roll" onClick={() => roll(stageId)} disabled={!result.groups.length && !stageId}>再抽一次</button>
        </footer>
      </section>
    </div>
  );
}
