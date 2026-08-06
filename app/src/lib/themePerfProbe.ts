/** Temporary Chat theme switch timing probe (diagnostic only, never uploaded). */

export type ThemeProbeMode = 'normal' | 'transcript-hidden';

export interface ThemePerfRecord {
  mode: ThemeProbeMode;
  at: number;
  applyMs: number;
  flushMs: number;
  raf1Ms: number;
  raf2Ms: number;
}

let probeMode: ThemeProbeMode = 'normal';
let lastRecord: ThemePerfRecord | null = null;
let pending: { t0: number; t1: number; mode: ThemeProbeMode } | null = null;
let skipThemePerf = false;

export function setSkipThemePerf(skip: boolean) {
  skipThemePerf = skip;
}

export function getThemeProbeMode(): ThemeProbeMode {
  return probeMode;
}

export function setThemeProbeMode(mode: ThemeProbeMode) {
  probeMode = mode;
}

export function getLastThemePerf(): ThemePerfRecord | null {
  return lastRecord;
}

export function beginThemePerfProbe(mode: ThemeProbeMode = probeMode): void {
  if (skipThemePerf) return;
  pending = { t0: performance.now(), t1: 0, mode };
}

export function flushThemePerfProbe(root: HTMLElement | null): void {
  if (skipThemePerf || !pending || !root) return;
  pending.t1 = performance.now();
  void getComputedStyle(root).backgroundColor;
  const { t0, t1, mode } = pending;
  const flushAt = performance.now();

  requestAnimationFrame(() => {
    const raf1At = performance.now();
    requestAnimationFrame(() => {
      const raf2At = performance.now();
      lastRecord = {
        mode,
        at: t0,
        applyMs: t1 - t0,
        flushMs: flushAt - t1,
        raf1Ms: raf1At - flushAt,
        raf2Ms: raf2At - t0,
      };
      pending = null;
    });
  });
}

export function countDescendants(el: HTMLElement | null): number {
  if (!el) return 0;
  return el.getElementsByTagName('*').length;
}
