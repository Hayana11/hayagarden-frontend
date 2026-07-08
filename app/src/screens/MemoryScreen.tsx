import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { useEffect, useRef, useState } from 'react';
import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { DragScrollRow } from '../components/DragScrollRow';
import { useMemoryLibrary } from '../hooks/useMemoryLibrary';
import { dateKey, seeded } from '../lib/format';
import {
  constellationColor,
  formatDateDot,
  memoryState,
  moonInfo,
  relativeTime,
  tagColor,
  titleColor,
  weekdayCN,
  weightDot,
  type MemoryState,
  type MoonInfo,
} from '../lib/memoryLib';
import type { MemoryEntry, MemoryWeight } from '../types';

type ViewMode = 'time' | 'topic' | 'star';
type StateFilter = MemoryState | 'all';
type Chip = { type: 'tag' | 'who'; value: string };
type DrawerFrame = { type: 'day'; date: string } | { type: 'topic'; key: string } | { type: 'mem'; id: number };

const VIEW_TABS: { key: ViewMode; icon: string; label: string }[] = [
  { key: 'time', icon: '🌓', label: '月相' },
  { key: 'topic', icon: '🏷', label: '主题' },
  { key: 'star', icon: '🌌', label: '星图' },
];
const STATE_LABELS: [Exclude<StateFilter, 'all'>, string][] = [
  ['core', '核心'],
  ['long', '长期'],
  ['short', '短期'],
  ['inbox', '待消化'],
];
const MONTH_LABEL = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const MONTH_CN = ['一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月'];
const STAR_ZOOM_MIN = 0.65;
const STAR_ZOOM_MAX = 2.5;

function clampStarZoom(z: number) {
  return Math.min(STAR_ZOOM_MAX, Math.max(STAR_ZOOM_MIN, +z.toFixed(2)));
}

function pointerDist(a: { x: number; y: number }, b: { x: number; y: number }) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

type StarPointer = { x: number; y: number };
type StarGesture =
  | { kind: 'pending'; x: number; y: number; ox: number; oy: number; starId?: number }
  | { kind: 'pan'; x: number; y: number; ox: number; oy: number }
  | { kind: 'pinch'; startDist: number; startZoom: number; startPan: { x: number; y: number }; startMid: StarPointer; moved: boolean };

type ObserveMode = 'none' | 'weight' | 'topic' | 'who' | 'recent';
type TelescopeStep = 'menu' | 'topic' | 'who' | 'year';
const TELESCOPE_ITEMS: { key: 'weight' | 'topic' | 'who' | 'year' | 'recent'; icon: string; label: string }[] = [
  { key: 'weight', icon: '🌕', label: '高亮重要记忆' },
  { key: 'topic', icon: '🏷', label: '高亮某个主题' },
  { key: 'who', icon: '🧑', label: '高亮某个人' },
  { key: 'year', icon: '📅', label: '回到某一年' },
  { key: 'recent', icon: '✨', label: '查看最近新增' },
];
function toTs(dateStr: string): number {
  return new Date(dateStr).getTime();
}

export function MemoryScreen() {
  const library = useMemoryLibrary();
  const now = new Date();

  const [view, setView] = useState<ViewMode>('time');
  const [query, setQuery] = useState('');
  const [chips, setChips] = useState<Chip[]>([]);
  const [stack, setStack] = useState<DrawerFrame[]>([]);
  const [isDesktop, setIsDesktop] = useState(false);
  const [fState, setFState] = useState<StateFilter>('all');
  const [fTopic, setFTopic] = useState<string>('all');
  const [zoom, setZoom] = useState(1);
  const [telescopeOpen, setTelescopeOpen] = useState(false);
  const [telescopeStep, setTelescopeStep] = useState<TelescopeStep>('menu');
  const [observeMode, setObserveMode] = useState<ObserveMode>('none');
  const [observeValue, setObserveValue] = useState<string | null>(null);
  const [replayPct, setReplayPct] = useState(100);
  const [rippleId, setRippleId] = useState<number | null>(null);
  const [starPan, setStarPan] = useState({ x: 0, y: 0 });
  const starGestureRef = useRef<StarGesture | null>(null);
  const starPointersRef = useRef(new Map<number, StarPointer>());
  const zoomRef = useRef(1);
  const starPanRef = useRef({ x: 0, y: 0 });

  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);
  useEffect(() => {
    starPanRef.current = starPan;
  }, [starPan]);

  useEffect(() => {
    const onResize = () => setIsDesktop(window.innerWidth >= 900);
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    if (view !== 'star') {
      setStarPan({ x: 0, y: 0 });
      setZoom(1);
    }
  }, [view]);

  if (!library) {
    return (
      <ScreenLayout>
        <BackHeader title="记忆库" />
      </ScreenLayout>
    );
  }

  const { topics, entries } = library;
  const topicByKey = new Map(topics.map((t) => [t.key, t]));

  const whoChips = chips.filter((c) => c.type === 'who');
  const tagChips = chips.filter((c) => c.type === 'tag');
  const whoOk = (m: MemoryEntry) => whoChips.every((c) => m.who === c.value);
  const tagOk = (m: MemoryEntry) => tagChips.every((c) => m.tags.includes(c.value));
  const stateOk = (m: MemoryEntry) => fState === 'all' || memoryState(m.weight) === fState;
  const topicOk = (m: MemoryEntry) => fTopic === 'all' || m.topics.includes(fTopic);
  const facetScope = (m: MemoryEntry) => stateOk(m) && topicOk(m) && whoOk(m);
  const filtered = entries.filter((m) => facetScope(m) && tagOk(m));
  const stateCountOk = (m: MemoryEntry) => topicOk(m) && whoOk(m) && tagOk(m);
  const topicCountOk = (m: MemoryEntry) => stateOk(m) && whoOk(m) && tagOk(m);

  function pushFrame(frame: DrawerFrame) {
    setStack((s) => [...s, frame]);
    setQuery('');
  }
  function popFrame() {
    setStack((s) => s.slice(0, -1));
  }
  function closeDrawer() {
    setStack([]);
  }
  function toggleTag(tag: string) {
    setChips((s) =>
      s.some((c) => c.type === 'tag' && c.value === tag)
        ? s.filter((c) => !(c.type === 'tag' && c.value === tag))
        : [...s, { type: 'tag', value: tag }],
    );
  }
  function removeTag(tag: string) {
    setChips((s) => s.filter((c) => !(c.type === 'tag' && c.value === tag)));
  }
  function addWhoChip(who: string) {
    setChips((s) => (s.some((c) => c.type === 'who' && c.value === who) ? s : [...s, { type: 'who', value: who }]));
  }
  function removeWho(who: string) {
    setChips((s) => s.filter((c) => !(c.type === 'who' && c.value === who)));
  }
  function clearFilters() {
    setChips([]);
    setFState('all');
    setFTopic('all');
  }

  const hasFacetFilters = fState !== 'all' || fTopic !== 'all' || chips.length > 0;
  const activeStateLabel = fState !== 'all' ? STATE_LABELS.find(([k]) => k === fState)?.[1] : null;
  const activeTopicMeta = fTopic !== 'all' ? topicByKey.get(fTopic) : null;

  // ── search suggestions ──
  const q = query.trim();
  const showSuggest = q.length > 0 && stack.length === 0;
  type Suggest = { icon: string; iconColor: string; pre: string; hit: string; post: string; meta: string; pick: () => void };
  const suggestGroups: { label: string; items: Suggest[] }[] = [];
  if (q) {
    const ql = q.toLowerCase();
    const highlight = (t: string) => {
      const i = t.toLowerCase().indexOf(ql);
      return i < 0 ? { pre: t, hit: '', post: '' } : { pre: t.slice(0, i), hit: t.slice(i, i + q.length), post: t.slice(i + q.length) };
    };
    const memHits: Suggest[] = entries
      .filter((m) => (m.summaryTitle + m.content + m.tags.join('') + m.who).toLowerCase().includes(ql))
      .slice(0, 4)
      .map((m) => ({ icon: '●', iconColor: '#D9C6C0', meta: formatDateDot(m.date), pick: () => pushFrame({ type: 'mem', id: m.id }), ...highlight(m.summaryTitle) }));
    const topicHits: Suggest[] = topics
      .filter((t) => (t.name + t.desc).toLowerCase().includes(ql))
      .slice(0, 3)
      .map((t) => ({
        icon: t.emoji,
        iconColor: '#8C7B76',
        meta: `${entries.filter((m) => m.topics.includes(t.key)).length} 条`,
        pick: () => pushFrame({ type: 'topic', key: t.key }),
        ...highlight(t.name),
      }));
    const allTags = [...new Set(entries.flatMap((m) => m.tags))];
    const tagHits: Suggest[] = allTags
      .filter((t) => t.toLowerCase().includes(ql))
      .slice(0, 4)
      .map((t) => ({
        icon: '#',
        iconColor: tagColor(t).color,
        meta: '筛选',
        pick: () => {
          setQuery('');
          toggleTag(t);
        },
        ...highlight(t),
      }));
    const whoHits: Suggest[] = ['费佳', '哈娅']
      .filter((w) => w.includes(q))
      .map((w) => ({
        icon: '@',
        iconColor: '#8A7AB5',
        meta: '筛选',
        pick: () => {
          setQuery('');
          addWhoChip(w);
        },
        ...highlight(w),
      }));
    if (memHits.length) suggestGroups.push({ label: '记忆', items: memHits });
    if (topicHits.length) suggestGroups.push({ label: '主题', items: topicHits });
    if (tagHits.length || whoHits.length) suggestGroups.push({ label: '标签与人物', items: [...tagHits, ...whoHits] });
  }
  const suggestEmpty = q.length > 0 && suggestGroups.length === 0;

  // ── unified filter console ──
  const stateTabs = STATE_LABELS.map(([key, label]) => ({
    key,
    label,
    active: fState === key,
    n: entries.filter((m) => stateCountOk(m) && memoryState(m.weight) === key).length,
    pick: () => setFState((cur) => (cur === key ? 'all' : key)),
  }));

  const topicOrder = topics
    .map((t) => ({ topic: t, total: entries.filter((m) => m.topics.includes(t.key)).length }))
    .filter((x) => x.total > 0)
    .sort((a, b) => b.total - a.total)
    .map((x) => x.topic);

  const topicTabs = topicOrder.map((t) => ({
    key: t.key,
    label: t.name,
    emoji: t.emoji as string | null,
    active: fTopic === t.key,
    n: entries.filter((m) => topicCountOk(m) && m.topics.includes(t.key)).length,
    pick: () => setFTopic((cur) => (cur === t.key ? 'all' : t.key)),
  }));

  const tagFreq = new Map<string, number>();
  entries.filter(facetScope).forEach((m) => {
    m.tags.forEach((tag) => tagFreq.set(tag, (tagFreq.get(tag) ?? 0) + 1));
  });
  tagChips.forEach((c) => {
    if (!tagFreq.has(c.value)) tagFreq.set(c.value, 0);
  });
  const tagRow = [...tagFreq.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'))
    .slice(0, 16)
    .map(([label, n]) => {
      const col = tagColor(label);
      const active = tagChips.some((c) => c.value === label);
      const stale = n === 0;
      return {
        label,
        n,
        color: stale && !active ? '#C4B4AF' : col.color,
        bg: active ? col.bg : '#FFFFFF',
        border: active ? col.color : '#EFE3DE',
        bold: active,
        stale,
        pick: () => toggleTag(label),
      };
    });
  const showTagRow = tagRow.length > 0;

  // ── 月相（时间线） ──
  const sortedFiltered = [...filtered].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
  const dayGroups = new Map<string, MemoryEntry[]>();
  sortedFiltered.forEach((m) => {
    if (!dayGroups.has(m.date)) dayGroups.set(m.date, []);
    dayGroups.get(m.date)!.push(m);
  });
  type TimeItem =
    | { kind: 'month'; num: string; label: string; cn: string; monthCount: number; monthCore: number }
    | {
        kind: 'day';
        date: string;
        dayNum: string;
        monthCn: string;
        weekday: string;
        countLabel: string;
        isToday: boolean;
        hasCore: boolean;
        coreN: number;
        moon: MoonInfo;
        rows: { size: number; bg: string; border: string; time: string; title: string; titleColor: string; preview: string; pick: () => void }[];
        pick: () => void;
      };
  const timeItems: TimeItem[] = [];
  const seenMonths = new Set<string>();
  const todayKey = dateKey(now);
  for (const [date, mems] of dayGroups) {
    const monthOf = date.slice(0, 7);
    const monthIndex = parseInt(date.slice(5, 7), 10) - 1;
    if (!seenMonths.has(monthOf)) {
      seenMonths.add(monthOf);
      const monthMems = sortedFiltered.filter((m) => m.date.slice(0, 7) === monthOf);
      const monthCore = monthMems.filter((m) => m.weight === 5).length;
      timeItems.push({ kind: 'month', num: date.slice(5, 7), label: `${MONTH_LABEL[monthIndex]} ${date.slice(0, 4)}`, cn: MONTH_CN[monthIndex], monthCount: monthMems.length, monthCore });
    }
    const sortedMems = [...mems].sort((a, b) => (a.time < b.time ? 1 : -1));
    const coreN = mems.filter((m) => m.weight === 5).length;
    const maxWeight = Math.max(...mems.map((m) => m.weight)) as MemoryWeight;
    timeItems.push({
      kind: 'day',
      date,
      dayNum: date.slice(8, 10),
      monthCn: `${date.slice(5, 7)}月`,
      weekday: weekdayCN(date),
      countLabel: `${mems.length} 条记忆`,
      isToday: date === todayKey,
      hasCore: coreN > 0,
      coreN,
      moon: moonInfo(maxWeight),
      rows: sortedMems.map((m) => ({
        ...weightDot(m.weight),
        time: m.time,
        title: m.summaryTitle ?? m.title,
        titleColor: titleColor(m.weight),
        preview: (m.content || '').replace(/\s+/g, ' ').trim(),
        pick: () => pushFrame({ type: 'mem', id: m.id }),
      })),
      pick: () => pushFrame({ type: 'day', date }),
    });
  }

  // ── 主题 view（跟随筛选） ──
  const topicStatsFiltered = new Map<string, { n: number; sumWeight: number }>();
  filtered.forEach((m) =>
    m.topics.forEach((k) => {
      const s = topicStatsFiltered.get(k) ?? { n: 0, sumWeight: 0 };
      s.n++;
      s.sumWeight += m.weight;
      topicStatsFiltered.set(k, s);
    }),
  );
  const topicCards = topics
    .filter((t) => topicStatsFiltered.has(t.key))
    .sort((a, b) => topicStatsFiltered.get(b.key)!.n - topicStatsFiltered.get(a.key)!.n)
    .map((t) => {
      const stat = topicStatsFiltered.get(t.key)!;
      const topicMems = filtered.filter((m) => m.topics.includes(t.key)).sort((a, b) => (a.date + a.time < b.date + b.time ? 1 : -1));
      const freq = new Map<string, number>();
      topicMems.forEach((m) => m.tags.forEach((tag) => freq.set(tag, (freq.get(tag) ?? 0) + 1)));
      const sortedTags = [...freq.keys()].sort((a, b) => freq.get(b)! - freq.get(a)!);
      return {
        key: t.key,
        emoji: t.emoji,
        name: t.name,
        countLabel: `${stat.n} 条`,
        keywords: sortedTags.slice(0, 3).join(' · '),
        keywordCount: sortedTags.length,
        latest: topicMems.length ? relativeTime(topicMems[0].date, topicMems[0].time, now) : '',
        moon: moonInfo(Math.round(stat.sumWeight / stat.n) as MemoryWeight),
        pick: () => pushFrame({ type: 'topic', key: t.key }),
      };
    });

  // ── 星图观测台：时间回放 + 聚焦 ──
  const starDates = filtered.map((m) => m.date).filter(Boolean).sort();
  const minTs = starDates.length ? toTs(starDates[0]) : 0;
  const maxTs = starDates.length ? toTs(starDates[starDates.length - 1]) : 0;
  const replayTs = minTs + (replayPct / 100) * (maxTs - minTs);
  const replayActive = replayPct < 100 && maxTs > minTs;
  const starEntries = replayActive ? filtered.filter((m) => toTs(m.date) <= replayTs) : filtered;
  const replayYearLabel = replayActive ? new Date(replayTs).getFullYear().toString() : '现在';
  const yearsAvailable = [...new Set(filtered.map((m) => m.date.slice(0, 4)))].sort();

  function isObserved(m: MemoryEntry): boolean {
    if (observeMode === 'weight') return m.weight >= 4;
    if (observeMode === 'topic') return observeValue ? m.topics.includes(observeValue) : true;
    if (observeMode === 'who') return observeValue ? m.who === observeValue : true;
    if (observeMode === 'recent') return (now.getTime() - toTs(m.date)) / 86400000 <= 14;
    return true;
  }
  const observing = observeMode !== 'none';
  const observeLabel =
    observeMode === 'weight'
      ? '重要记忆'
      : observeMode === 'topic'
        ? (topicByKey.get(observeValue ?? '')?.name ?? '主题')
        : observeMode === 'who'
          ? (observeValue ?? '')
          : observeMode === 'recent'
            ? '最近新增'
            : '';

  function clearObserve() {
    setObserveMode('none');
    setObserveValue(null);
  }
  function pickObserve(mode: 'topic' | 'who', value: string) {
    setObserveMode(mode);
    setObserveValue(value);
    setTelescopeOpen(false);
    setTelescopeStep('menu');
  }
  function jumpToYear(year: string) {
    const endOfYear = toTs(`${year}-12-31`);
    const pct = maxTs > minTs ? Math.max(0, Math.min(100, ((endOfYear - minTs) / (maxTs - minTs)) * 100)) : 100;
    setReplayPct(pct);
    setTelescopeOpen(false);
    setTelescopeStep('menu');
  }
  function handleTelescopePick(key: 'weight' | 'topic' | 'who' | 'year' | 'recent') {
    if (key === 'weight' || key === 'recent') {
      setObserveMode(key);
      setObserveValue(null);
      setTelescopeOpen(false);
      setTelescopeStep('menu');
    } else {
      setTelescopeStep(key);
    }
  }
  function starClick(id: number) {
    setRippleId(id);
    window.setTimeout(() => {
      pushFrame({ type: 'mem', id });
      setRippleId(null);
    }, 380);
  }

  // ── 星图 ──
  const activeTopics = topics.filter((t) => starEntries.some((m) => m.topics[0] === t.key));
  const topicLayout = new Map<string, { x: number; y: number; color: string }>();
  activeTopics.forEach((t, i) => {
    const angle = (i / Math.max(1, activeTopics.length)) * Math.PI * 2 - Math.PI / 2;
    topicLayout.set(t.key, { x: 50 + Math.cos(angle) * 28, y: 50 + Math.sin(angle) * 26, color: constellationColor(i) });
  });
  const starPos = new Map<number, { x: number; y: number }>();
  type StarPoint = { id: number; x: number; y: number; s: number; c: string; big: boolean; tip: string; topicKey: string; links: number[]; observed: boolean };
  const stars: StarPoint[] = [];
  const constellationPaths: string[] = [];
  const starLabels: { x: number; y: number; name: string }[] = [];
  const starLegend: { color: string; name: string }[] = [];
  activeTopics.forEach((t) => {
    const layout = topicLayout.get(t.key)!;
    const members = starEntries.filter((m) => m.topics[0] === t.key).sort((a, b) => (a.date < b.date ? -1 : 1));
    let prev: { x: number; y: number } | null = null;
    members.forEach((m, i) => {
      const angle = i * 2.4 + seeded(m.id) * 0.8;
      const radius = i === 0 ? 0 : 7 + (i % 3) * 5;
      const x = Math.max(8, Math.min(92, layout.x + Math.cos(angle) * radius));
      const y = Math.max(10, Math.min(88, layout.y + Math.sin(angle) * radius * 0.9));
      starPos.set(m.id, { x, y });
      stars.push({
        id: m.id,
        x: +x.toFixed(1),
        y: +y.toFixed(1),
        s: +(4 + m.weight * 1.8).toFixed(1),
        c: layout.color,
        big: m.weight === 5,
        tip: `${m.summaryTitle}（权重 ${m.weight}）`,
        topicKey: t.key,
        links: m.links,
        observed: isObserved(m),
      });
      if (prev) constellationPaths.push(`M${prev.x.toFixed(1)} ${prev.y.toFixed(1)}L${x.toFixed(1)} ${y.toFixed(1)}`);
      prev = { x, y };
    });
    if (members.length) {
      starLabels.push({ x: layout.x, y: layout.y - 15, name: t.name });
      starLegend.push({ color: layout.color, name: t.name });
    }
  });
  const linkPaths: string[] = [];
  starEntries.forEach((m) =>
    m.links.forEach((id) => {
      if (m.id < id && starPos.has(m.id) && starPos.has(id)) {
        const a = starPos.get(m.id)!;
        const b = starPos.get(id)!;
        linkPaths.push(`M${a.x.toFixed(1)} ${a.y.toFixed(1)}L${b.x.toFixed(1)} ${b.y.toFixed(1)}`);
      }
    }),
  );
  const rippleCenter = rippleId !== null ? (stars.find((s) => s.id === rippleId) ?? null) : null;
  const bgStars = Array.from({ length: 26 }, (_, i) => ({
    x: +(seeded(i) * 96 + 2).toFixed(1),
    y: +(seeded(i + 50) * 94 + 3).toFixed(1),
    o: +(0.08 + seeded(i + 90) * 0.28).toFixed(2),
  }));
  const zoomIn = () => setZoom((z) => clampStarZoom(z + 0.25));
  const zoomOut = () => setZoom((z) => clampStarZoom(z - 0.25));

  function beginStarPinch() {
    const pts = [...starPointersRef.current.values()].slice(0, 2);
    if (pts.length < 2) return;
    const dist = Math.max(pointerDist(pts[0], pts[1]), 24);
    starGestureRef.current = {
      kind: 'pinch',
      startDist: dist,
      startZoom: zoomRef.current,
      startPan: { ...starPanRef.current },
      startMid: { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 },
      moved: false,
    };
  }

  function onStarPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    starPointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
    if (starPointersRef.current.size >= 2) {
      beginStarPinch();
      return;
    }
    const starNode = (e.target as HTMLElement).closest('[data-star-node]');
    const starId = starNode ? Number((starNode as HTMLElement).dataset.starId) : undefined;
    starGestureRef.current = { kind: 'pending', x: e.clientX, y: e.clientY, ox: starPan.x, oy: starPan.y, starId };
  }

  function onStarPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    if (!starPointersRef.current.has(e.pointerId)) return;
    starPointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (starPointersRef.current.size >= 2) {
      if (starGestureRef.current?.kind !== 'pinch') beginStarPinch();
      const pinch = starGestureRef.current;
      if (pinch?.kind !== 'pinch') return;
      const pts = [...starPointersRef.current.values()].slice(0, 2);
      const dist = Math.max(pointerDist(pts[0], pts[1]), 24);
      const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      const scale = dist / pinch.startDist;
      if (Math.abs(scale - 1) > 0.02 || Math.hypot(mid.x - pinch.startMid.x, mid.y - pinch.startMid.y) > 6) {
        pinch.moved = true;
      }
      setZoom(clampStarZoom(pinch.startZoom * scale));
      setStarPan({
        x: pinch.startPan.x + (mid.x - pinch.startMid.x),
        y: pinch.startPan.y + (mid.y - pinch.startMid.y),
      });
      return;
    }

    const g = starGestureRef.current;
    if (!g || g.kind === 'pinch') return;
    const dx = e.clientX - g.x;
    const dy = e.clientY - g.y;
    if (g.kind === 'pending') {
      if (Math.abs(dx) + Math.abs(dy) < 8) return;
      starGestureRef.current = { kind: 'pan', x: g.x, y: g.y, ox: g.ox, oy: g.oy };
    }
    const pan = starGestureRef.current;
    if (pan?.kind === 'pan') {
      setStarPan({ x: pan.ox + (e.clientX - pan.x), y: pan.oy + (e.clientY - pan.y) });
    }
  }

  function onStarPointerUp(e: ReactPointerEvent<HTMLDivElement>) {
    const g = starGestureRef.current;
    starPointersRef.current.delete(e.pointerId);
    try {
      (e.currentTarget as HTMLDivElement).releasePointerCapture(e.pointerId);
    } catch {
      /* already released */
    }

    if (starPointersRef.current.size >= 2) {
      beginStarPinch();
      return;
    }

    if (starPointersRef.current.size === 1) {
      starGestureRef.current = null;
      return;
    }

    if (g?.kind === 'pending' && g.starId !== undefined && !Number.isNaN(g.starId)) {
      starClick(g.starId);
    }
    starGestureRef.current = null;
  }

  // ── drawer ──
  const drawerFrame = stack[stack.length - 1] ?? null;

  function renderTimeView() {
    if (!timeItems.length) {
      return (
        <div style={{ textAlign: 'center', padding: '50px 0' }}>
          <svg viewBox="0 0 24 24" style={{ width: 40, height: 40 }}>
            <circle cx={12} cy={12} r={9} fill="none" stroke="#D9C6C0" strokeWidth={1.5} />
          </svg>
          <div style={{ fontSize: 14, color: '#A99590', marginTop: 12, letterSpacing: 1 }}>没有匹配的记忆</div>
          <div onClick={clearFilters} style={{ cursor: 'pointer', fontSize: 12, color: '#B76E79', marginTop: 10, letterSpacing: 1 }}>
            清除筛选
          </div>
        </div>
      );
    }
    return (
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {timeItems.map((item, i) =>
          item.kind === 'month' ? (
            <div key={`month-${item.num}-${i}`} style={{ display: 'flex', alignItems: 'center', padding: '8px 0 16px' }}>
              <span style={{ width: 48, textAlign: 'center', fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 32, color: '#E4D0CA', lineHeight: 1, flexShrink: 0 }}>
                {item.num}
              </span>
              <span style={{ width: 14, display: 'flex', justifyContent: 'center', flexShrink: 0 }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', background: '#B76E79' }} />
              </span>
              <div style={{ marginLeft: 12, flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 20, color: '#9C3B4A', letterSpacing: 1 }}>{item.label}</span>
                  <span style={{ fontSize: 12, color: '#A99590', letterSpacing: 1 }}>· {item.cn}</span>
                  <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0, paddingRight: 2 }}>
                    <span title="本月记忆条数" style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                      <svg viewBox="0 0 24 24" style={{ width: 14, height: 14 }}>
                        <circle cx={12} cy={12} r={9} fill="none" stroke="#A99590" strokeWidth={1.5} />
                        <circle cx={12} cy={12} r={3} fill="#A99590" />
                      </svg>
                      <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, color: '#8C7B76' }}>{item.monthCount}</span>
                    </span>
                    {item.monthCore > 0 && (
                      <span title="本月核心记忆" style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                        <svg viewBox="0 0 24 24" style={{ width: 14, height: 14 }}>
                          <circle cx={12} cy={12} r={9} fill="#B76E79" stroke="#B76E79" strokeWidth={1.5} />
                        </svg>
                        <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, color: '#B76E79' }}>{item.monthCore}</span>
                      </span>
                    )}
                  </span>
                </div>
              </div>
            </div>
          ) : (
            <div key={item.date} style={{ display: 'flex' }}>
              <div style={{ width: 48, flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: 12 }}>
                <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 24, fontWeight: 500, lineHeight: 1 }}>{item.dayNum}</span>
                <span style={{ fontFamily: "'Bodoni Moda',serif", fontStyle: 'italic', fontSize: 11, color: '#A99590', marginTop: 4 }}>{item.monthCn}</span>
                <span style={{ fontSize: 10, color: '#C4B4AF', marginTop: 2 }}>{item.weekday}</span>
              </div>
              <div style={{ width: 14, flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#FFFFFF', border: '2px solid #B76E79', marginTop: 18, flexShrink: 0 }} />
                <div style={{ width: 1, flex: 1, background: '#EAD9D4', marginTop: 4 }} />
              </div>
              <Card onClick={item.pick} style={{ flex: 1, minWidth: 0, margin: '0 0 14px 12px', padding: '14px 16px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 600, letterSpacing: 1 }}>{item.countLabel}</span>
                  {item.isToday && (
                    <span style={{ fontSize: 10, border: '1px solid rgba(217,164,65,0.5)', color: '#B98A2E', borderRadius: 999, padding: '2px 8px', letterSpacing: 1 }}>今天</span>
                  )}
                  {item.hasCore && (
                    <span style={{ fontSize: 10, border: '1px solid rgba(183,110,121,0.4)', color: '#B76E79', borderRadius: 999, padding: '2px 8px', letterSpacing: 1 }}>
                      核心 · {item.coreN}
                    </span>
                  )}
                  <span style={{ marginLeft: 'auto', display: 'inline-flex', flexShrink: 0, lineHeight: 0 }}>
                    <MoonIcon info={item.moon} />
                  </span>
                </div>
                <div style={{ borderTop: '1px dashed #EFE3DE', marginTop: 10 }} />
                <div style={{ marginTop: 4 }}>
                  {item.rows.map((r, ri) => (
                    <div
                      key={ri}
                      onClick={(e) => {
                        e.stopPropagation();
                        r.pick();
                      }}
                      style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0' }}
                    >
                      <span style={{ width: 16, height: 16, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                        <span style={{ width: r.size, height: r.size, borderRadius: '50%', background: r.bg, border: r.border, flexShrink: 0 }} />
                      </span>
                      <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: '#A99590', flexShrink: 0, width: 36 }}>{r.time}</span>
                      <span
                        style={{
                          fontSize: 13,
                          fontWeight: 600,
                          color: r.titleColor,
                          flexShrink: 0,
                          maxWidth: '44%',
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                        }}
                      >
                        {r.title}
                      </span>
                      <span style={{ fontSize: 12, color: '#D9CCC7', flexShrink: 0 }}>·</span>
                      <span
                        style={{
                          fontSize: 12,
                          color: '#8C7B76',
                          flex: 1,
                          minWidth: 0,
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                        }}
                      >
                        {r.preview}
                      </span>
                    </div>
                  ))}
                </div>
              </Card>
            </div>
          ),
        )}
      </div>
    );
  }

  function renderTopicView() {
    if (!topicCards.length) {
      return (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <div style={{ fontSize: 14, color: '#A99590', letterSpacing: 1 }}>这个筛选下没有主题</div>
          <div onClick={clearFilters} style={{ cursor: 'pointer', fontSize: 12, color: '#B76E79', marginTop: 10, letterSpacing: 1 }}>
            清除筛选
          </div>
        </div>
      );
    }
    return (
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {topicCards.map((t) => (
          <Card key={t.key} onClick={t.pick} style={{ padding: '18px 16px' }}>
            <div style={{ fontSize: 26, lineHeight: 1 }}>{t.emoji}</div>
            <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: 1, marginTop: 10 }}>{t.name}</div>
            <div style={{ fontSize: 11, color: '#8C7B76', letterSpacing: 1, marginTop: 6, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {t.keywords} <span style={{ color: '#C9BDB8' }}>+{t.keywordCount}</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 8 }}>
              <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 13, color: '#A99590' }}>{t.countLabel}</span>
              <span style={{ color: '#D9CCC7' }}>·</span>
              <MoonIcon info={t.moon} />
              <span style={{ marginLeft: 'auto', fontSize: 10, color: '#C4B4AF', flexShrink: 0 }}>{t.latest}</span>
            </div>
          </Card>
        ))}
      </div>
    );
  }

  function renderStarView() {
    return (
      <div>
        <div
          style={{ position: 'relative', background: 'radial-gradient(circle at 62% 28%, #46394E 0%, #2E2733 70%)', borderRadius: 22, boxShadow: '0 10px 30px rgba(183,110,121,0.10)', height: 460, overflow: 'hidden', cursor: 'grab', touchAction: 'none' }}
          onPointerDown={onStarPointerDown}
          onPointerMove={onStarPointerMove}
          onPointerUp={onStarPointerUp}
          onPointerCancel={onStarPointerUp}
        >
          <div style={{ position: 'absolute', inset: 0, transform: `translate(${starPan.x}px, ${starPan.y}px) scale(${zoom})`, transformOrigin: '50% 50%' }}>
            <svg viewBox="0 0 100 100" preserveAspectRatio="none" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}>
              <path d={constellationPaths.join('')} stroke="rgba(255,255,255,0.16)" strokeWidth={1} fill="none" vectorEffect="non-scaling-stroke" />
              <path d={linkPaths.join('')} stroke="rgba(233,194,117,0.4)" strokeWidth={1} strokeDasharray="3 4" fill="none" vectorEffect="non-scaling-stroke" />
            </svg>
            {bgStars.map((b, i) => (
              <div key={i} style={{ position: 'absolute', left: `${b.x}%`, top: `${b.y}%`, width: 2, height: 2, borderRadius: '50%', background: '#FFFFFF', opacity: b.o, pointerEvents: 'none' }} />
            ))}
            {stars.map((s) => {
              const related = rippleCenter
                ? s.id === rippleCenter.id || s.topicKey === rippleCenter.topicKey || rippleCenter.links.includes(s.id) || s.links.includes(rippleCenter.id)
                : false;
              const opacity = rippleCenter ? (related ? 1 : 0.12) : observing ? (s.observed ? 1 : 0.3) : 1;
              const glow =
                rippleCenter && s.id === rippleCenter.id
                  ? '0 0 16px rgba(255,255,255,0.9), 0 0 26px rgba(233,194,117,0.8)'
                  : s.big
                    ? '0 0 12px rgba(233,194,117,0.95), 0 0 4px rgba(255,255,255,0.7)'
                    : 'none';
              return (
                <div
                  key={s.id}
                  data-star-node
                  data-star-id={s.id}
                  title={s.tip}
                  style={{
                    position: 'absolute',
                    left: `${s.x}%`,
                    top: `${s.y}%`,
                    width: s.s,
                    height: s.s,
                    borderRadius: '50%',
                    background: s.c,
                    boxShadow: glow,
                    transform: 'translate(-50%,-50%)',
                    pointerEvents: 'auto',
                    opacity,
                    transition: 'opacity 0.35s ease',
                  }}
                />
              );
            })}
            {starLabels.map((l, i) => (
              <div key={i} style={{ position: 'absolute', left: `${l.x}%`, top: `${l.y}%`, transform: 'translate(-50%,-50%)', fontSize: 11, letterSpacing: 4, color: 'rgba(255,255,255,0.4)', pointerEvents: 'none', whiteSpace: 'nowrap' }}>
                {l.name}
              </div>
            ))}
          </div>

          {/* 聚焦指示 */}
          {observing && (
            <div
              onPointerDown={(e) => e.stopPropagation()}
              style={{ position: 'absolute', top: 14, left: 14, zIndex: 5, display: 'flex', alignItems: 'center', gap: 6, background: 'rgba(233,194,117,0.16)', border: '1px solid rgba(233,194,117,0.4)', borderRadius: 999, padding: '5px 10px' }}
            >
              <span style={{ fontSize: 11, color: '#E9C275', letterSpacing: 1 }}>🔭 {observeLabel}</span>
              <span
                onClick={clearObserve}
                style={{ cursor: 'pointer', width: 15, height: 15, borderRadius: '50%', background: 'rgba(255,255,255,0.14)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, color: 'rgba(255,255,255,0.75)' }}
              >
                ×
              </span>
            </div>
          )}

          {/* 望远镜：观测台 */}
          {telescopeOpen && (
            <div
              onClick={() => {
                setTelescopeOpen(false);
                setTelescopeStep('menu');
              }}
              onPointerDown={(e) => e.stopPropagation()}
              style={{ position: 'absolute', inset: 0, zIndex: 4 }}
            />
          )}
          <div onPointerDown={(e) => e.stopPropagation()} style={{ position: 'absolute', top: 14, right: 14, zIndex: 5 }}>
            <div
              onClick={() => {
                setTelescopeOpen((v) => !v);
                setTelescopeStep('menu');
              }}
              style={{
                cursor: 'pointer',
                width: 36,
                height: 36,
                borderRadius: '50%',
                background: telescopeOpen ? 'rgba(233,194,117,0.85)' : 'rgba(46,39,51,0.72)',
                border: '1px solid rgba(255,255,255,0.18)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 17,
                boxShadow: '0 4px 14px rgba(0,0,0,0.25)',
              }}
            >
              🔭
            </div>
            {telescopeOpen && (
              <div
                style={{
                  position: 'absolute',
                  top: 44,
                  right: 0,
                  width: 190,
                  background: 'rgba(38,32,42,0.95)',
                  border: '1px solid rgba(255,255,255,0.14)',
                  borderRadius: 16,
                  padding: 8,
                  boxShadow: '0 14px 34px rgba(0,0,0,0.35)',
                  backdropFilter: 'blur(6px)',
                }}
              >
                {telescopeStep === 'menu' &&
                  TELESCOPE_ITEMS.map((it) => (
                    <div
                      key={it.key}
                      onClick={() => handleTelescopePick(it.key)}
                      style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 9, padding: '9px 10px', borderRadius: 10, fontSize: 13, color: 'rgba(255,255,255,0.88)' }}
                    >
                      <span style={{ fontSize: 14 }}>{it.icon}</span>
                      <span>{it.label}</span>
                    </div>
                  ))}
                {telescopeStep === 'topic' && (
                  <>
                    <div onClick={() => setTelescopeStep('menu')} style={{ cursor: 'pointer', fontSize: 12, color: 'rgba(255,255,255,0.5)', padding: '4px 10px 8px' }}>
                      ‹ 返回
                    </div>
                    {topicOrder.map((t) => (
                      <div
                        key={t.key}
                        onClick={() => pickObserve('topic', t.key)}
                        style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 9, padding: '8px 10px', borderRadius: 10, fontSize: 13, color: 'rgba(255,255,255,0.88)' }}
                      >
                        <span>{t.emoji}</span>
                        <span>{t.name}</span>
                      </div>
                    ))}
                  </>
                )}
                {telescopeStep === 'who' && (
                  <>
                    <div onClick={() => setTelescopeStep('menu')} style={{ cursor: 'pointer', fontSize: 12, color: 'rgba(255,255,255,0.5)', padding: '4px 10px 8px' }}>
                      ‹ 返回
                    </div>
                    {['费佳', '哈娅'].map((w) => (
                      <div key={w} onClick={() => pickObserve('who', w)} style={{ cursor: 'pointer', padding: '8px 10px', borderRadius: 10, fontSize: 13, color: 'rgba(255,255,255,0.88)' }}>
                        {w}
                      </div>
                    ))}
                  </>
                )}
                {telescopeStep === 'year' && (
                  <>
                    <div onClick={() => setTelescopeStep('menu')} style={{ cursor: 'pointer', fontSize: 12, color: 'rgba(255,255,255,0.5)', padding: '4px 10px 8px' }}>
                      ‹ 返回
                    </div>
                    {yearsAvailable.map((y) => (
                      <div key={y} onClick={() => jumpToYear(y)} style={{ cursor: 'pointer', padding: '8px 10px', borderRadius: 10, fontSize: 13, color: 'rgba(255,255,255,0.88)' }}>
                        {y} 年
                      </div>
                    ))}
                  </>
                )}
              </div>
            )}
          </div>

          {/* 时间回放 */}
          {starDates.length >= 2 && (
            <div
              onPointerDown={(e) => e.stopPropagation()}
              style={{ position: 'absolute', left: 14, right: 14, bottom: 54, display: 'flex', alignItems: 'center', gap: 10, background: 'rgba(46,39,51,0.55)', border: '1px solid rgba(255,255,255,0.10)', borderRadius: 999, padding: '6px 14px', backdropFilter: 'blur(4px)' }}
            >
              <span style={{ fontSize: 12, flexShrink: 0 }}>🕐</span>
              <input type="range" min={0} max={100} step={1} value={replayPct} onChange={(e) => setReplayPct(parseInt(e.target.value, 10))} style={{ flex: 1, height: 3 }} />
              <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12, color: 'rgba(255,255,255,0.75)', width: 44, textAlign: 'right', flexShrink: 0 }}>{replayYearLabel}</span>
            </div>
          )}

          <div
            onPointerDown={(e) => e.stopPropagation()}
            style={{ position: 'absolute', left: 14, right: 14, bottom: 12, display: 'flex', alignItems: 'center', gap: 10, background: 'rgba(46,39,51,0.72)', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 999, padding: '7px 14px', backdropFilter: 'blur(4px)' }}
          >

            <span onClick={zoomOut} style={{ cursor: 'pointer', width: 22, height: 22, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'rgba(255,255,255,0.75)', fontSize: 15, flexShrink: 0, border: '1px solid rgba(255,255,255,0.2)' }}>
              −
            </span>
            <input type="range" min={STAR_ZOOM_MIN} max={STAR_ZOOM_MAX} step={0.1} value={zoom} onChange={(e) => setZoom(clampStarZoom(parseFloat(e.target.value)))} style={{ flex: 1, height: 3 }} />
            <span onClick={zoomIn} style={{ cursor: 'pointer', width: 22, height: 22, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'rgba(255,255,255,0.75)', fontSize: 13, flexShrink: 0, border: '1px solid rgba(255,255,255,0.2)' }}>
              ＋
            </span>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: 'rgba(255,255,255,0.6)', width: 34, textAlign: 'right', flexShrink: 0 }}>{zoom.toFixed(1)}×</span>
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'center', gap: 16, flexWrap: 'wrap', marginTop: 12 }}>
          {starLegend.map((lg, i) => (
            <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#A99590' }}>
              <span style={{ width: 7, height: 7, borderRadius: '50%', background: lg.color }} />
              {lg.name}
            </span>
          ))}
        </div>
        <div style={{ textAlign: 'center', fontSize: 11, color: '#C4B4AF', marginTop: 8, letterSpacing: 1 }}>星点大小 = 权重 · 连线 = 关联 · 点击星点查看记忆 · 🔭 右上角开启观测</div>
      </div>
    );
  }

  function renderDayDetail(date: string) {
    const dayMems = entries.filter((m) => m.date === date).sort((a, b) => (a.time < b.time ? -1 : 1));
    if (!dayMems.length) return null;
    const maxWeight = Math.max(...dayMems.map((m) => m.weight)) as MemoryWeight;
    const stats = [
      { v: dayMems.length, k: '条目' },
      { v: dayMems.filter((m) => m.weight === 5).length, k: '核心' },
      { v: dayMems.filter((m) => m.weight === 4).length, k: '重要' },
      { v: new Set(dayMems.flatMap((m) => m.topics)).size, k: '主题' },
    ];
    return (
      <>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12 }}>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 44, fontWeight: 500, lineHeight: 0.9 }}>{date.slice(8, 10)}</span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1, paddingBottom: 2 }}>
            <span style={{ fontSize: 13, color: '#8C7B76', letterSpacing: 1 }}>
              {date.slice(0, 4)} 年 {date.slice(5, 7)} 月
            </span>
            <span style={{ fontSize: 11, color: '#C4B4AF' }}>{weekdayCN(date)}</span>
          </div>
          <MoonIcon info={moonInfo(maxWeight)} style={{ paddingBottom: 4 }} />
        </div>
        <div style={{ fontSize: 14, fontStyle: 'italic', color: '#6B5A55', lineHeight: 1.8, marginTop: 12 }}>这一天有 {dayMems.length} 条记忆。</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', background: '#FFFFFF', borderRadius: 16, boxShadow: '0 6px 18px rgba(183,110,121,0.08)', padding: '14px 0', marginTop: 16 }}>
          {stats.map((s) => (
            <div key={s.k} style={{ textAlign: 'center' }}>
              <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 24, fontWeight: 500, lineHeight: 1 }}>{s.v}</div>
              <div style={{ fontSize: 10, letterSpacing: 2, color: '#A99590', marginTop: 6 }}>{s.k}</div>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 18 }}>
          {dayMems.map((m) => {
            const dot = weightDot(m.weight);
            return (
              <div key={m.id} style={{ display: 'flex' }}>
                <span style={{ width: 38, flexShrink: 0, fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: '#A99590', paddingTop: 17 }}>{m.time}</span>
                <div style={{ width: 20, flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                  <span style={{ width: dot.size, height: dot.size, borderRadius: '50%', background: dot.bg, border: dot.border, marginTop: 17, flexShrink: 0 }} />
                  <span style={{ width: 1, flex: 1, background: '#EAD9D4', marginTop: 4 }} />
                </div>
                <div
                  onClick={() => pushFrame({ type: 'mem', id: m.id })}
                  className="card-hover"
                  style={{ cursor: 'pointer', flex: 1, minWidth: 0, margin: '0 0 14px 8px', background: '#FFFFFF', borderRadius: 16, boxShadow: '0 6px 18px rgba(183,110,121,0.08)', padding: '14px 16px' }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ flex: 1, fontSize: 15, fontWeight: 600, lineHeight: 1.5 }}>{m.summaryTitle}</span>
                    <MoonIcon info={moonInfo(m.weight)} />
                  </div>
                  <div style={{ borderLeft: '2px solid #D6A5A1', background: '#FBF4F1', borderRadius: '0 10px 10px 0', padding: '10px 12px', fontSize: 13, lineHeight: 1.9, color: '#6B5A55', marginTop: 10 }}>
                    {m.content}
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
                    {m.tags.map((tag) => {
                      const col = tagColor(tag);
                      return (
                        <span key={tag} style={{ fontSize: 11, borderRadius: 10, padding: '3px 9px', background: col.bg, color: col.color, letterSpacing: 1 }}>
                          #{tag}
                        </span>
                      );
                    })}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </>
    );
  }

  function renderTopicDetail(key: string) {
    const topic = topicByKey.get(key);
    if (!topic) return null;
    const topicMems = entries.filter((m) => m.topics.includes(key)).sort((a, b) => (a.date < b.date ? 1 : -1));
    const sumWeight = topicMems.reduce((s, m) => s + m.weight, 0);
    const avgWeight = (topicMems.length ? Math.round(sumWeight / topicMems.length) : 1) as MemoryWeight;
    const byWeight = [...topicMems].sort((a, b) => b.weight - a.weight);
    const top1 = byWeight[0]?.id ?? null;
    const top2 = byWeight[1]?.id ?? null;
    const related = topic.related.filter((r) => topicByKey.has(r.key));
    return (
      <>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 28, lineHeight: 1 }}>{topic.emoji}</span>
          <span style={{ fontSize: 20, fontWeight: 600, letterSpacing: 1 }}>{topic.name}</span>
          <span style={{ marginLeft: 'auto' }}>
            <MoonIcon info={moonInfo(avgWeight)} />
          </span>
        </div>
        <div style={{ background: '#F9EDEA', borderRadius: 14, padding: '14px 16px', fontSize: 14, fontStyle: 'italic', lineHeight: 1.9, color: '#7A625E', marginTop: 14 }}>{topic.ai}</div>
        <div style={{ fontSize: 13, color: '#8C7B76', lineHeight: 1.8, marginTop: 12 }}>{topic.desc}</div>
        <SectionDivider label="包含标签" />
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
          {[...new Set(topicMems.flatMap((m) => m.tags))].map((tag) => {
            const col = tagColor(tag);
            return (
              <span key={tag} onClick={() => toggleTag(tag)} style={{ cursor: 'pointer', fontSize: 11, borderRadius: 10, padding: '4px 10px', background: col.bg, color: col.color, letterSpacing: 1 }}>
                #{tag}
              </span>
            );
          })}
        </div>
        <SectionDivider label={`相关记忆 · ${topicMems.length}`} marginTop={22} />
        <div style={{ display: 'flex', flexDirection: 'column', marginTop: 4 }}>
          {topicMems.map((m) => (
            <div
              key={m.id}
              onClick={() => pushFrame({ type: 'mem', id: m.id })}
              style={{ cursor: 'pointer', display: 'flex', alignItems: 'flex-start', gap: 12, padding: '12px 2px', borderBottom: '1px solid #F5EDE9' }}
            >
              <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12, color: '#7E93AD', width: 86, flexShrink: 0, paddingTop: 2 }}>{formatDateDot(m.date)}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <span
                  style={{
                    display: 'block',
                    fontSize: 14,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    color: m.id === top1 ? '#9C3B4A' : m.id === top2 ? '#8A7AB5' : '#4A3F3C',
                    fontWeight: m.id === top1 || m.id === top2 ? 600 : 400,
                  }}
                >
                  {m.summaryTitle}
                </span>
                <div
                  style={{
                    fontSize: 12,
                    color: '#B9A8A2',
                    lineHeight: 1.7,
                    marginTop: 2,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    WebkitMaskImage: 'linear-gradient(to right, #000 75%, transparent 100%)',
                    maskImage: 'linear-gradient(to right, #000 75%, transparent 100%)',
                  }}
                >
                  {(m.content || '').replace(/\s+/g, ' ').trim()}
                </div>
              </div>
            </div>
          ))}
        </div>
        {related.length > 0 && (
          <>
            <SectionDivider label="可能与你有关" marginTop={22} />
            <div style={{ display: 'flex', flexDirection: 'column', marginTop: 4 }}>
              {related.map((r) => {
                const rTopic = topicByKey.get(r.key)!;
                const tops = entries
                  .filter((m) => m.topics.includes(r.key))
                  .sort((a, b) => b.weight - a.weight || (a.date < b.date ? 1 : -1))
                  .slice(0, 2);
                return (
                  <div key={r.key} style={{ padding: '12px 2px', borderBottom: '1px solid #F5EDE9' }}>
                    <div onClick={() => pushFrame({ type: 'topic', key: r.key })} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 12 }}>
                      <span style={{ fontSize: 16, width: 22, textAlign: 'center', flexShrink: 0 }}>{rTopic.emoji}</span>
                      <span style={{ flex: 1, fontSize: 14 }}>{rTopic.name}</span>
                      <span style={{ width: 64, height: 4, borderRadius: 2, background: '#F0E6E2', overflow: 'hidden', flexShrink: 0 }}>
                        <span style={{ display: 'block', height: '100%', borderRadius: 2, background: '#8A7AB5', width: `${Math.round(r.pct * 100)}%` }} />
                      </span>
                      <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12, color: '#A99590', width: 34, textAlign: 'right', flexShrink: 0 }}>{Math.round(r.pct * 100)}%</span>
                    </div>
                    <div style={{ margin: '8px 0 2px 34px', display: 'flex', flexDirection: 'column', gap: 7 }}>
                      {tops.map((x) => (
                        <div key={x.id} onClick={() => pushFrame({ type: 'mem', id: x.id })} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#D9C6C0', flexShrink: 0 }} />
                          <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: '#A99590', flexShrink: 0 }}>{formatDateDot(x.date)}</span>
                          <span style={{ flex: 1, minWidth: 0, fontSize: 12, color: '#8C7B76', whiteSpace: 'nowrap', overflow: 'hidden' }}>
                            {x.title} · {x.preview}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </>
    );
  }

  function renderMemDetail(id: number) {
    const m = entries.find((x) => x.id === id);
    if (!m) return null;
    const same = entries.filter((x) => x.date === m.date && x.id !== m.id).sort((a, b) => (a.time < b.time ? -1 : 1));
    const links = m.links.map((lid) => entries.find((x) => x.id === lid)).filter((x): x is MemoryEntry => Boolean(x));
    return (
      <>
        <div style={{ fontSize: 21, fontWeight: 600, lineHeight: 1.6 }}>{m.summaryTitle ?? m.title}</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
          <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12, color: '#8C7B76', letterSpacing: 1 }}>
            {formatDateDot(m.date)} · {weekdayCN(m.date)} · {m.time}
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: m.who === '费佳' ? '#8A7AB5' : '#D9A441' }} />
            <span style={{ fontSize: 12, color: '#8C7B76' }}>{m.who}</span>
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 'auto' }}>
            <MoonIcon info={moonInfo(m.weight)} />
            <span style={{ display: 'flex', gap: 3 }}>
              {[1, 2, 3, 4, 5].map((i) => (
                <span key={i} style={{ width: 11, height: 6, borderRadius: 3, background: i <= m.weight ? '#B76E79' : '#F0E2DD' }} />
              ))}
            </span>
            <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12, color: '#8C7B76' }}>{m.weight}</span>
          </span>
        </div>
        <SectionDivider label="正文" />
        <div style={{ fontSize: 14, lineHeight: 2, color: '#5E524E', marginTop: 10 }}>{m.content}</div>
        <SectionDivider label="标签" />
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
          {m.tags.map((tag) => {
            const col = tagColor(tag);
            return (
              <span key={tag} style={{ fontSize: 11, borderRadius: 10, padding: '4px 10px', background: col.bg, color: col.color, letterSpacing: 1 }}>
                #{tag}
              </span>
            );
          })}
        </div>
        <SectionDivider label="所属主题" />
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
          {m.topics.map((k) => {
            const t = topicByKey.get(k);
            if (!t) return null;
            return (
              <span
                key={k}
                onClick={() => pushFrame({ type: 'topic', key: k })}
                style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6, background: '#FFFFFF', borderRadius: 999, padding: '6px 14px', boxShadow: '0 4px 12px rgba(183,110,121,0.08)', fontSize: 13 }}
              >
                <span>{t.emoji}</span>
                <span>{t.name}</span>
                <span style={{ color: '#C4B4AF' }}>›</span>
              </span>
            );
          })}
        </div>
        {same.length > 0 && (
          <>
            <SectionDivider label={`同日其他记忆 · ${same.length}`} marginTop={22} />
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 10 }}>
              {same.map((sm) => (
                <div key={sm.id} onClick={() => pushFrame({ type: 'mem', id: sm.id })} className="card-hover" style={{ cursor: 'pointer', background: '#FFFFFF', borderRadius: 14, boxShadow: '0 6px 18px rgba(183,110,121,0.07)', padding: 12 }}>
                  <div style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 10, color: '#A99590' }}>{sm.time}</div>
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: titleColor(sm.weight),
                      marginTop: 4,
                      lineHeight: 1.5,
                      display: '-webkit-box',
                      WebkitLineClamp: 1,
                      WebkitBoxOrient: 'vertical',
                      overflow: 'hidden',
                    }}
                  >
                    {sm.summaryTitle}
                  </div>
                  <div style={{ fontSize: 11, color: '#A99590', marginTop: 4, lineHeight: 1.6, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                    {sm.content}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
        {links.length > 0 && (
          <>
            <SectionDivider label="可能关联 · 语义相似" marginTop={22} />
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 10 }}>
              {links.map((lk, i) => (
                <div key={lk.id} onClick={() => pushFrame({ type: 'mem', id: lk.id })} className="card-hover" style={{ cursor: 'pointer', background: '#FAF2EF', borderRadius: 14, padding: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: "'Bodoni Moda',serif", fontSize: 10, color: '#A99590' }}>
                    <span>{formatDateDot(lk.date)}</span>
                    <span style={{ color: '#8A7AB5' }}>{92 - i * 7}%</span>
                  </div>
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: '#8A7AB5',
                      marginTop: 4,
                      lineHeight: 1.5,
                      display: '-webkit-box',
                      WebkitLineClamp: 1,
                      WebkitBoxOrient: 'vertical',
                      overflow: 'hidden',
                    }}
                  >
                    {lk.summaryTitle}
                  </div>
                  <div style={{ fontSize: 11, color: '#A99590', marginTop: 4, lineHeight: 1.6, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                    {lk.content}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </>
    );
  }

  function renderDrawer() {
    if (!drawerFrame) return null;
    const crumb =
      drawerFrame.type === 'day'
        ? `月相 › ${formatDateDot(drawerFrame.date)}`
        : drawerFrame.type === 'topic'
          ? `主题 › ${topicByKey.get(drawerFrame.key)?.name ?? ''}`
          : `记忆 › ${(() => {
              const m = entries.find((x) => x.id === drawerFrame.id);
              return m ? formatDateDot(m.date) : '';
            })()}`;
    return (
      <>
        <div onClick={closeDrawer} style={{ position: 'fixed', inset: 0, background: 'rgba(74,63,60,0.22)', zIndex: 55 }} />
        <div
          style={{
            position: 'fixed',
            zIndex: 60,
            background: '#FBF7F4',
            boxShadow: '0 -12px 44px rgba(183,110,121,0.20)',
            display: 'flex',
            flexDirection: 'column',
            ...(isDesktop
              ? { right: 0, top: 0, width: '40%', minWidth: 420, height: '100%', borderRadius: '26px 0 0 26px' }
              : { left: '50%', bottom: 0, transform: 'translateX(-50%)', width: '100%', maxWidth: 430, height: '85%', borderRadius: '26px 26px 0 0' }),
          }}
        >
          {!isDesktop && <div style={{ width: 40, height: 4, borderRadius: 2, background: '#E4D6D1', margin: '10px auto 0', flexShrink: 0 }} />}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 20px 8px', flexShrink: 0 }}>
            {stack.length > 1 && (
              <div
                onClick={popFrame}
                style={{ cursor: 'pointer', width: 32, height: 32, borderRadius: '50%', background: '#FFFFFF', boxShadow: '0 4px 12px rgba(183,110,121,0.10)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 17, color: '#8C7B76' }}
              >
                ‹
              </div>
            )}
            <span style={{ flex: 1, fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 3, color: '#B9A8A2', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{crumb}</span>
            <div
              onClick={closeDrawer}
              style={{ cursor: 'pointer', width: 32, height: 32, borderRadius: '50%', background: '#FFFFFF', boxShadow: '0 4px 12px rgba(183,110,121,0.10)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 15, color: '#8C7B76' }}
            >
              ×
            </div>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: '4px 22px 34px' }}>
            {drawerFrame.type === 'day' && renderDayDetail(drawerFrame.date)}
            {drawerFrame.type === 'topic' && renderTopicDetail(drawerFrame.key)}
            {drawerFrame.type === 'mem' && renderMemDetail(drawerFrame.id)}
          </div>
        </div>
      </>
    );
  }

  return (
    <div style={{ zoom: 1.07 }}>
    <ScreenLayout>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <BackHeader title="记忆库" />
        <span style={{ marginLeft: 'auto', fontFamily: "'Bodoni Moda',serif", fontSize: 12, letterSpacing: 2, color: 'var(--color-text-faint)' }}>{entries.length} memories</span>
      </div>

      {/* search + active filters */}
      <div style={{ position: 'relative', zIndex: 40 }}>
        <Card style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', borderRadius: 999, padding: '10px 16px', boxShadow: 'inset 0 2px 6px rgba(183,110,121,0.08), 0 6px 18px rgba(183,110,121,0.08)' }}>
          <svg viewBox="0 0 24 24" style={{ width: 17, height: 17, flexShrink: 0 }} fill="none" stroke="#B9A8A2" strokeWidth={2} strokeLinecap="round">
            <circle cx={11} cy={11} r={7} />
            <path d="M20 20l-3.5-3.5" />
          </svg>
          {activeStateLabel && (
            <span
              onClick={() => setFState('all')}
              style={{ cursor: 'pointer', flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, letterSpacing: 1, borderRadius: 999, padding: '4px 10px', background: 'rgba(183,110,121,0.12)', color: '#9C3B4A', border: '1px solid rgba(183,110,121,0.35)' }}
            >
              {activeStateLabel}
              <span style={{ fontSize: 12, opacity: 0.7 }}>×</span>
            </span>
          )}
          {activeTopicMeta && (
            <span
              onClick={() => setFTopic('all')}
              style={{ cursor: 'pointer', flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, letterSpacing: 1, borderRadius: 999, padding: '4px 10px', background: 'rgba(183,110,121,0.12)', color: '#9C3B4A', border: '1px solid rgba(183,110,121,0.35)' }}
            >
              {activeTopicMeta.emoji} {activeTopicMeta.name}
              <span style={{ fontSize: 12, opacity: 0.7 }}>×</span>
            </span>
          )}
          {tagChips.map((c) => {
            const col = tagColor(c.value);
            return (
              <span
                key={`tag-${c.value}`}
                onClick={() => removeTag(c.value)}
                style={{ cursor: 'pointer', flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, letterSpacing: 1, borderRadius: 10, padding: '4px 10px', background: col.bg, color: col.color, border: `1px solid ${col.color}`, fontWeight: 600 }}
              >
                #{c.value}
                <span style={{ fontSize: 12, opacity: 0.75 }}>×</span>
              </span>
            );
          })}
          {whoChips.map((c) => (
            <span
              key={`who-${c.value}`}
              onClick={() => removeWho(c.value)}
              style={{ cursor: 'pointer', flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, letterSpacing: 1, borderRadius: 999, padding: '4px 10px', background: '#F3EBFF', color: '#7A6A9A', border: '1px solid rgba(122,106,154,0.35)' }}
            >
              {c.value}
              <span style={{ fontSize: 12, opacity: 0.75 }}>×</span>
            </span>
          ))}
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={hasFacetFilters ? '继续搜索…' : '搜索内容、标签、人物、主题…'}
            className="memory-search-input"
            style={{ flex: '1 1 120px', border: 'none', background: 'transparent', outline: 'none', fontFamily: "'Noto Serif SC',serif", fontSize: 15, color: '#4A3F3C', minWidth: 88 }}
          />
          {hasFacetFilters && (
            <span
              onClick={clearFilters}
              style={{ cursor: 'pointer', flexShrink: 0, fontSize: 11, letterSpacing: 1, color: '#B76E79', padding: '4px 2px', whiteSpace: 'nowrap' }}
            >
              清除筛选
            </span>
          )}
          {q.length > 0 && (
            <span
              onClick={() => setQuery('')}
              style={{ cursor: 'pointer', width: 20, height: 20, borderRadius: '50%', background: '#F3EBE8', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, color: '#A99590', flexShrink: 0 }}
            >
              ×
            </span>
          )}
        </Card>
        {showSuggest && (
          <Card style={{ position: 'absolute', top: 'calc(100% + 8px)', left: 0, right: 0, padding: '10px 8px', maxHeight: 340, overflowY: 'auto', boxShadow: '0 14px 40px rgba(183,110,121,0.18)' }}>
            {suggestGroups.map((g) => (
              <div key={g.label}>
                <div style={{ padding: '6px 12px 2px', fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 3, color: '#C4B4AF' }}>{g.label}</div>
                {g.items.map((it, i) => (
                  <div key={i} onClick={it.pick} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px', borderRadius: 12 }}>
                    <span style={{ fontSize: 13, width: 20, textAlign: 'center', flexShrink: 0, color: it.iconColor }}>{it.icon}</span>
                    <span style={{ flex: 1, fontSize: 14, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {it.pre}
                      <span style={{ background: 'rgba(217,164,65,0.28)', borderRadius: 3 }}>{it.hit}</span>
                      {it.post}
                    </span>
                    <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, color: '#C4B4AF', flexShrink: 0 }}>{it.meta}</span>
                  </div>
                ))}
              </div>
            ))}
            {suggestEmpty && <div style={{ padding: '16px 12px', textAlign: 'center', fontSize: 13, color: '#A99590' }}>没有找到相关记忆</div>}
          </Card>
        )}
      </div>

      {/* view switch */}
      <Card style={{ display: 'flex', gap: 4, borderRadius: 18, padding: 5, boxShadow: '0 6px 18px rgba(183,110,121,0.08)' }}>
        {VIEW_TABS.map((t) => {
          const active = view === t.key;
          return (
            <div
              key={t.key}
              onClick={() => setView(t.key)}
              style={{ cursor: 'pointer', flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 5, padding: '9px 0', borderRadius: 14, background: active ? 'rgba(183,110,121,0.12)' : 'transparent' }}
            >
              <span style={{ fontSize: 13, opacity: active ? 1 : 0.75 }}>{t.icon}</span>
              <span style={{ fontSize: 13, letterSpacing: 1, color: active ? '#9C3B4A' : '#8C7B76', fontWeight: active ? 600 : 400 }}>{t.label}</span>
            </div>
          );
        })}
      </Card>

      {/* unified filter console */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: -4 }}>
        <DragScrollRow
          label={
            <span style={{ flexShrink: 0, fontFamily: "'Bodoni Moda',serif", fontSize: 10, letterSpacing: 3, color: '#B9A8A2', width: 34 }}>状态</span>
          }
        >
          {stateTabs.map((t) => (
            <button
              key={t.key}
              type="button"
              data-filter-pill
              onPointerDown={(e) => e.stopPropagation()}
              onClick={t.pick}
              style={{ cursor: 'pointer', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, letterSpacing: 1, borderRadius: 999, padding: '6px 14px', background: t.active ? 'rgba(183,110,121,0.12)' : '#FFFFFF', color: t.active ? '#9C3B4A' : '#8C7B76', border: `1px solid ${t.active ? 'rgba(183,110,121,0.4)' : '#EFE3DE'}`, fontWeight: t.active ? 600 : 400 }}
            >
              <span>{t.label}</span>
              <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, opacity: 0.75 }}>{t.n}</span>
            </button>
          ))}
        </DragScrollRow>
        <DragScrollRow
          label={
            <span style={{ flexShrink: 0, fontFamily: "'Bodoni Moda',serif", fontSize: 10, letterSpacing: 3, color: '#B9A8A2', width: 34 }}>主题</span>
          }
        >
          {topicTabs.map((t) => (
            <button
              key={t.key}
              type="button"
              data-filter-pill
              onPointerDown={(e) => e.stopPropagation()}
              onClick={t.pick}
              style={{ cursor: 'pointer', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, letterSpacing: 1, borderRadius: 999, padding: '6px 14px', background: t.active ? 'rgba(183,110,121,0.12)' : '#FFFFFF', color: t.active ? '#9C3B4A' : '#8C7B76', border: `1px solid ${t.active ? 'rgba(183,110,121,0.4)' : '#EFE3DE'}`, fontWeight: t.active ? 600 : 400 }}
            >
              {t.emoji && <span style={{ fontSize: 12 }}>{t.emoji}</span>}
              <span>{t.label}</span>
              <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, opacity: 0.75 }}>{t.n}</span>
            </button>
          ))}
        </DragScrollRow>
        {showTagRow && (
          <DragScrollRow
            label={
              <span style={{ flexShrink: 0, fontFamily: "'Bodoni Moda',serif", fontSize: 10, letterSpacing: 3, color: '#B9A8A2', width: 34 }}>标签</span>
            }
          >
            {tagRow.map((t) => (
              <button
                key={t.label}
                type="button"
                data-filter-pill
                onPointerDown={(e) => e.stopPropagation()}
                onClick={t.pick}
                style={{ cursor: 'pointer', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, letterSpacing: 1, borderRadius: 10, padding: '4px 11px', background: t.bg, color: t.color, border: `1px solid ${t.border}`, fontWeight: t.bold ? 600 : 400, opacity: t.stale && !t.bold ? 0.5 : 1 }}
              >
                <span>#{t.label}</span>
                <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 10, opacity: 0.75 }}>{t.n}</span>
              </button>
            ))}
          </DragScrollRow>
        )}
      </div>

      {view === 'time' && renderTimeView()}
      {view === 'topic' && renderTopicView()}
      {view === 'star' && renderStarView()}

      {renderDrawer()}
    </ScreenLayout>
    </div>
  );
}

function MoonIcon({ info, style }: { info: MoonInfo; style?: CSSProperties }) {
  return (
    <span title={info.tip} style={{ display: 'inline-flex', flexShrink: 0, filter: info.glow, lineHeight: 0, ...style }}>
      <svg viewBox="0 0 24 24" style={{ width: info.size, height: info.size }}>
        <circle cx={12} cy={12} r={9} fill="none" stroke={info.stroke} strokeWidth={1.5} />
        <path d={info.d} fill={info.fill} />
      </svg>
    </span>
  );
}

function SectionDivider({ label, marginTop = 20 }: { label: string; marginTop?: number }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop }}>
      <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 11, letterSpacing: 3, color: '#B9A8A2', flexShrink: 0 }}>{label}</span>
      <span style={{ flex: 1, height: 1, background: '#F0E6E2' }} />
    </div>
  );
}
