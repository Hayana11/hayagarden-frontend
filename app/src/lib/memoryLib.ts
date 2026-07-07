import type { MemoryWeight } from '../types';

export type MemoryState = 'core' | 'long' | 'short' | 'inbox';

export function memoryState(weight: MemoryWeight): MemoryState {
  if (weight === 5) return 'core';
  if (weight === 4) return 'long';
  if (weight >= 2) return 'short';
  return 'inbox';
}

export interface MoonInfo {
  d: string;
  fill: string;
  stroke: string;
  size: number;
  glow: string;
  tip: string;
}

const MOON_PATH: Record<MemoryWeight, string> = {
  1: '',
  2: 'M12 3 A9 9 0 0 1 12 21 A4.5 9 0 0 0 12 3 Z',
  3: 'M12 3 A9 9 0 0 1 12 21 Z',
  4: 'M12 3 A9 9 0 0 1 12 21 A4.5 9 0 0 1 12 3 Z',
  5: 'M12 3 A9 9 0 0 1 12 21 A9 9 0 0 1 12 3 Z',
};

const MOON_META: Record<MemoryWeight, [name: string, hint: string, size: number]> = {
  1: ['新月', '碎片、随口一提', 14],
  2: ['娥眉月', '有点印象', 16],
  3: ['上弦月', '值得记住', 18],
  4: ['盈凸月', '重要', 20],
  5: ['满月', '核心记忆', 22],
};

export function moonInfo(weight: MemoryWeight, glowEnabled = true): MoonInfo {
  const [name, hint, size] = MOON_META[weight] ?? MOON_META[1];
  return {
    d: MOON_PATH[weight] ?? '',
    fill: '#B76E79',
    stroke: weight === 1 ? '#C9BDB8' : '#B76E79',
    size,
    glow: weight === 5 && glowEnabled ? 'drop-shadow(0 0 6px rgba(217,164,65,0.85))' : 'none',
    tip: `${name} · ${hint}（权重 ${weight}）`,
  };
}

export interface WeightDot {
  size: number;
  bg: string;
  border: string;
}

export function weightDot(weight: MemoryWeight): WeightDot {
  if (weight === 5) return { size: 11, bg: '#FFFFFF', border: '3px solid #B76E79' };
  if (weight === 4) return { size: 10, bg: '#FFFFFF', border: '2.5px solid #8A7AB5' };
  if (weight === 3) return { size: 8, bg: '#C08497', border: 'none' };
  if (weight === 2) return { size: 7, bg: '#D9A441', border: 'none' };
  return { size: 6, bg: '#DECFC9', border: 'none' };
}

export function titleColor(weight: MemoryWeight): string {
  return weight === 5 ? '#9C3B4A' : weight === 4 ? '#8A7AB5' : '#4A3F3C';
}

const CAT_TAGS = new Set(['小猫', '猫粮', '健康', '快递']);
const READ_TAGS = new Set(['共读', '陀思妥耶夫斯基', '批注', '电影', '争论']);
const WARM_TAGS = new Set(['信任', '情绪', '陪伴', '雷雨', '称呼', '由来', '照顾']);

export function tagColor(tag: string): { color: string; bg: string } {
  if (CAT_TAGS.has(tag)) return { color: '#B98A2E', bg: 'rgba(217,164,65,0.14)' };
  if (READ_TAGS.has(tag)) return { color: '#8A7AB5', bg: 'rgba(138,122,181,0.13)' };
  if (WARM_TAGS.has(tag)) return { color: '#B76E79', bg: 'rgba(183,110,121,0.12)' };
  return { color: '#6E9A80', bg: 'rgba(127,169,143,0.16)' };
}

export function formatDateDot(date: string): string {
  return date.replace(/-/g, '.');
}

const WEEKDAY_CN = '日一二三四五六';

export function weekdayCN(date: string): string {
  const [y, m, d] = date.split('-').map(Number);
  return '周' + WEEKDAY_CN[new Date(y, m - 1, d).getDay()];
}

export function relativeTime(date: string, time: string, now: Date): string {
  const [y, m, d] = date.split('-').map(Number);
  const [hh, mm] = time.split(':').map(Number);
  const minutes = Math.round((now.getTime() - new Date(y, m - 1, d, hh, mm).getTime()) / 60000);
  if (minutes < 3) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} 小时前`;
  if (minutes < 2880) return '昨天';
  return formatDateDot(date);
}

const CONSTELLATION_PALETTE = ['#E9C275', '#B7A8E8', '#E3A0AE', '#9FD0B2', '#7EB6D9', '#D9A6C2', '#C2B280', '#A7C7E7'];

export function constellationColor(index: number): string {
  return CONSTELLATION_PALETTE[index % CONSTELLATION_PALETTE.length];
}
