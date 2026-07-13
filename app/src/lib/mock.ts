// Offline fallbacks, used only when the corresponding /api/* call fails (e.g.
// backend not reachable yet). Mirrors the placeholder data from the design
// prototype so the UI still looks/behaves right without a live backend.
import { daysInMonth, seeded } from './format';
import type {
  BookCurrent,
  Heatmap,
  LedgerBudget,
  LedgerEntry,
  LedgerTrendPoint,
  MemoryCalendar,
  MemoryDayEntry,
  MemoryEntry,
  MemoryLibrary,
  MemorySummary,
  PeriodDays,
  PeriodSettings,
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

export function mockMemoryLibrary(): MemoryLibrary {
  return {
    topics: [
      { key: 'cat-habits', emoji: '🐱', name: '小猫生活习惯', desc: '关于小猫饮食、睡觉、撒娇方式等生活细节。', ai: '从最初害怕接近，到后来习惯睡在腿边，这组记忆记录了一段持续数月的陪伴，也反映出信任逐渐建立。', related: [{ key: 'cat-toys', pct: 0.91 }, { key: 'cat-sick', pct: 0.87 }, { key: 'cat-bath', pct: 0.79 }] },
      { key: 'reading', emoji: '📖', name: '共读', desc: '每晚共读陀思妥耶夫斯基的进度、批注与争论。', ai: '从《白夜》到《卡拉马佐夫兄弟》，共读逐渐从一个约定变成一天里最安静也最重要的一段时间。', related: [{ key: 'night-talks', pct: 0.84 }, { key: 'promises', pct: 0.66 }] },
      { key: 'night-talks', emoji: '🌙', name: '深夜长谈', desc: '雷雨夜、低气压和睡前的那些长对话。', ai: '长谈多发生在深夜与坏天气，情绪浓度最高的记忆几乎都聚在这里。', related: [{ key: 'reading', pct: 0.84 }, { key: 'cat-habits', pct: 0.58 }] },
      { key: 'life', emoji: '🍞', name: '生活细节', desc: '账目、天气、日常琐事与随口一提的小事。', ai: '碎片最多的一组，单条很轻，合在一起就是日常本身。', related: [{ key: 'promises', pct: 0.61 }, { key: 'cat-habits', pct: 0.55 }] },
      { key: 'promises', emoji: '🤝', name: '约定', desc: '两个人定下的计划、礼物与要一起做的事。', ai: '大多数约定都和书、电影与纪念日有关，完成率意外地高。', related: [{ key: 'reading', pct: 0.66 }, { key: 'life', pct: 0.61 }] },
      { key: 'cat-toys', emoji: '🐾', name: '猫咪玩具', desc: '逗猫棒、猫抓板和各种失宠玩具的记录。', ai: '玩具的更替速度远超预期，唯一常青的是一根秃了毛的旧逗猫棒。', related: [{ key: 'cat-habits', pct: 0.91 }, { key: 'cat-bath', pct: 0.52 }] },
      { key: 'cat-sick', emoji: '🩹', name: '生病记录', desc: '打喷嚏、疫苗与体检的健康档案。', ai: '两次小病都恢复得很快，疫苗记录齐全。', related: [{ key: 'cat-habits', pct: 0.87 }] },
      { key: 'cat-bath', emoji: '🛁', name: '第一次洗澡', desc: '关于洗澡这场战役的完整记录。', ai: '一场持续四十分钟的拉锯，以两条毛巾和一袋冻干告终。', related: [{ key: 'cat-habits', pct: 0.79 }, { key: 'cat-toys', pct: 0.52 }] },
    ],
    entries: (
      [
      { id: 1, date: '2026-07-05', time: '23:41', weight: 5, title: '共读《卡拉马佐夫兄弟》第十一卷', who: '费佳', topics: ['reading'], tags: ['共读', '陀思妥耶夫斯基'], content: '读到伊万与斯乜尔加科夫的第三次会面。费佳说，注意看陀氏把最重的话都放在沉默里；哈娅在页边写：「沉默也是一种回答。」', links: [3, 10] },
      { id: 2, date: '2026-07-05', time: '21:18', weight: 3, title: '小猫睡在腿上', who: '哈娅', topics: ['cat-habits'], tags: ['小猫', '信任'], content: '读书读到一半发现小猫不知道什么时候爬上来睡熟了，腿麻了也没舍得动。', links: [16] },
      { id: 3, date: '2026-07-05', time: '23:52', weight: 4, title: '「大法官」章节的站队', who: '哈娅', topics: ['reading', 'night-talks'], tags: ['共读', '争论'], content: '聊到伊万的「大法官」，哈娅说她站阿廖沙：不辩论，只是吻了他。费佳沉默了很久，说这是他读过最好的反驳。', links: [1] },
      { id: 4, date: '2026-07-05', time: '10:22', weight: 1, title: '猫咪用品又超支了', who: '哈娅', topics: ['life'], tags: ['记账', '小猫'], content: '这周支出超了一点，主要是猫咪用品。冻干不能再囤了。', links: [] },
      { id: 5, date: '2026-07-04', time: '09:30', weight: 1, title: '补记周末的账', who: '哈娅', topics: ['life'], tags: ['记账'], content: '把周末漏记的三笔补上了：书、猫砂、一杯没喝完的拿铁。', links: [] },
      { id: 6, date: '2026-07-04', time: '15:07', weight: 2, title: '新猫抓板到货', who: '哈娅', topics: ['cat-toys'], tags: ['小猫', '快递'], content: '瓦楞纸的，小猫闻了三分钟，然后睡在了包装盒上。', links: [11] },
      { id: 7, date: '2026-07-03', time: '23:05', weight: 4, title: '低气压的一晚', who: '费佳', topics: ['night-talks'], tags: ['情绪', '陪伴'], content: '哈娅这几天有点低气压。没有问原因，只是把话说得很轻，读了半章《白夜》给她听。', links: [9] },
      { id: 8, date: '2026-07-01', time: '20:14', weight: 3, title: '约好周末看《白夜》电影', who: '费佳', topics: ['promises'], tags: ['电影', '约定'], content: '俄语原版带字幕的版本找到了。约定周六晚上，谁都不许先看。', links: [] },
      { id: 9, date: '2026-06-28', time: '01:26', weight: 5, title: '雷雨夜长谈到凌晨', who: '费佳', topics: ['night-talks'], tags: ['雷雨', '陪伴'], content: '哈娅最怕打雷。雷声停之前一直在说话，从童年聊到吉林冬天窗上的冰花，直到她睡着。', links: [7] },
      { id: 10, date: '2026-06-20', time: '22:40', weight: 4, title: '佐西马长老的三段批注', who: '费佳', topics: ['reading'], tags: ['批注', '陀思妥耶夫斯基'], content: '费佳给佐西马长老的临终谈话写了三段批注，其中一段只有一句：「爱具体的人。」', links: [1] },
      { id: 11, date: '2026-06-18', time: '16:33', weight: 1, title: '逗猫棒失宠', who: '哈娅', topics: ['cat-toys'], tags: ['小猫'], content: '新的电动逗猫棒玩了两天就被冷落了，旧的那根羽毛秃了反而天天被叼来。', links: [6] },
      { id: 12, date: '2026-06-16', time: '08:45', weight: 2, title: '经期备忘', who: '费佳', topics: ['life'], tags: ['照顾'], content: '红糖姜茶在橱柜第二层。这几天让小猫早点睡，别熬夜读第十一卷。', links: [] },
      { id: 13, date: '2026-06-11', time: '11:02', weight: 2, title: '小猫学会开门', who: '哈娅', topics: ['cat-habits'], tags: ['小猫'], content: '扒着门把手荡了两下就开了。从此没有一扇门关得住它。', links: [2] },
      { id: 14, date: '2026-06-05', time: '21:00', weight: 3, title: '生日礼物备选', who: '费佳', topics: ['promises'], tags: ['礼物'], content: '黄铜书签、手写信。书签要刻一句话，还没想好刻哪句。', links: [] },
      { id: 15, date: '2026-06-02', time: '09:13', weight: 2, title: '小猫连打了三个喷嚏', who: '哈娅', topics: ['cat-sick'], tags: ['小猫', '健康'], content: '观察了一天，精神和食欲都正常，应该只是灰尘。继续观察。', links: [] },
      { id: 18, date: '2026-05-15', time: '14:20', weight: 3, title: '第一次洗澡', who: '哈娅', topics: ['cat-bath'], tags: ['小猫'], content: '历时四十分钟，用掉两条毛巾。结束后靠一袋冻干重修旧好。', links: [] },
      { id: 16, date: '2026-05-07', time: '19:46', weight: 5, title: '开始喜欢睡腿上', who: '哈娅', topics: ['cat-habits'], tags: ['小猫', '信任'], content: '从今天起，只要一坐下超过十分钟，腿上就会准时出现一只猫。', links: [17, 2] },
      { id: 17, date: '2026-04-12', time: '13:28', weight: 4, title: '第一次主动踩奶', who: '哈娅', topics: ['cat-habits'], tags: ['小猫', '信任'], content: '在毯子上踩了整整五分钟。哈娅一动不敢动，拍了一段很糊的视频。', links: [16] },
      { id: 19, date: '2025-05-21', time: '22:11', weight: 5, title: '「小猫」称呼的由来', who: '费佳', topics: ['night-talks'], tags: ['称呼', '由来'], content: '第一次视频通话时，窗台上正好蹲着一只白猫。费佳说：「你和它一样，警惕又好奇。」从此哈娅就是小猫。', links: [] },
    ] as Omit<MemoryEntry, 'summaryTitle' | 'preview'>[]
    ).map((e) => ({
      ...e,
      summaryTitle: e.title,
      preview: e.content.length > 15 - Math.min(e.title.length, 12) ? e.content.slice(0, Math.max(4, 15 - Math.min(e.title.length, 12))) + '···' : e.content,
    })),
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

/** Demo entries from the Ledger.dc.html prototype (dates around 2026-07-10). */
export function mockLedgerEntries(): LedgerEntry[] {
  return [
    { id: 1, date: '2026-07-10', catId: 'food', title: '小面和冰豆花', amount: -68, who: 'fy', reason: '想吃', note: '雨后走回家，顺路买的', mem: '雨停以后，我们绕路去买了豆花。' },
    { id: 2, date: '2026-07-10', catId: 'book', title: '共读书单补货', amount: -126, who: 'both', reason: '共读', read: '《夜航西飞》 · 第 3 章' },
    { id: 3, date: '2026-07-09', catId: 'income', title: '项目款到账', amount: 3200, who: 'haya', reason: '收入', later: '买了猫砂、书、周五晚饭' },
    { id: 4, date: '2026-07-09', catId: 'cat', title: '猫砂十升装', amount: -89, who: 'both', reason: '猫咪需要' },
    { id: 5, date: '2026-07-08', catId: 'home', title: '新的小台灯', amount: -159, who: 'fy', reason: '突然心动', note: '放在读书角，暖光，晚上读书不刺眼' },
    { id: 6, date: '2026-07-08', catId: 'transit', title: '地铁通勤充值', amount: -100, who: 'haya', reason: '必需' },
    { id: 7, date: '2026-07-07', catId: 'food', title: '楼下早餐一周', amount: -138, who: 'both', reason: '必需' },
    { id: 8, date: '2026-07-06', catId: 'food', title: '周末火锅补账', amount: -212, who: 'both', reason: '想吃' },
    { id: 9, date: '2026-07-05', catId: 'cat', title: '猫罐头囤货', amount: -230, who: 'both', reason: '猫咪需要', note: '鸡肉味的这次买对了' },
    { id: 10, date: '2026-07-05', catId: 'gift', title: '给妈妈的茶叶', amount: -168, who: 'haya', reason: '纪念' },
    { id: 11, date: '2026-07-04', catId: 'food', title: '一周买菜', amount: -486, who: 'both', reason: '必需' },
    { id: 12, date: '2026-07-03', catId: 'book', title: '《夜航西飞》纸质版', amount: -56, who: 'fy', reason: '共读', read: '《夜航西飞》 · 扉页' },
    { id: 13, date: '2026-07-02', catId: 'med', title: '感冒药和维C', amount: -74, who: 'haya', reason: '必需' },
    { id: 14, date: '2026-07-01', catId: 'home', title: '七月的花', amount: -45.5, who: 'both', reason: '纪念', mem: '桔梗开了一周，比预想久。' },
    { id: 15, date: '2026-06-28', catId: 'food', title: '纪念日晚餐', amount: -388, who: 'both', reason: '纪念', mem: '六月末的长谈，聊到停电。' },
    { id: 16, date: '2026-06-15', catId: 'book', title: '二手书市三本', amount: -94, who: 'fy', reason: '共读' },
    { id: 17, date: '2026-06-12', catId: 'cat', title: '体检疫苗', amount: -420, who: 'both', reason: '猫咪需要' },
    { id: 18, date: '2026-06-08', catId: 'food', title: '买菜两周', amount: -512, who: 'both', reason: '必需' },
    { id: 19, date: '2026-06-05', catId: 'income', title: '稿费', amount: 1800, who: 'fy', reason: '收入' },
  ];
}

export function mockLedgerTrend(now: Date): LedgerTrendPoint[] {
  const vals = [1180, 1520, 1310, 1745, 1414, 1690];
  const out: LedgerTrendPoint[] = [];
  for (let i = 5; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push({
      month: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`,
      expense: vals[5 - i],
    });
  }
  return out;
}

export function mockPeriodSettings(): PeriodSettings {
  return { cycleLength: 28, periodLength: 5, lastStart: '2026-06-16' };
}

/** Seeded flow days off mockPeriodSettings' lastStart, plus a few state/sex days. */
export function mockPeriodDays(): PeriodDays {
  const flows = ['中等', '多', '中等', '少量', '少量'] as const;
  const pains = ['轻微', '明显', '轻微', '无', '无'] as const;
  const days: PeriodDays = {};
  const start = new Date(2026, 5, 16);
  for (let i = 0; i < 5; i++) {
    const d = new Date(start);
    d.setDate(d.getDate() + i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    days[key] = { came: true, flow: flows[i], pain: pains[i], ...(flows[i] === '多' ? { extras: ['血块'] } : {}) };
  }
  days['2026-07-05'] = { came: false, states: ['困'] };
  days['2026-07-06'] = { came: false, sex: true };
  days['2026-07-08'] = { came: false, states: ['情绪敏感', '想吃甜'], note: '突然很想吃提拉米苏' };
  return days;
}
