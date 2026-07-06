import { WEEK_CN_SUN_FIRST } from './format';

export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  const v = n / 1000;
  return (Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)) + 'k';
}

export function formatCurrency(n: number): string {
  return '¥' + n.toLocaleString('en-US');
}

export function formatResetHint(iso: string, now: Date): string {
  const d = new Date(iso);
  const sameDay = d.toDateString() === now.toDateString();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (sameDay) return `重置于今天 ${hh}:${mm}`;
  return `重置于 ${d.getMonth() + 1}/${d.getDate()} 周${WEEK_CN_SUN_FIRST[d.getDay()]}`;
}
