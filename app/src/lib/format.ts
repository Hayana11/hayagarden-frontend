export const pad = (n: number): string => String(n).padStart(2, '0');

export const WEEK_CN_SUN_FIRST = ['日', '一', '二', '三', '四', '五', '六'];
export const WEEK_CN_MON_FIRST = ['一', '二', '三', '四', '五', '六', '日'];
export const MONTH_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function monthKey(base: Date): string {
  return `${base.getFullYear()}-${pad(base.getMonth() + 1)}`;
}

export function dateKey(base: Date): string {
  return `${base.getFullYear()}-${pad(base.getMonth() + 1)}-${pad(base.getDate())}`;
}

/** Leading blank cells so the 1st lands under the right weekday, Monday-first grid. */
export function leadingBlanks(base: Date): number {
  return (base.getDay() + 6) % 7;
}

export function daysInMonth(base: Date): number {
  return new Date(base.getFullYear(), base.getMonth() + 1, 0).getDate();
}

/** Deterministic pseudo-random in [0,1), used only for offline mock fallbacks. */
export function seeded(i: number): number {
  const x = Math.sin(i * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
}

/** Smooth Catmull-Rom spline through normalized [0,1] values, for the emotion sparkline. */
export function smoothPath(vals: number[], w: number, h: number): string {
  const n = vals.length;
  const pts = vals.map((v, i) => [(i / (n - 1)) * w, h - v * (h - 10) - 5]);
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[Math.min(n - 1, i + 2)];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}
