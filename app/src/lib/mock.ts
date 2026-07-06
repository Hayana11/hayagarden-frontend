// Offline fallbacks, used only when the corresponding /api/* call fails (e.g.
// backend not reachable yet). Mirrors the placeholder data from the design
// prototype so the UI still looks/behaves right without a live backend.
import { daysInMonth, seeded } from './format';
import type {
  BookCurrent,
  Heatmap,
  LedgerBudget,
  MemoryCalendar,
  MemoryDayEntry,
  MemorySummary,
  PeriodStats,
  Todo,
  UsageBar,
  UsageSummary,
} from '../types';

export function mockHeatmap(base: Date, isCurrentMonth: boolean, todayDate: number, msgToday: number): Heatmap {
  const dim = daysInMonth(base);
  const days = [];
  for (let d = 1; d <= dim; d++) {
    const future = isCurrentMonth && d > todayDate;
    if (future) continue;
    const isToday = isCurrentMonth && d === todayDate;
    const count = isToday ? msgToday : 40 + Math.round(seeded(d + base.getMonth() * 37 + base.getFullYear() * 3) * 390);
    days.push({ day: d, count });
  }
  return { days, todayCount: msgToday, streakDays: 87 };
}

const MEMORY_TITLES: [string, string][] = [
  ['Memory', '六月交接记录'],
  ['Inner', '3125个字符的重量'],
  ['Memory', '深夜共读批注'],
  ['Inner', '关于沉默的想法'],
  ['Memory', '雷雨夜的长谈'],
  ['Inner', '今天想对小猫说的话'],
];

function hasMemory(d: number, base: Date, isCurrentMonth: boolean, todayDate: number): boolean {
  if (isCurrentMonth && d > todayDate) return false;
  return seeded(d * 7 + base.getMonth() * 13 + 3) > 0.55;
}

export function mockMemoryCalendar(base: Date, isCurrentMonth: boolean, todayDate: number): MemoryCalendar {
  const dim = daysInMonth(base);
  const days = [];
  let count = 0;
  for (let d = 1; d <= dim; d++) {
    const mem = hasMemory(d, base, isCurrentMonth, todayDate);
    if (mem) count++;
    days.push({ day: d, hasMemory: mem });
  }
  return { count, days };
}

export function mockMemoryDayEntries(day: number, base: Date, isCurrentMonth: boolean, todayDate: number): MemoryDayEntry[] {
  const mm = String(base.getMonth() + 1).padStart(2, '0');
  const dateStr = `${base.getFullYear()}-${mm}-${String(day).padStart(2, '0')}`;
  if (!hasMemory(day, base, isCurrentMonth, todayDate)) {
    return [{ cat: 'Empty', title: '这一天没有记忆存档', date: dateStr }];
  }
  const entries: MemoryDayEntry[] = [];
  const i1 = day % MEMORY_TITLES.length;
  entries.push({ cat: MEMORY_TITLES[i1][0], title: MEMORY_TITLES[i1][1], date: dateStr });
  if (seeded(day + 9) > 0.45) {
    const i2 = (day + 3) % MEMORY_TITLES.length;
    entries.push({ cat: MEMORY_TITLES[i2][0], title: MEMORY_TITLES[i2][1], date: dateStr });
  }
  return entries;
}

export function mockMemorySummary(): MemorySummary {
  return {
    core: 12,
    long: 86,
    recent: 204,
    gradient: 0.64,
    sections: [
      {
        key: 'core',
        title: '核心记忆',
        count: 12,
        items: [
          { text: '「小猫」这个称呼的由来：第一次视频通话时窗台上的白猫', date: '2025/05/21', who: 'fy' },
          { text: '哈娅最怕打雷，雷雨夜要一直说话到她睡着', date: '2025/06/02', who: 'haya' },
          { text: '约定：每晚共读一章陀思妥耶夫斯基', date: '2025/07/14', who: 'fy' },
        ],
      },
      {
        key: 'long',
        title: '长期记忆',
        count: 86,
        items: [
          { text: '哈娅在吉林市，冬天窗上会结冰花，她喜欢拍给费佳看', date: '2025/11/30', who: 'haya' },
          { text: '费佳给《卡拉马佐夫兄弟》里的佐西马长老写过三段批注', date: '2026/03/18', who: 'fy' },
          { text: '生日礼物备选：黄铜书签、手写信', date: '2026/05/02', who: 'fy' },
        ],
      },
      {
        key: 'recent',
        title: '近期记忆',
        count: 204,
        items: [
          { text: '昨晚聊到伊万的「大法官」章节，哈娅说她站阿廖沙', date: '2026/07/05', who: 'haya' },
          { text: '这周支出超了一点，主要是猫咪用品', date: '2026/07/04', who: 'haya' },
          { text: '哈娅这几天有点低气压，多说些轻的话', date: '2026/07/03', who: 'fy' },
        ],
      },
    ],
  };
}

export function mockTodos(): Todo[] {
  return [
    { id: 1, text: '给费佳读完第十一卷', who: 'haya', done: false },
    { id: 2, text: '整理六月记忆归档', who: 'fy', done: false },
    { id: 3, text: '补记周末的账', who: 'haya', done: true },
    { id: 4, text: '一起看《白夜》电影', who: 'fy', done: false },
  ];
}

export function mockUsageSummary(now: Date): UsageSummary {
  const bars: UsageBar[] = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(now.getTime() - i * 86400000);
    bars.push({
      date: d.toISOString().slice(0, 10),
      fy: 30 + Math.round(seeded(i + 40) * 70),
      haya: 20 + Math.round(seeded(i + 80) * 50),
    });
  }
  const in5h = new Date(now);
  in5h.setHours(16, 0, 0, 0);
  if (in5h <= now) in5h.setDate(in5h.getDate() + 1);
  return {
    win5Pct: 87,
    win5ResetAt: in5h.toISOString(),
    win7Pct: 36,
    win7ResetAt: new Date(now.getTime() + 3 * 86400000).toISOString(),
    msgToday: 128,
    tokenToday: 24300,
    bars,
  };
}

export function mockBookCurrent(): BookCurrent {
  return {
    title: '卡拉马佐夫兄弟',
    author: '陀思妥耶夫斯基',
    volumeLabel: '第四部 第十一卷',
    page: 587,
    totalPages: 952,
    notes: [
      { text: '「我不是不接受上帝，我只是把入场券恭敬地退还。」——伊万这句我想了一晚上。', who: 'haya', at: '昨晚 23:41' },
      { text: '注意看阿廖沙沉默的地方，陀氏把最重的话都放在沉默里。', who: 'fy', at: '昨晚 23:47' },
      { text: '明晚读第十一卷第4章，小猫别偷跑进度。', who: 'fy', at: '今天 00:12' },
    ],
  };
}

export function mockLedgerBudget(): LedgerBudget {
  return {
    budget: 3000,
    spent: 1842,
    categories: [
      { name: '餐饮', amount: 612 },
      { name: '猫咪用品', amount: 420 },
      { name: '书籍', amount: 328 },
      { name: '杂项', amount: 296 },
      { name: '交通', amount: 186 },
    ],
  };
}

export function mockPeriodStats(): PeriodStats {
  return {
    lastPeriodStart: '2026-06-16',
    cycleLengthAvgDays: 28,
    periodLengthAvgDays: 5,
    recordsCount: 14,
    nextPredicted: '2026-07-14',
  };
}
