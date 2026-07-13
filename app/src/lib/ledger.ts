// Shared vocabulary + helpers for the 账本 (LedgerScreen) page, ported from
// the Ledger.dc.html design prototype. The backend stores category as a
// Chinese display name and the page's extra fields in a JSON `meta` column,
// so this module owns the mapping in both directions.
import type { LedgerEntry, LedgerWho } from '../types';

export interface LedgerCat {
  id: string;
  name: string;
  emoji: string;
  color: string;
}

export const LEDGER_CATS: LedgerCat[] = [
  { id: 'food', name: '餐饮', emoji: '🍜', color: '#B76E79' },
  { id: 'cat', name: '猫咪', emoji: '🐱', color: '#D9A441' },
  { id: 'book', name: '书', emoji: '📚', color: '#8A7AB5' },
  { id: 'transit', name: '交通', emoji: '🚇', color: '#7E93AD' },
  { id: 'home', name: '居家', emoji: '🪴', color: '#7FA98F' },
  { id: 'med', name: '医疗', emoji: '🩹', color: '#C08497' },
  { id: 'gift', name: '礼物', emoji: '🎁', color: '#9C3B4A' },
  { id: 'other', name: '其他', emoji: '🌾', color: '#C4B4AF' },
  { id: 'income', name: '收入', emoji: '💰', color: '#5E8A6E' },
];

export const LEDGER_WHO: Record<LedgerWho, { name: string; color: string }> = {
  fy: { name: '费佳', color: '#8A7AB5' },
  haya: { name: '哈娅', color: '#D9A441' },
  both: { name: '一起', color: '#B76E79' },
};

export const LEDGER_REASONS = ['想吃', '必需', '纪念', '猫咪需要', '共读', '突然心动'];

export function catOf(id: string): LedgerCat {
  return LEDGER_CATS.find((c) => c.id === id) || LEDGER_CATS[7];
}

export function catIdOfName(name: string | null | undefined, amount: number): string {
  if (amount > 0) return 'income';
  const hit = LEDGER_CATS.find((c) => c.name === (name || '').trim());
  return hit ? hit.id : 'other';
}

/** 1234.5 -> '1,234.50' (decimals only when needed, or forced) */
export function fmtAmount(n: number, forceDec = false): string {
  const abs = Math.abs(n);
  const dec = forceDec || abs % 1 ? abs.toFixed(2) : String(Math.round(abs));
  const parts = dec.split('.');
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return parts.join('.');
}

/** Backend row (SELECT * from ledger) -> LedgerEntry. */
export function rowToEntry(row: {
  id: number;
  amount?: number;
  category?: string | null;
  note?: string | null;
  date?: string | null;
  author?: string | null;
  meta?: string | null;
}): LedgerEntry {
  const amount = Number(row.amount ?? 0);
  let meta: Record<string, string> = {};
  if (row.meta) {
    try {
      const parsed = JSON.parse(row.meta);
      if (parsed && typeof parsed === 'object') meta = parsed;
    } catch {
      // ignore malformed meta
    }
  }
  const author = (row.author || '').toLowerCase();
  const whoFallback: LedgerWho = author === 'haya' ? 'haya' : author.startsWith('fy') ? 'fy' : 'both';
  const who = (['fy', 'haya', 'both'] as const).includes(meta.who as LedgerWho) ? (meta.who as LedgerWho) : whoFallback;
  const catId = catIdOfName(row.category, amount);
  return {
    id: row.id,
    date: (row.date || '').slice(0, 10),
    amount,
    catId,
    title: (row.note || '').trim() || (amount > 0 ? '一笔收入' : catOf(catId).name),
    who,
    reason: meta.reason || (amount > 0 ? '收入' : '必需'),
    note: meta.note || undefined,
    mem: meta.mem || undefined,
    read: meta.read || undefined,
    later: meta.later || undefined,
  };
}

/** LedgerEntry fields -> POST/PATCH /api/ledger payload. */
export function entryToPayload(e: {
  date: string;
  amount: number;
  catId: string;
  title: string;
  who: LedgerWho;
  reason: string;
  note?: string;
  mem?: string;
  read?: string;
  later?: string;
}) {
  return {
    amount: e.amount,
    category: catOf(e.catId).name,
    note: e.title,
    date: e.date,
    author: e.who === 'both' ? 'both' : e.who,
    meta: {
      who: e.who,
      reason: e.reason,
      ...(e.note ? { note: e.note } : {}),
      ...(e.mem ? { mem: e.mem } : {}),
      ...(e.read ? { read: e.read } : {}),
      ...(e.later ? { later: e.later } : {}),
    },
  };
}

/** Catmull-Rom smoothed SVG path through evenly spaced points. */
export function smoothPath(vals: number[], w: number, h: number): { d: string; pts: Array<[number, number]> } {
  const max = Math.max(...vals, 1);
  const n = vals.length;
  const pts: Array<[number, number]> = vals.map((v, i) => [(i / (n - 1)) * w, h - (v / max) * (h - 14) - 5]);
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
  return { d, pts };
}
