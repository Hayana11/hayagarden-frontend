import type { CSSProperties } from 'react';

/** Canonical React SPA font families — single source, backed by :root CSS variables. */
export const FONT_CN = 'var(--font-serif-cn)';
export const FONT_DISPLAY = 'var(--font-serif-display)';
/** Code / JSON / tool ids — true monospace (not --font-mono UI sans in config CSS). */
export const FONT_MONO = 'ui-monospace, Menlo, monospace';

const CJK_RE = /[\u3400-\u9FFF\uF900-\uFAFF]/;

export function hasCJK(text: string): boolean {
  return CJK_RE.test(text);
}

/** Pick CN serif when user-visible text contains CJK; otherwise Bodoni display. */
export function fontFamilyForText(text: string): string {
  return hasCJK(text) ? FONT_CN : FONT_DISPLAY;
}

export const sectionLabelStyle: CSSProperties = {
  fontSize: 11,
  letterSpacing: 3,
  color: 'var(--ghost)',
};
