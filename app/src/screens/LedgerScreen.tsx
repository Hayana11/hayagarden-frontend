// 账本 — implements the Ledger.dc.html design: month gauge with budget ring,
// four tabs (流水/统计/日历/探索), an add/edit bottom drawer with who/reason/
// memory/reading/"later" links, and a budget sheet with auto daily recalc.
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import {
  addLedgerEntry,
  deleteLedgerEntry,
  fetchBookCurrent,
  fetchLedgerBudgetAmount,
  fetchLedgerEntries,
  fetchLedgerTrend,
  fetchMemorySummary,
  setLedgerBudgetAmount,
  updateLedgerEntry,
  type LedgerEntryDraft,
} from '../lib/api';
import {
  LEDGER_CATS,
  LEDGER_REASONS,
  LEDGER_WHO,
  catOf,
  createMonthRequestGuard,
  fmtAmount,
  migrateLegacyDailyBudget,
  readDailyBudget,
  resolveLedgerLinks,
  seedDrawerLinksFromEntry,
  shouldApplyMonthTicket,
  smoothPath,
  weekSpendBuckets,
  writeDailyBudget,
} from '../lib/ledger';
import { monthKey } from '../lib/format';
import type { LedgerEntry, LedgerTrendPoint, LedgerWho, MemoryItem } from '../types';

const DISPLAY = 'var(--font-serif-display)';
const WEEKDAY_EN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const TABS = ['流水', '统计', '日历', '探索'] as const;
type Tab = (typeof TABS)[number];

const RING_C = 389.6; // 2π·62
const DONUT_C = 2 * Math.PI * 54;

const LS_AUTO = 'ledger.autoRecalc';

interface DrawerForm {
  type: 'exp' | 'inc';
  amount: string;
  catId: string;
  who: LedgerWho;
  reason: string;
  title: string;
  /** Actual linked memory text (source of truth). */
  mem?: string;
  memSel: number | null;
  memOpen: boolean;
  /** Actual linked reading text (source of truth). */
  read?: string;
  readOn: boolean;
  laterOpen: boolean;
  later: string;
}

function blankForm(): DrawerForm {
  return {
    type: 'exp',
    amount: '',
    catId: 'food',
    who: 'both',
    reason: '想吃',
    title: '',
    mem: undefined,
    memSel: null,
    memOpen: false,
    read: undefined,
    readOn: false,
    laterOpen: false,
    later: '',
  };
}

function toYmd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const sectionLabel: CSSProperties = { fontSize: 12, color: 'var(--color-text-faint)', letterSpacing: 2, marginTop: 18 };
const sheetShell: CSSProperties = {
  position: 'fixed',
  bottom: 0,
  left: '50%',
  transform: 'translateX(-50%)',
  width: '100%',
  maxWidth: 452,
  background: '#FBF7F5',
  borderRadius: '26px 26px 0 0',
  boxShadow: '0 -14px 44px rgba(74,63,60,0.20)',
  zIndex: 61,
};
const overlayStyle: CSSProperties = { position: 'fixed', inset: 0, background: 'rgba(74,63,60,0.35)', backdropFilter: 'blur(2px)', zIndex: 60 };
const grabber: CSSProperties = { width: 40, height: 4, borderRadius: 2, background: '#E4D4CC', margin: '0 auto' };

export function LedgerScreen() {
  const today = toYmd(new Date());
  const [viewOffset, setViewOffset] = useState(0);
  const [tab, setTab] = useState<Tab>('流水');
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [budget, setBudget] = useState<number | null>(null);
  const [budgetDraft, setBudgetDraft] = useState(0);
  const [trend, setTrend] = useState<LedgerTrendPoint[]>([]);
  const [memPicks, setMemPicks] = useState<MemoryItem[]>([]);
  const [readRef, setReadRef] = useState('');

  const [filterCat, setFilterCat] = useState('all');
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [barMode, setBarMode] = useState<'week' | 'cat'>('week');
  const [aiOpen, setAiOpen] = useState(false);
  const [calSel, setCalSel] = useState<number | null>(null);
  const [laterOpen, setLaterOpen] = useState(false);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editKeep, setEditKeep] = useState<{ date: string; note?: string } | null>(null);
  const [form, setForm] = useState<DrawerForm>(blankForm);

  const [budgetSheetOpen, setBudgetSheetOpen] = useState(false);
  const [dailyBudget, setDailyBudget] = useState<number | null>(null);
  const [autoRecalc, setAutoRecalc] = useState(() => localStorage.getItem(LS_AUTO) !== '0');

  const [monthLoading, setMonthLoading] = useState(true);
  const [budgetLoading, setBudgetLoading] = useState(true);
  const [entriesError, setEntriesError] = useState(false);
  const [budgetError, setBudgetError] = useState(false);
  const [trendError, setTrendError] = useState(false);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [budgetSaving, setBudgetSaving] = useState(false);

  const monthGuardRef = useRef(createMonthRequestGuard());
  const migratedDailyRef = useRef(false);

  const now = new Date();
  const base = new Date(now.getFullYear(), now.getMonth() + viewOffset, 1);
  const key = monthKey(base);
  const currentMonthRef = useRef(key);
  currentMonthRef.current = key;
  const isCur = viewOffset === 0;
  const monthLabel = `${base.getFullYear()}.${String(base.getMonth() + 1).padStart(2, '0')}`;

  function showError(msg: string) {
    setErrorBanner(msg);
    window.setTimeout(() => setErrorBanner((cur) => (cur === msg ? null : cur)), 4200);
  }

  async function reloadMonth(month: string) {
    const ticket = monthGuardRef.current.begin(month);
    setMonthLoading(true);
    setBudgetLoading(true);
    setEntriesError(false);
    setBudgetError(false);
    setEntries([]);
    setBudget(null);
    try {
      const [entriesRes, budgetRes] = await Promise.all([
        fetchLedgerEntries(month).then(
          (data) => ({ ok: true as const, data }),
          () => ({ ok: false as const }),
        ),
        fetchLedgerBudgetAmount(month).then(
          (v) => ({ ok: true as const, v }),
          () => ({ ok: false as const }),
        ),
      ]);
      if (!shouldApplyMonthTicket(ticket, currentMonthRef.current)) return;
      if (!entriesRes.ok) {
        setEntriesError(true);
        setEntries([]);
      } else {
        setEntries(entriesRes.data);
      }
      if (!budgetRes.ok || budgetRes.v === null) {
        setBudgetError(true);
        setBudget(null);
      } else {
        setBudget(budgetRes.v);
        setBudgetError(false);
      }
    } finally {
      if (shouldApplyMonthTicket(ticket, currentMonthRef.current)) {
        setMonthLoading(false);
        setBudgetLoading(false);
      }
    }
  }

  useEffect(() => {
    if (!migratedDailyRef.current) {
      migrateLegacyDailyBudget(monthKey(new Date()));
      migratedDailyRef.current = true;
    }
    setDailyBudget(readDailyBudget(key));
    void reloadMonth(key);
  }, [key]);

  useEffect(() => {
    fetchLedgerTrend(new Date()).then(
      (t) => {
        setTrend(t);
        setTrendError(false);
      },
      () => {
        setTrend([]);
        setTrendError(true);
      },
    );
    fetchMemorySummary().then((s) => {
      const recent = s.sections.find((x) => x.key === 'recent');
      setMemPicks((recent?.items || []).slice(0, 3));
    });
    fetchBookCurrent().then((b) => setReadRef(`《${b.title}》 · p.${b.page}`));
  }, []);

  const patchForm = (p: Partial<DrawerForm>) => setForm((f) => ({ ...f, ...p }));

  // ── derivations ──
  const monthEntries = useMemo(
    () => [...entries].sort((a, b) => (b.date < a.date ? -1 : b.date > a.date ? 1 : b.id - a.id)),
    [entries],
  );
  const spend = monthEntries.filter((e) => e.amount < 0).reduce((s, e) => s - e.amount, 0);
  const income = monthEntries.filter((e) => e.amount > 0).reduce((s, e) => s + e.amount, 0);
  const budgetReady = !budgetLoading && !budgetError && budget !== null;
  const pct = budgetReady && budget > 0 ? spend / budget : 0;
  const over = budgetReady && spend > budget;
  const near = budgetReady && !over && pct >= 0.8;

  const dim = new Date(base.getFullYear(), base.getMonth() + 1, 0).getDate();
  const todayD = now.getDate();
  const todaySpend = monthEntries.filter((e) => e.date === today && e.amount < 0).reduce((s, e) => s - e.amount, 0);
  const daysLeft = isCur ? Math.max(dim - todayD + 1, 1) : dim;
  const dayAllow = budgetReady
    ? dailyBudget ?? (autoRecalc && isCur ? Math.max(budget - (spend - todaySpend), 0) / daysLeft : budget / dim)
    : 0;
  const todayLeft = budgetReady ? dayAllow - todaySpend : 0;
  const fmt1 = (v: number) => fmtAmount(Math.round(v * 10) / 10);

  const statusLine = !budgetReady
    ? ''
    : over
      ? `超出预算 ¥${fmtAmount(spend - budget)} · 这个月先慢一点`
      : near
        ? `快到预算了，还可以花 ¥${fmtAmount(budget - spend)}`
        : `还可以花 ¥${fmtAmount(budget - spend)}`;

  // 流水
  const usedCats = [...new Set(monthEntries.map((e) => e.catId))];
  const filtered = filterCat === 'all' ? monthEntries : monthEntries.filter((e) => e.catId === filterCat);
  const dayGroups = useMemo(() => {
    const gs: Array<{ date: string; label: string; items: LedgerEntry[] }> = [];
    for (const e of filtered) {
      let g = gs.find((x) => x.date === e.date);
      if (!g) {
        const d = new Date(`${e.date}T12:00:00`);
        g = { date: e.date, label: `${e.date.slice(5).replace('-', '.')} ${WEEKDAY_EN[d.getDay()]}`, items: [] };
        gs.push(g);
      }
      g.items.push(e);
    }
    return gs;
  }, [filtered]);

  // 统计
  const catTotals = useMemo(() => {
    const t: Record<string, number> = {};
    for (const e of monthEntries) if (e.amount < 0) t[e.catId] = (t[e.catId] || 0) - e.amount;
    return Object.entries(t).sort((a, b) => b[1] - a[1]);
  }, [monthEntries]);
  let donutAcc = 0;
  const donutSegs = catTotals.map(([id, v]) => {
    const frac = spend ? v / spend : 0;
    const len = Math.max(frac * DONUT_C - 2, 0.5);
    const seg = { color: catOf(id).color, dash: `${len.toFixed(1)} ${(DONUT_C - len).toFixed(1)}`, offset: (-donutAcc * DONUT_C).toFixed(1) };
    donutAcc += frac;
    return seg;
  });
  const donutTop = catTotals[0];

  const trendVals = trend.length ? trend.map((t) => (t.month === key && isCur ? Math.round(spend) : t.expense)) : [0, 0, 0, 0, 0, Math.round(spend)];
  const { d: trendPath, pts: trendPts } = smoothPath(trendVals.map((v) => Math.max(v, 0)), 340, 96);
  const trendLast = trendPts[trendPts.length - 1];
  const trendLabels = trend.length
    ? trend.map((t) => new Date(`${t.month}-01T12:00:00`).toLocaleString('en', { month: 'short' }))
    : ['—', '—', '—', '—', '—', '—'];

  const { values: weeks, labels: weekLabels } = useMemo(
    () => weekSpendBuckets(monthEntries, dim),
    [monthEntries, dim],
  );
  const wMax = Math.max(...weeks, 1);
  const cMax = catTotals.length ? catTotals[0][1] : 1;

  const memLinkedCount = monthEntries.filter((e) => e.mem).length;
  const readLinkedCount = monthEntries.filter((e) => e.read).length;
  const obsText = donutTop
    ? `这个月${catOf(donutTop[0]).name}花得最多，占了 ${Math.round((donutTop[1] / (spend || 1)) * 100)}%。` +
      (readLinkedCount ? `书的支出有 ${readLinkedCount} 笔关联了共读。` : '') +
      (memLinkedCount ? `有 ${memLinkedCount} 笔消费和记忆连在一起——钱花在了会被记住的地方。` : '')
    : '这个月还没有支出记录，观察也要有素材才行。';

  // 日历
  const byDay = useMemo(() => {
    const m: Record<number, { spend: number; income: boolean; mem: boolean; items: LedgerEntry[] }> = {};
    for (const e of monthEntries) {
      const d = parseInt(e.date.slice(8), 10);
      m[d] = m[d] || { spend: 0, income: false, mem: false, items: [] };
      if (e.amount < 0) m[d].spend -= e.amount;
      else m[d].income = true;
      if (e.mem) m[d].mem = true;
      m[d].items.push(e);
    }
    return m;
  }, [monthEntries]);

  // 探索
  const laterChains = monthEntries
    .filter((e) => e.amount > 0 && e.later)
    .map((e) => ({
      head: e,
      spends: (e.later || '').split(/[、，,]/).map((s) => s.trim()).filter(Boolean),
    }));
  const reasonCount: Record<string, number> = {};
  for (const e of monthEntries) if (e.amount < 0) reasonCount[e.reason] = (reasonCount[e.reason] || 0) + 1;
  const prefChips = Object.entries(reasonCount)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);

  // ── actions ──
  function openDrawer() {
    setEditingId(null);
    setEditKeep(null);
    setForm(blankForm());
    setDrawerOpen(true);
  }

  function startEdit(e: LedgerEntry) {
    const links = seedDrawerLinksFromEntry(e, memPicks);
    setEditingId(e.id);
    setEditKeep({ date: e.date, note: e.note });
    setForm({
      type: e.amount > 0 ? 'inc' : 'exp',
      amount: String(Math.abs(e.amount)),
      catId: e.catId === 'income' ? 'food' : e.catId,
      who: e.who,
      reason: e.reason,
      title: e.title,
      mem: links.mem,
      memSel: links.memSel,
      memOpen: false,
      read: links.read,
      readOn: links.readOn,
      laterOpen: !!e.later,
      later: links.later,
    });
    setDrawerOpen(true);
  }

  async function removeEntry(id: number) {
    if (saving) return;
    const opMonth = currentMonthRef.current;
    setSaving(true);
    const prev = entries;
    setEntries((es) => es.filter((e) => e.id !== id));
    setExpandedId(null);
    try {
      const ok = await deleteLedgerEntry(id);
      if (!ok) {
        if (opMonth === currentMonthRef.current) {
          setEntries(prev);
          showError('删除失败，记录未被删除');
        }
      }
    } finally {
      setSaving(false);
    }
  }

  async function saveEntry() {
    if (saving) return;
    const opMonth = currentMonthRef.current;
    const val = parseFloat(form.amount);
    if (!val || val <= 0) return;
    const isInc = form.type === 'inc';
    const catId = isInc ? 'income' : form.catId;
    const links = resolveLedgerLinks(form, memPicks);
    const draft: LedgerEntryDraft = {
      date: editKeep?.date ?? today,
      amount: isInc ? val : -val,
      catId,
      title: form.title.trim() || (isInc ? '一笔收入' : catOf(catId).name),
      who: form.who,
      reason: isInc ? '收入' : form.reason,
      note: editKeep?.note,
      ...links,
    };
    setSaving(true);
    try {
      if (editingId !== null) {
        const prev = entries;
        setEntries((es) => es.map((e) => (e.id === editingId ? { ...e, ...draft, id: e.id } : e)));
        const ok = await updateLedgerEntry(editingId, draft);
        if (!ok) {
          if (opMonth === currentMonthRef.current) {
            setEntries(prev);
            showError('保存失败，请重试');
          }
          return;
        }
      } else {
        const id = await addLedgerEntry(draft);
        if (id === null) {
          showError('保存失败，请重试');
          return;
        }
        if (opMonth === currentMonthRef.current) {
          setEntries((es) => [{ ...draft, id }, ...es]);
          setViewOffset(0);
          setTab('流水');
          setFilterCat('all');
        }
      }
      setDrawerOpen(false);
      setEditingId(null);
      setEditKeep(null);
      setForm(blankForm());
    } finally {
      setSaving(false);
    }
  }

  function setDaily(v: number | null) {
    setDailyBudget(v);
    writeDailyBudget(key, v);
  }

  function toggleAuto() {
    const next = !autoRecalc;
    setAutoRecalc(next);
    localStorage.setItem(LS_AUTO, next ? '1' : '0');
    if (next) setDaily(null);
  }

  function openBudgetSheet() {
    setBudgetDraft(budget ?? 0);
    setBudgetSheetOpen(true);
  }

  async function saveBudgetAndClose() {
    if (budgetSaving) return;
    setBudgetSaving(true);
    try {
      const ok = await setLedgerBudgetAmount(key, budgetDraft);
      if (!ok) {
        showError('预算保存失败');
        return;
      }
      setBudget(budgetDraft);
      setBudgetError(false);
      setBudgetLoading(false);
      setBudgetSheetOpen(false);
    } finally {
      setBudgetSaving(false);
    }
  }

  const monthBtn: CSSProperties = {
    cursor: 'pointer',
    width: 34,
    height: 34,
    borderRadius: 12,
    background: 'var(--color-bg)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: 15,
  };
  const autoOn = autoRecalc && dailyBudget === null;

  return (
    <>
      <ScreenLayout>
        <BackHeader title="账本" subtitle="our ledger" />

        {errorBanner && (
          <div
            data-testid="ledger-error-banner"
            style={{
              margin: '0 0 12px',
              padding: '10px 14px',
              borderRadius: 12,
              background: 'rgba(156,59,74,0.10)',
              color: 'var(--color-rose-deep)',
              fontSize: 13,
              letterSpacing: 1,
            }}
          >
            {errorBanner}
          </div>
        )}

        {/* ── month gauge ── */}
        <Card style={{ padding: 22 }} data-testid="ledger-month-gauge">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span onClick={() => { setViewOffset(viewOffset - 1); setCalSel(null); setExpandedId(null); }} style={{ ...monthBtn, color: 'var(--color-text-soft)' }}>
              ‹
            </span>
            <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 21, color: 'var(--color-rose-deep)', letterSpacing: 2 }}>{monthLabel}</span>
            <span
              onClick={() => { if (!isCur) { setViewOffset(viewOffset + 1); setCalSel(null); setExpandedId(null); } }}
              style={{ ...monthBtn, cursor: isCur ? 'default' : 'pointer', color: isCur ? '#E3D8D3' : 'var(--color-text-soft)' }}
            >
              ›
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginTop: 18 }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 38, fontWeight: 500, letterSpacing: 1, color: over ? 'var(--color-rose-deep)' : 'var(--color-text)' }}>
                ¥ {fmtAmount(spend, true)}
              </div>
              <div style={{ fontSize: 13, color: 'var(--color-text-mute)', letterSpacing: 2, marginTop: 5 }}>本月共同支出</div>
              {income > 0 && <div style={{ fontSize: 12, color: 'var(--color-green-deep)', marginTop: 8 }}>收入 +¥{fmtAmount(income)}</div>}
              {budgetReady && statusLine && (
                <div style={{ fontSize: 12, color: over ? 'var(--color-rose-deep)' : near ? '#B8862F' : 'var(--color-text-faint)', marginTop: 4, lineHeight: 1.7 }}>
                  {statusLine}
                </div>
              )}
              {!budgetLoading && (budgetError || budget === null) && (
                <div data-testid="ledger-budget-unavailable" style={{ fontSize: 12, color: 'var(--color-rose-deep)', marginTop: 4, lineHeight: 1.7 }}>
                  预算暂不可用
                </div>
              )}
              {budgetLoading && (
                <div style={{ fontSize: 12, color: 'var(--color-text-faint)', marginTop: 4, lineHeight: 1.7 }}>预算加载中…</div>
              )}
            </div>
            {budgetReady ? (
              <svg viewBox="0 0 150 150" style={{ width: 126, height: 126, flexShrink: 0 }} data-testid="ledger-budget-ring">
                <circle cx={75} cy={75} r={62} fill="none" stroke={over ? '#F3DDD9' : near ? '#F3E7CE' : '#F0E3D2'} strokeWidth={10} />
                <circle
                  cx={75}
                  cy={75}
                  r={62}
                  fill="none"
                  stroke={over ? 'var(--color-rose-deep)' : near ? 'var(--color-amber)' : 'var(--color-rose)'}
                  strokeWidth={10}
                  strokeLinecap="round"
                  strokeDasharray={RING_C}
                  strokeDashoffset={over ? 0 : (RING_C * (1 - Math.min(pct, 1))).toFixed(1)}
                  transform="rotate(-90 75 75)"
                  style={over ? { animation: 'livePulse 5s ease-in-out infinite' } : undefined}
                />
                <text x={75} y={71} textAnchor="middle" fill="var(--color-text)" style={{ fontFamily: DISPLAY, fontSize: 23, fontWeight: 600 }}>
                  {Math.round(pct * 100)}%
                </text>
                <text x={75} y={92} textAnchor="middle" fill="var(--color-text-faint)" style={{ fontSize: 11, letterSpacing: 3 }}>
                  预算
                </text>
              </svg>
            ) : (
              <div
                data-testid="ledger-budget-ring-placeholder"
                style={{ width: 126, height: 126, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, color: 'var(--color-text-faint)', textAlign: 'center', lineHeight: 1.6 }}
              >
                {budgetLoading ? '加载中…' : '预算\n暂不可用'}
              </div>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 16, paddingTop: 14, borderTop: '1px solid var(--color-track)', fontSize: 12, color: 'var(--color-text-mute)' }}>
            {budgetReady ? (
              <>
                <span>本月预算 ¥{fmtAmount(budget)}</span>
                <span style={{ color: '#DFD4CF' }}>·</span>
                <span>日均 ¥{fmt1(dayAllow)}{dailyBudget !== null ? '（手动）' : ''}</span>
              </>
            ) : budgetLoading ? (
              <span>预算加载中…</span>
            ) : (
              <span style={{ color: 'var(--color-rose-deep)' }}>预算暂不可用</span>
            )}
            <span
              onClick={openBudgetSheet}
              data-testid="ledger-adjust-budget"
              style={{ cursor: 'pointer', marginLeft: 'auto', color: 'var(--color-rose-deep)', padding: '4px 14px', borderRadius: 999, background: 'rgba(183,110,121,0.10)' }}
            >
              调整
            </span>
          </div>
        </Card>

        {/* ── tabs ── */}
        <div style={{ display: 'flex', gap: 6, background: '#FFFFFF', borderRadius: 16, boxShadow: '0 6px 18px rgba(183,110,121,0.08)', padding: 6 }}>
          {TABS.map((t) => (
            <div
              key={t}
              onClick={() => setTab(t)}
              style={{
                cursor: 'pointer',
                flex: 1,
                textAlign: 'center',
                padding: '9px 0',
                borderRadius: 11,
                background: t === tab ? 'rgba(183,110,121,0.13)' : 'transparent',
                color: t === tab ? 'var(--color-rose-deep)' : 'var(--color-text-faint)',
                fontSize: 14,
                fontWeight: t === tab ? 600 : 400,
                letterSpacing: 2,
                transition: 'background .2s',
              }}
            >
              {t}
            </div>
          ))}
        </div>

        {/* ══════════ 流水 ══════════ */}
        {tab === '流水' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {monthLoading ? (
              <div style={{ padding: '48px 20px', textAlign: 'center', fontSize: 13, color: 'var(--color-text-faint)', letterSpacing: 2 }}>加载中…</div>
            ) : entriesError ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '48px 24px 36px', gap: 10 }}>
                <div style={{ fontSize: 15, color: 'var(--color-rose-deep)', letterSpacing: 2 }}>账目加载失败</div>
                <div style={{ fontSize: 13, color: 'var(--color-text-mute)', textAlign: 'center', lineHeight: 1.7 }}>
                  没有展示任何账目，以免把演示数据当成真实记录
                </div>
                <div
                  onClick={() => void reloadMonth(key)}
                  style={{ cursor: 'pointer', marginTop: 10, padding: '10px 26px', borderRadius: 999, background: 'var(--color-rose)', color: '#FFF9F7', fontSize: 13, letterSpacing: 2 }}
                >
                  重新加载
                </div>
              </div>
            ) : monthEntries.length > 0 ? (
              <>
                <div style={{ display: 'flex', gap: 8, overflowX: 'auto', padding: '2px 2px 6px', margin: '0 -2px' }}>
                  {[{ id: 'all', label: '全部' }, ...LEDGER_CATS.filter((c) => usedCats.includes(c.id)).map((c) => ({ id: c.id, label: `${c.emoji} ${c.name}` }))].map((c) => (
                    <span
                      key={c.id}
                      onClick={() => setFilterCat(c.id)}
                      style={{
                        cursor: 'pointer',
                        flexShrink: 0,
                        padding: '7px 15px',
                        borderRadius: 999,
                        background: filterCat === c.id ? 'var(--color-rose)' : '#FFFFFF',
                        color: filterCat === c.id ? '#FFF9F7' : 'var(--color-text-soft)',
                        fontSize: 13,
                        letterSpacing: 1,
                        boxShadow: '0 4px 12px rgba(183,110,121,0.08)',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {c.label}
                    </span>
                  ))}
                </div>

                {dayGroups.map((g) => (
                  <div key={g.date} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    <div style={{ fontFamily: DISPLAY, fontSize: 13, color: 'var(--color-text-faint)', letterSpacing: 2, padding: '4px 4px 0' }}>{g.label}</div>
                    {g.items.map((e) => {
                      const cat = catOf(e.catId);
                      const who = LEDGER_WHO[e.who];
                      const isInc = e.amount > 0;
                      const subBits = [who.name, e.reason];
                      if (e.mem) subBits.push('关联记忆');
                      if (e.read) subBits.push('关联共读');
                      const open = expandedId === e.id;
                      return (
                        <div
                          key={e.id}
                          data-testid={`ledger-entry-${e.id}`}
                          onClick={() => setExpandedId(open ? null : e.id)}
                          className="card-hover"
                          style={{ cursor: 'pointer', background: '#FFFFFF', borderRadius: 18, boxShadow: '0 6px 20px rgba(183,110,121,0.08)', padding: '14px 16px' }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                            <div style={{ width: 40, height: 40, borderRadius: 14, background: 'var(--color-bg)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 19, flexShrink: 0 }}>
                              {cat.emoji}
                            </div>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontSize: 15, whiteSpace: 'nowrap', overflow: 'hidden', maskImage: 'linear-gradient(90deg,#000 85%,transparent)' }}>{e.title}</div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3, fontSize: 12, color: 'var(--color-text-faint)' }}>
                                <span style={{ width: 6, height: 6, borderRadius: '50%', background: who.color, flexShrink: 0 }} />
                                <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{subBits.join(' · ')}</span>
                              </div>
                            </div>
                            <div style={{ fontFamily: DISPLAY, fontSize: 17, fontWeight: 600, color: isInc ? 'var(--color-green-deep)' : 'var(--color-rose)', flexShrink: 0 }}>
                              {isInc ? '+¥' : '−¥'}
                              {fmtAmount(e.amount)}
                            </div>
                          </div>
                          {open && (
                            <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                              {e.note && <div style={{ fontSize: 13, color: 'var(--color-text-mute)', lineHeight: 1.7 }}>{e.note}</div>}
                              {(e.mem || e.read) && (
                                <div style={{ background: '#F9F3F0', borderRadius: 12, padding: '10px 13px' }}>
                                  <div style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 11, color: 'var(--color-rose-pink)', letterSpacing: 1 }}>
                                    {e.mem ? '关联记忆' : '关联共读'}
                                  </div>
                                  <div style={{ fontSize: 13, color: 'var(--color-text-soft)', marginTop: 4, lineHeight: 1.7 }}>{e.mem || e.read}</div>
                                </div>
                              )}
                              {e.later && <div style={{ fontSize: 12, color: 'var(--color-green-deep)', lineHeight: 1.7 }}>后来：{e.later}</div>}
                              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 2 }}>
                                <span
                                  onClick={(ev) => { ev.stopPropagation(); startEdit(e); }}
                                  style={{ cursor: 'pointer', padding: '6px 18px', borderRadius: 999, background: 'var(--color-bg)', color: 'var(--color-text-soft)', fontSize: 12, letterSpacing: 1 }}
                                >
                                  修改
                                </span>
                                <span
                                  onClick={(ev) => { ev.stopPropagation(); removeEntry(e.id); }}
                                  style={{ cursor: 'pointer', padding: '6px 18px', borderRadius: 999, background: 'rgba(156,59,74,0.08)', color: 'var(--color-rose-deep)', fontSize: 12, letterSpacing: 1 }}
                                >
                                  删除
                                </span>
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                ))}
              </>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '52px 20px 40px' }}>
                <div style={{ position: 'relative', width: 96, height: 68, background: '#F3EAE4', borderRadius: '8px 14px 14px 8px', boxShadow: '0 10px 26px rgba(183,110,121,0.14)' }}>
                  <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 9, background: '#E4D4CC', borderRadius: '8px 0 0 8px' }} />
                  <div style={{ position: 'absolute', right: 16, top: -6, width: 10, height: 34, background: 'var(--color-rose)', borderRadius: '0 0 5px 5px' }} />
                  <div style={{ position: 'absolute', left: -16, top: -14, width: 12, height: 12, borderRadius: '50%', background: '#EFDDBB' }} />
                </div>
                <div style={{ fontSize: 14, color: 'var(--color-text-mute)', letterSpacing: 2, marginTop: 30 }}>这个月还没有留下任何记录</div>
                <div
                  onClick={openDrawer}
                  style={{ cursor: 'pointer', marginTop: 22, padding: '11px 30px', borderRadius: 999, background: 'var(--color-rose)', color: '#FFF9F7', fontSize: 14, letterSpacing: 3, boxShadow: '0 8px 22px rgba(183,110,121,0.28)' }}
                >
                  记第一笔
                </div>
              </div>
            )}
          </div>
        )}

        {/* ══════════ 统计 ══════════ */}
        {tab === '统计' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <Card style={{ padding: 22 }}>
              <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>本月构成</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginTop: 16 }}>
                <svg viewBox="0 0 140 140" style={{ width: 128, height: 128, flexShrink: 0 }}>
                  {donutSegs.map((s, i) => (
                    <circle key={i} cx={70} cy={70} r={54} fill="none" stroke={s.color} strokeWidth={16} strokeDasharray={s.dash} strokeDashoffset={s.offset} transform="rotate(-90 70 70)" />
                  ))}
                  <text x={70} y={67} textAnchor="middle" fill="var(--color-text)" style={{ fontFamily: DISPLAY, fontSize: 17, fontWeight: 600 }}>
                    {donutTop ? `${Math.round((donutTop[1] / (spend || 1)) * 100)}%` : '—'}
                  </text>
                  <text x={70} y={85} textAnchor="middle" fill="var(--color-text-faint)" style={{ fontSize: 10 }}>
                    {donutTop ? catOf(donutTop[0]).name : ''}
                  </text>
                </svg>
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {catTotals.slice(0, 6).map(([id, v]) => (
                    <div key={id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                      <span style={{ width: 8, height: 8, borderRadius: 3, background: catOf(id).color, flexShrink: 0 }} />
                      <span style={{ color: 'var(--color-text-soft)' }}>{catOf(id).name}</span>
                      <span style={{ fontFamily: DISPLAY, color: 'var(--color-text-faint)', marginLeft: 'auto' }}>{Math.round((v / (spend || 1)) * 100)}%</span>
                    </div>
                  ))}
                </div>
              </div>
            </Card>

            <Card style={{ padding: 22 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>最近六个月</span>
                <span style={{ fontFamily: DISPLAY, fontSize: 12, color: 'var(--color-text-faint)' }}>总支出趋势</span>
              </div>
              {trendError ? (
                <div style={{ marginTop: 18, fontSize: 13, color: 'var(--color-text-mute)' }}>趋势暂不可用</div>
              ) : (
                <>
                  <svg viewBox="0 0 340 96" style={{ width: '100%', height: 96, marginTop: 14, overflow: 'visible' }}>
                    <path d={trendPath} fill="none" stroke="var(--color-rose)" strokeWidth={2} strokeLinecap="round" />
                    <circle cx={trendLast[0]} cy={trendLast[1]} r={4} fill="var(--color-rose-deep)" />
                  </svg>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8 }}>
                    {trendLabels.map((m, i) => (
                      <span key={i} style={{ fontFamily: DISPLAY, fontSize: 11, color: i === trendLabels.length - 1 ? 'var(--color-rose-deep)' : 'var(--color-text-fainter)' }}>
                        {m}
                      </span>
                    ))}
                  </div>
                </>
              )}
            </Card>

            <Card style={{ padding: 22 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>分布</span>
                <span style={{ display: 'flex', gap: 4, background: 'var(--color-bg)', borderRadius: 10, padding: 3 }}>
                  {(['week', 'cat'] as const).map((m) => (
                    <span
                      key={m}
                      onClick={() => setBarMode(m)}
                      style={{
                        cursor: 'pointer',
                        padding: '4px 12px',
                        borderRadius: 8,
                        background: barMode === m ? '#FFFFFF' : 'transparent',
                        color: barMode === m ? 'var(--color-rose-deep)' : 'var(--color-text-faint)',
                        fontSize: 12,
                      }}
                    >
                      {m === 'week' ? '按周' : '按分类'}
                    </span>
                  ))}
                </span>
              </div>
              {barMode === 'week' ? (
                <div style={{ display: 'flex', alignItems: 'flex-end', gap: weeks.length > 4 ? 10 : 16, height: 120, marginTop: 18, padding: '0 6px' }}>
                  {weeks.map((v, i) => (
                    <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, justifyContent: 'flex-end', height: '100%' }}>
                      <span style={{ fontFamily: DISPLAY, fontSize: 10, color: 'var(--color-text-faint)' }}>{v ? `¥${fmtAmount(Math.round(v))}` : ''}</span>
                      <div style={{ width: '100%', maxWidth: 34, height: Math.max(Math.round((v / wMax) * 84), v ? 8 : 3), background: v ? (i % 2 ? 'var(--color-amber)' : 'var(--color-rose)') : '#F0E6E2', borderRadius: '7px 7px 3px 3px' }} />
                      <span style={{ fontSize: 10, color: 'var(--color-text-faint)', textAlign: 'center', lineHeight: 1.3 }}>{weekLabels[i]}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13, marginTop: 16 }}>
                  {catTotals.map(([id, v]) => (
                    <div key={id}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
                        <span style={{ color: 'var(--color-text-soft)' }}>
                          {catOf(id).emoji} {catOf(id).name}
                        </span>
                        <span style={{ fontFamily: DISPLAY, color: 'var(--color-text-mute)' }}>¥{fmtAmount(Math.round(v))}</span>
                      </div>
                      <div style={{ height: 6, borderRadius: 3, background: 'var(--color-track)', marginTop: 6, overflow: 'hidden' }}>
                        <div style={{ height: '100%', borderRadius: 3, background: catOf(id).color, width: `${Math.round((v / cMax) * 100)}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <div onClick={() => setAiOpen(!aiOpen)} style={{ cursor: 'pointer', background: 'rgba(138,122,181,0.10)', borderRadius: 22, padding: '18px 20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: 14, fontWeight: 600, letterSpacing: 2, color: '#6E608F' }}>本月观察</span>
                <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 11, color: '#9C8FBB', letterSpacing: 1 }}>{aiOpen ? 'fold' : 'unfold'}</span>
              </div>
              {aiOpen && <div style={{ fontSize: 13, color: '#7A6F94', lineHeight: 1.9, marginTop: 10 }}>{obsText}</div>}
            </div>
          </div>
        )}

        {/* ══════════ 日历 ══════════ */}
        {tab === '日历' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <Card style={{ padding: 22 }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 6 }}>
                {['一', '二', '三', '四', '五', '六', '日'].map((w) => (
                  <span key={w} style={{ textAlign: 'center', fontSize: 11, color: 'var(--color-text-faint)' }}>
                    {w}
                  </span>
                ))}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 6, marginTop: 8 }}>
                {Array.from({ length: (base.getDay() + 6) % 7 }).map((_, i) => (
                  <div key={`lead-${i}`} style={{ height: 46 }} />
                ))}
                {Array.from({ length: dim }, (_, i) => i + 1).map((d) => {
                  const info = byDay[d];
                  const future = isCur && d > todayD;
                  const v = info ? info.spend : 0;
                  let bg = '#F9F5F3';
                  let dColor = future ? '#DFD4CF' : 'var(--color-nav-inactive)';
                  let nColor = 'transparent';
                  if (v > 0) {
                    nColor = 'var(--color-text-mute)';
                    dColor = 'var(--color-text-soft)';
                    if (v < 100) bg = '#F3E7CE';
                    else if (v < 250) bg = '#E8C6C9';
                    else {
                      bg = 'var(--color-rose)';
                      dColor = '#FFFFFF';
                      nColor = 'rgba(255,255,255,0.85)';
                    }
                  }
                  return (
                    <div
                      key={d}
                      onClick={() => setCalSel(d)}
                      style={{
                        cursor: 'pointer',
                        height: 46,
                        borderRadius: 10,
                        background: bg,
                        border: calSel === d ? '2px solid var(--color-rose-deep)' : isCur && d === todayD ? '2px solid var(--color-amber)' : '2px solid transparent',
                        position: 'relative',
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        gap: 1,
                      }}
                    >
                      <span style={{ fontFamily: DISPLAY, fontSize: 13, color: dColor }}>{d}</span>
                      <span style={{ fontSize: 9, color: nColor }}>{v > 0 ? fmtAmount(Math.round(v)) : ''}</span>
                      {info?.income && <span style={{ position: 'absolute', top: 5, right: 5, width: 5, height: 5, borderRadius: '50%', background: 'var(--color-green-deep)' }} />}
                      {info?.mem && <span style={{ position: 'absolute', top: 5, left: 5, width: 5, height: 5, borderRadius: '50%', background: 'var(--color-violet)' }} />}
                    </div>
                  );
                })}
              </div>
              <div style={{ display: 'flex', gap: 14, marginTop: 14, fontSize: 10, color: 'var(--color-text-faint)', flexWrap: 'wrap' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 3, background: '#F3E7CE' }} />低
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 3, background: '#E8C6C9' }} />中
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 3, background: 'var(--color-rose)' }} />高
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--color-green-deep)' }} />有收入
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--color-violet)' }} />有记忆
                </span>
              </div>
            </Card>

            {calSel !== null && byDay[calSel] && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ fontFamily: DISPLAY, fontSize: 13, color: 'var(--color-text-faint)', letterSpacing: 2, padding: '0 4px' }}>
                  {String(base.getMonth() + 1).padStart(2, '0')}.{String(calSel).padStart(2, '0')} {WEEKDAY_EN[new Date(base.getFullYear(), base.getMonth(), calSel).getDay()]}
                </div>
                {byDay[calSel].items.map((e) => (
                  <div key={e.id} style={{ background: '#FFFFFF', borderRadius: 16, boxShadow: '0 6px 20px rgba(183,110,121,0.08)', padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
                    <span style={{ fontSize: 17 }}>{catOf(e.catId).emoji}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 14, whiteSpace: 'nowrap', overflow: 'hidden', maskImage: 'linear-gradient(90deg,#000 85%,transparent)' }}>{e.title}</div>
                      <div style={{ fontSize: 11, color: 'var(--color-text-faint)', marginTop: 2 }}>
                        {LEDGER_WHO[e.who].name} · {e.reason}
                      </div>
                    </div>
                    <span style={{ fontFamily: DISPLAY, fontSize: 15, fontWeight: 600, color: e.amount > 0 ? 'var(--color-green-deep)' : 'var(--color-rose)' }}>
                      {e.amount > 0 ? '+¥' : '−¥'}
                      {fmtAmount(e.amount)}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {calSel !== null && !byDay[calSel] && (
              <div style={{ textAlign: 'center', fontSize: 12, color: 'var(--color-text-fainter)', letterSpacing: 2, padding: '10px 0' }}>这一天很安静，没有花钱</div>
            )}
          </div>
        )}

        {/* ══════════ 探索 ══════════ */}
        {tab === '探索' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Card style={{ padding: 20, cursor: 'pointer' }} onClick={() => setLaterOpen(!laterOpen)}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>后来</span>
                <span style={{ color: 'var(--color-text-fainter)', fontSize: 16 }}>{laterOpen ? '∨' : '›'}</span>
              </div>
              <div style={{ fontSize: 13, color: 'var(--color-text-faint)', marginTop: 6, lineHeight: 1.7 }}>看一笔收入后来变成了什么</div>
              {laterOpen && (
                <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column' }}>
                  {laterChains.length ? (
                    laterChains.map((chain, ci) => (
                      <div key={chain.head.id} style={{ display: 'flex', flexDirection: 'column', marginTop: ci ? 18 : 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                          <span style={{ width: 11, height: 11, borderRadius: '50%', background: 'var(--color-green-deep)', flexShrink: 0 }} />
                          <span style={{ fontSize: 14, flex: 1 }}>{chain.head.title}</span>
                          <span style={{ fontFamily: DISPLAY, fontSize: 14, fontWeight: 600, color: 'var(--color-green-deep)' }}>+¥{fmtAmount(chain.head.amount)}</span>
                        </div>
                        {chain.spends.map((s, i) => (
                          <div key={i} style={{ display: 'flex', flexDirection: 'column' }}>
                            <div style={{ width: 1, height: 16, background: '#E4D4CC', marginLeft: 5 }} />
                            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                              <span style={{ width: 11, height: 11, borderRadius: '50%', background: 'var(--color-rose)', flexShrink: 0 }} />
                              <span style={{ fontSize: 14, flex: 1 }}>{s}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    ))
                  ) : (
                    <div style={{ fontSize: 13, color: 'var(--color-text-faint)', lineHeight: 1.8 }}>还没有带「后来」的收入。记一笔收入时，写下它后来变成了什么。</div>
                  )}
                  <div style={{ fontSize: 12, color: 'var(--color-text-faint)', lineHeight: 1.8, marginTop: 14, fontStyle: 'italic' }}>这不是流水，是生活被钱怎么托住的路径。</div>
                </div>
              )}
            </Card>

            <Card style={{ padding: 20 }}>
              <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>共同偏好</span>
              <div style={{ fontSize: 13, color: 'var(--color-text-faint)', marginTop: 6, lineHeight: 1.7 }}>我们最近总在为什么花钱</div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
                {prefChips.length ? (
                  prefChips.map(([r, n]) => (
                    <span key={r} style={{ padding: '6px 14px', borderRadius: 999, background: 'var(--color-bg)', fontSize: 12, color: 'var(--color-text-soft)' }}>
                      {r} ×{n}
                    </span>
                  ))
                ) : (
                  <span style={{ fontSize: 12, color: 'var(--color-text-fainter)' }}>本月还没有支出</span>
                )}
              </div>
            </Card>

            <Card style={{ padding: 20, opacity: 0.72 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2 }}>记忆账本</span>
                <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 11, color: 'var(--color-text-fainter)', letterSpacing: 1 }}>soon</span>
              </div>
              <div style={{ fontSize: 13, color: 'var(--color-text-faint)', marginTop: 6, lineHeight: 1.7 }}>从 Memory 反向看消费</div>
            </Card>
          </div>
        )}
      </ScreenLayout>

      {/* ── fab ── */}
      {!drawerOpen && !budgetSheetOpen && tab === '流水' && monthEntries.length > 0 && (
        <div
          style={{
            position: 'fixed',
            bottom: 72,
            left: '50%',
            transform: 'translateX(-50%)',
            width: '100%',
            maxWidth: 452,
            padding: '14px 20px 10px',
            background: 'linear-gradient(180deg,rgba(247,241,238,0),rgba(247,241,238,0.94) 45%)',
            display: 'flex',
            justifyContent: 'center',
            pointerEvents: 'none',
            zIndex: 49,
          }}
        >
          <div
            onClick={openDrawer}
            style={{ cursor: 'pointer', pointerEvents: 'auto', padding: '13px 36px', borderRadius: 999, background: 'var(--color-rose)', color: '#FFF9F7', fontSize: 15, letterSpacing: 3, boxShadow: '0 10px 26px rgba(183,110,121,0.32)' }}
          >
            ＋ 记一笔
          </div>
        </div>
      )}

      {/* ══════════ 记一笔抽屉 ══════════ */}
      {drawerOpen && (
        <>
          <div onClick={() => setDrawerOpen(false)} style={overlayStyle} />
          <div style={{ ...sheetShell, height: '85%', padding: '14px 20px 30px', overflowY: 'auto' }} data-testid="ledger-drawer">
            <div style={grabber} />

            <div style={{ display: 'flex', gap: 4, background: '#F1E7E2', borderRadius: 14, padding: 4, marginTop: 18, width: 'fit-content' }}>
              {(['exp', 'inc'] as const).map((t) => (
                <div
                  key={t}
                  onClick={() => patchForm({ type: t })}
                  style={{
                    cursor: 'pointer',
                    textAlign: 'center',
                    padding: '8px 22px',
                    borderRadius: 10,
                    background: form.type === t ? '#FFFFFF' : 'transparent',
                    color: form.type === t ? (t === 'inc' ? 'var(--color-green-deep)' : 'var(--color-rose-deep)') : 'var(--color-text-faint)',
                    fontSize: 14,
                    letterSpacing: 3,
                  }}
                >
                  {t === 'exp' ? '支出' : '收入'}
                </div>
              ))}
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14, background: '#FFFFFF', borderRadius: 18, padding: '15px 18px', boxShadow: '0 6px 18px rgba(183,110,121,0.08)' }}>
              <span style={{ fontFamily: DISPLAY, fontSize: 24, color: form.type === 'inc' ? 'var(--color-green-deep)' : 'var(--color-rose)' }}>¥</span>
              <input
                value={form.amount}
                onChange={(ev) => {
                  let v = ev.target.value.replace(/[^\d.]/g, '');
                  const i = v.indexOf('.');
                  if (i >= 0) v = v.slice(0, i + 1) + v.slice(i + 1).replace(/\./g, '').slice(0, 2);
                  if (v.replace('.', '').length > 8) return;
                  patchForm({ amount: v });
                }}
                inputMode="decimal"
                placeholder="0.00"
                style={{ flex: 1, minWidth: 0, border: 'none', background: 'transparent', fontFamily: DISPLAY, fontSize: 34, fontWeight: 500, color: form.type === 'inc' ? 'var(--color-green-deep)' : 'var(--color-rose)', padding: 0, outline: 'none' }}
              />
              {form.amount !== '' && (
                <span
                  onClick={() => patchForm({ amount: '' })}
                  style={{ cursor: 'pointer', width: 28, height: 28, borderRadius: '50%', background: '#F1E7E2', color: 'var(--color-text-mute)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 13, flexShrink: 0 }}
                >
                  ✕
                </span>
              )}
            </div>
            <div style={{ fontSize: 12, color: todayLeft >= 0 ? 'var(--color-text-mute)' : 'var(--color-rose-deep)', marginTop: 10, paddingLeft: 4 }}>
              {budgetReady
                ? todayLeft >= 0
                  ? `今日还可花 ¥${fmt1(todayLeft)}`
                  : `今日已超 ¥${fmt1(-todayLeft)}`
                : ''}
            </div>

            {form.type === 'exp' && (
              <>
                <div style={{ ...sectionLabel, marginTop: 22 }}>分类</div>
                <div style={{ display: 'flex', gap: 8, overflowX: 'auto', padding: '10px 2px 4px', margin: '0 -2px' }}>
                  {LEDGER_CATS.filter((c) => c.id !== 'income').map((c) => (
                    <span
                      key={c.id}
                      onClick={() => patchForm({ catId: c.id })}
                      style={{
                        cursor: 'pointer',
                        flexShrink: 0,
                        padding: '7px 14px',
                        borderRadius: 999,
                        background: form.catId === c.id ? 'var(--color-rose)' : '#FFFFFF',
                        color: form.catId === c.id ? '#FFF9F7' : 'var(--color-text-soft)',
                        fontSize: 13,
                        whiteSpace: 'nowrap',
                        boxShadow: '0 3px 10px rgba(183,110,121,0.06)',
                      }}
                    >
                      {c.emoji} {c.name}
                    </span>
                  ))}
                </div>
              </>
            )}

            <div style={sectionLabel}>谁想要的</div>
            <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
              {(Object.entries(LEDGER_WHO) as Array<[LedgerWho, { name: string; color: string }]>).map(([id, w]) => (
                <span
                  key={id}
                  onClick={() => patchForm({ who: id })}
                  style={{
                    cursor: 'pointer',
                    flex: 1,
                    textAlign: 'center',
                    padding: '9px 0',
                    borderRadius: 12,
                    background: form.who === id ? 'rgba(183,110,121,0.10)' : '#FFFFFF',
                    color: form.who === id ? 'var(--color-rose-deep)' : 'var(--color-text-mute)',
                    fontSize: 13,
                    letterSpacing: 2,
                    border: form.who === id ? `1.5px solid ${w.color}` : '1.5px solid transparent',
                  }}
                >
                  {w.name}
                </span>
              ))}
            </div>

            {form.type === 'exp' && (
              <>
                <div style={sectionLabel}>购买原因</div>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
                  {LEDGER_REASONS.map((r) => (
                    <span
                      key={r}
                      onClick={() => patchForm({ reason: r })}
                      style={{
                        cursor: 'pointer',
                        padding: '7px 14px',
                        borderRadius: 999,
                        background: form.reason === r ? 'var(--color-rose)' : '#F1E7E2',
                        color: form.reason === r ? '#FFF9F7' : 'var(--color-text-mute)',
                        fontSize: 12,
                      }}
                    >
                      {r}
                    </span>
                  ))}
                </div>
              </>
            )}

            <div style={sectionLabel}>备注</div>
            <input
              value={form.title}
              onChange={(ev) => patchForm({ title: ev.target.value })}
              placeholder="买了什么、为什么买"
              style={{ width: '100%', marginTop: 10, background: '#FFFFFF', border: 'none', borderRadius: 12, padding: '12px 14px', fontSize: 14, color: 'var(--color-text)', boxShadow: '0 3px 10px rgba(183,110,121,0.06)', fontFamily: 'var(--font-serif-cn)', outline: 'none' }}
            />

            <div style={sectionLabel}>关联</div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
              <span
                onClick={() => patchForm({ memOpen: !form.memOpen })}
                style={{ cursor: 'pointer', padding: '8px 14px', borderRadius: 12, background: form.mem ? 'rgba(183,110,121,0.12)' : 'transparent', color: form.mem ? 'var(--color-rose-deep)' : 'var(--color-text-mute)', fontSize: 12, border: '1px dashed #D9C6BF' }}
              >
                🌙 关联记忆
              </span>
              <span
                onClick={() => {
                  if (form.readOn) patchForm({ readOn: false, read: undefined });
                  else patchForm({ readOn: true, read: form.read || readRef || undefined });
                }}
                style={{ cursor: 'pointer', padding: '8px 14px', borderRadius: 12, background: form.readOn ? 'rgba(183,110,121,0.12)' : 'transparent', color: form.readOn ? 'var(--color-rose-deep)' : 'var(--color-text-mute)', fontSize: 12, border: '1px dashed #D9C6BF' }}
              >
                📖 关联共读
              </span>
              <span
                onClick={() => patchForm({ laterOpen: !form.laterOpen })}
                style={{ cursor: 'pointer', padding: '8px 14px', borderRadius: 12, background: form.laterOpen || !!form.later ? 'rgba(183,110,121,0.12)' : 'transparent', color: form.laterOpen || !!form.later ? 'var(--color-rose-deep)' : 'var(--color-text-mute)', fontSize: 12, border: '1px dashed #D9C6BF' }}
              >
                ↳ 添加「后来」
              </span>
            </div>
            {form.mem && !form.memOpen && (
              <div style={{ background: '#FFFFFF', borderRadius: 12, padding: '10px 13px', marginTop: 12, display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 10, color: 'var(--color-rose-pink)', letterSpacing: 1 }}>已关联记忆</div>
                  <div style={{ fontSize: 13, color: 'var(--color-text-soft)', marginTop: 3, lineHeight: 1.6 }}>{form.mem}</div>
                </div>
                <span
                  onClick={() => patchForm({ mem: undefined, memSel: null })}
                  style={{ cursor: 'pointer', flexShrink: 0, fontSize: 12, color: 'var(--color-rose-deep)', padding: '4px 8px' }}
                >
                  取消
                </span>
              </div>
            )}
            {form.memOpen && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
                {form.mem && form.memSel === null && (
                  <div style={{ background: '#F9F3F0', borderRadius: 12, padding: '10px 13px', border: '1.5px solid var(--color-rose)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                      <div style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 10, color: 'var(--color-rose-pink)', letterSpacing: 1 }}>已关联（不在最近候选）</div>
                      <span onClick={() => patchForm({ mem: undefined, memSel: null })} style={{ cursor: 'pointer', fontSize: 12, color: 'var(--color-rose-deep)' }}>取消</span>
                    </div>
                    <div style={{ fontSize: 13, color: 'var(--color-text-soft)', marginTop: 3, lineHeight: 1.6 }}>{form.mem}</div>
                  </div>
                )}
                {memPicks.length ? (
                  memPicks.map((m, i) => (
                    <div
                      key={i}
                      onClick={() => {
                        if (form.memSel === i) patchForm({ memSel: null, mem: undefined });
                        else patchForm({ memSel: i, mem: m.text });
                      }}
                      style={{ cursor: 'pointer', background: '#FFFFFF', borderRadius: 12, padding: '10px 13px', border: form.memSel === i || form.mem === m.text ? '1.5px solid var(--color-rose)' : '1.5px solid transparent' }}
                    >
                      <div style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 10, color: 'var(--color-rose-pink)', letterSpacing: 1 }}>MEMORY · {m.date}</div>
                      <div style={{ fontSize: 13, color: 'var(--color-text-soft)', marginTop: 3, lineHeight: 1.6 }}>{m.text}</div>
                    </div>
                  ))
                ) : (
                  <div style={{ fontSize: 12, color: 'var(--color-text-fainter)', padding: '4px 2px' }}>最近还没有可关联的记忆</div>
                )}
              </div>
            )}
            {form.readOn && (
              <div style={{ background: '#FFFFFF', borderRadius: 12, padding: '10px 13px', marginTop: 12 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                  <div style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 10, color: 'var(--color-rose-pink)', letterSpacing: 1 }}>关联共读</div>
                  <span
                    onClick={() => patchForm({ readOn: false, read: undefined })}
                    style={{ cursor: 'pointer', fontSize: 12, color: 'var(--color-rose-deep)' }}
                  >
                    取消
                  </span>
                </div>
                <div style={{ fontSize: 13, color: 'var(--color-text-soft)', marginTop: 3 }}>{form.read || readRef || '暂无进行中的共读'}</div>
                {form.read && readRef && form.read !== readRef && (
                  <div
                    onClick={() => patchForm({ read: readRef })}
                    style={{ cursor: 'pointer', marginTop: 8, fontSize: 12, color: 'var(--color-text-mute)' }}
                  >
                    改为当前共读：{readRef}
                  </div>
                )}
              </div>
            )}
            {form.laterOpen && (
              <input
                value={form.later}
                onChange={(ev) => patchForm({ later: ev.target.value })}
                placeholder="这笔钱后来变成了什么"
                style={{ width: '100%', marginTop: 12, background: '#FFFFFF', border: 'none', borderRadius: 12, padding: '12px 14px', fontSize: 13, color: 'var(--color-text)', boxShadow: '0 3px 10px rgba(183,110,121,0.06)', fontFamily: 'var(--font-serif-cn)', outline: 'none' }}
              />
            )}

            <div
              data-testid="ledger-save-button"
              onClick={() => { if (!saving) void saveEntry(); }}
              style={{
                cursor: saving ? 'default' : 'pointer',
                marginTop: 26,
                textAlign: 'center',
                padding: '14px 0',
                borderRadius: 16,
                background: saving ? '#D9C6BF' : parseFloat(form.amount) > 0 ? (form.type === 'inc' ? 'var(--color-green-deep)' : 'var(--color-rose)') : '#D9C6BF',
                color: '#FFF9F7',
                fontSize: 15,
                letterSpacing: 4,
                boxShadow: '0 10px 26px rgba(183,110,121,0.28)',
                opacity: saving ? 0.7 : 1,
              }}
            >
              {saving ? '保存中…' : editingId !== null ? '保存修改' : '记下这一笔'}
            </div>
          </div>
        </>
      )}

      {/* ══════════ 预算抽屉 ══════════ */}
      {budgetSheetOpen && (
        <>
          <div onClick={() => setBudgetSheetOpen(false)} style={overlayStyle} />
          <div style={{ ...sheetShell, padding: '14px 20px 34px' }} data-testid="ledger-budget-sheet">
            <div style={grabber} />
            <div style={{ fontSize: 16, fontWeight: 600, letterSpacing: 3, marginTop: 20 }}>预算</div>

            <div style={{ ...sectionLabel, marginTop: 20 }}>本月预算</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, background: '#FFFFFF', borderRadius: 14, padding: '12px 16px', boxShadow: '0 4px 12px rgba(183,110,121,0.07)' }}>
              <span style={{ fontFamily: DISPLAY, fontSize: 17, color: 'var(--color-text-faint)' }}>¥</span>
              <input
                data-testid="ledger-budget-input"
                value={String(budgetDraft)}
                onChange={(ev) => {
                  const v = parseInt(ev.target.value.replace(/[^\d]/g, ''), 10);
                  setBudgetDraft(Number.isNaN(v) ? 0 : v);
                }}
                inputMode="numeric"
                style={{ flex: 1, minWidth: 0, border: 'none', background: 'transparent', fontFamily: DISPLAY, fontSize: 22, color: 'var(--color-text)', padding: 0, outline: 'none' }}
              />
            </div>

            <div style={sectionLabel}>每日预算</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, background: '#FFFFFF', borderRadius: 14, padding: '12px 16px', boxShadow: '0 4px 12px rgba(183,110,121,0.07)', opacity: autoOn ? 0.6 : 1 }}>
              <span style={{ fontFamily: DISPLAY, fontSize: 17, color: 'var(--color-text-faint)' }}>¥</span>
              <input
                value={dailyBudget !== null ? String(dailyBudget) : ''}
                onChange={(ev) => {
                  const raw = ev.target.value.replace(/[^\d]/g, '');
                  setDaily(raw === '' ? null : parseInt(raw, 10));
                }}
                inputMode="numeric"
                placeholder={budgetReady ? `自动 · ¥${fmt1(dayAllow)}` : '自动'}
                style={{ flex: 1, minWidth: 0, border: 'none', background: 'transparent', fontFamily: DISPLAY, fontSize: 22, color: 'var(--color-text)', padding: 0, outline: 'none' }}
              />
            </div>

            <div onClick={toggleAuto} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, marginTop: 20 }}>
              <span style={{ width: 40, height: 24, borderRadius: 12, background: autoOn ? 'var(--color-rose)' : '#E4D4CC', position: 'relative', transition: 'background .2s', flexShrink: 0 }}>
                <span style={{ position: 'absolute', top: 3, left: autoOn ? 19 : 3, width: 18, height: 18, borderRadius: '50%', background: '#FFFFFF', boxShadow: '0 2px 6px rgba(74,63,60,0.2)', transition: 'left .2s' }} />
              </span>
              <span style={{ fontSize: 13, color: 'var(--color-text-soft)' }}>自动按剩余天数重算每日预算</span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--color-text-faint)', marginTop: 12, lineHeight: 1.7 }}>
              {budgetReady
                ? isCur
                  ? `本月还剩 ${daysLeft} 天 · 日均 ¥${fmt1(dayAllow)}${dailyBudget !== null ? '（手动设定，仅本月）' : '（自动重算）'}`
                  : dailyBudget !== null
                    ? `历史月手动日预算 ¥${fmt1(dailyBudget)}（仅 ${monthLabel}）`
                    : `按整月 ${dim} 天平均计算 · 日均 ¥${fmt1(dayAllow)}`
                : '预算加载完成后才能计算日均额度'}
            </div>

            <div
              data-testid="ledger-budget-save-button"
              onClick={() => { if (!budgetSaving) void saveBudgetAndClose(); }}
              style={{ cursor: budgetSaving ? 'default' : 'pointer', marginTop: 24, textAlign: 'center', padding: '13px 0', borderRadius: 16, background: 'var(--color-rose)', color: '#FFF9F7', fontSize: 14, letterSpacing: 4, boxShadow: '0 10px 26px rgba(183,110,121,0.28)', opacity: budgetSaving ? 0.7 : 1 }}
            >
              {budgetSaving ? '保存中…' : '完成'}
            </div>
          </div>
        </>
      )}
    </>
  );
}
