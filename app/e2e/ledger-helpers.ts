import { expect, type Page, type Route } from '@playwright/test';

export const MOCK_ENTRY = {
  id: 42,
  amount: -68,
  category: '餐饮',
  note: '测试面',
  date: '2026-07-10',
  author: 'both',
  meta: JSON.stringify({
    who: 'both',
    reason: '想吃',
    mem: '旧记忆不在候选',
    read: '《旧书》 · p.3',
  }),
};

export const MOCK_ENTRY_JUNE = {
  id: 7,
  amount: -20,
  category: '餐饮',
  note: '六月午餐',
  date: '2026-06-12',
  author: 'both',
  meta: JSON.stringify({ who: 'both', reason: '必需' }),
};

const EMPTY_SUMMARY = {
  core: 0,
  long: 0,
  recent: 0,
  gradient: 0.5,
  sections: [
    { key: 'recent', title: '最近', count: 0, items: [] },
    { key: 'core', title: '核心', count: 0, items: [] },
    { key: 'long', title: '长期', count: 0, items: [] },
  ],
};

const CURRENT_BOOK = {
  book: { title: '新书', author: '作者', read: 12, total: 200 },
};

const TREND_PAYLOAD = [
  { month: '2026-02', expense: 100 },
  { month: '2026-03', expense: 120 },
  { month: '2026-04', expense: 130 },
  { month: '2026-05', expense: 140 },
  { month: '2026-06', expense: 150 },
  { month: '2026-07', expense: 68 },
];

export interface LedgerRouteOptions {
  month?: string;
  entries?: Array<typeof MOCK_ENTRY>;
  budgetAmount?: number | null;
  entriesFail?: boolean;
  budgetFail?: boolean;
  postFail?: boolean;
  patchFail?: boolean;
  deleteFail?: boolean;
  budgetPostFail?: boolean;
  patchDelayMs?: number;
  entriesDelayMs?: number;
  juneEntries?: Array<typeof MOCK_ENTRY_JUNE>;
  juneBudgetAmount?: number | null;
}

export interface LedgerRequestCounts {
  entriesGet: number;
  budgetGet: number;
  trendGet: number;
  post: number;
  patch: number;
  delete: number;
  budgetPost: number;
}

export type ExpectedLedgerRequests = Partial<LedgerRequestCounts>;

export interface LedgerRouteHandle {
  counts: LedgerRequestCounts;
  assertExpected: (expected: ExpectedLedgerRequests) => void;
  getPatchBodies: () => unknown[];
  getStoredEntries: () => Array<typeof MOCK_ENTRY>;
}

function ledgerPathname(url: string): string {
  return new URL(url).pathname.replace(/\/$/, '');
}

export async function installLedgerRoutes(page: Page, opts: LedgerRouteOptions = {}): Promise<LedgerRouteHandle> {
  const month = opts.month ?? '2026-07';
  const entries = opts.entries ?? [MOCK_ENTRY];
  const juneEntries = opts.juneEntries ?? [MOCK_ENTRY_JUNE];
  const budgetAmount = opts.budgetAmount === undefined ? 5000 : opts.budgetAmount;
  const juneBudgetAmount = opts.juneBudgetAmount === undefined ? 4000 : opts.juneBudgetAmount;
  let storedEntries = [...entries];
  const patchBodies: unknown[] = [];

  const counts: LedgerRequestCounts = {
    entriesGet: 0,
    budgetGet: 0,
    trendGet: 0,
    post: 0,
    patch: 0,
    delete: 0,
    budgetPost: 0,
  };

  const failUnhandled = async (route: Route, detail: string) => {
    await route.fulfill({ status: 599, contentType: 'text/plain', body: `unhandled ledger mock: ${detail}` });
    throw new Error(`Unhandled /api/ledger* request: ${detail}`);
  };

  const fulfillEntriesGet = async (route: Route, reqMonth: string) => {
    counts.entriesGet += 1;
    if (opts.entriesDelayMs) await new Promise((r) => setTimeout(r, opts.entriesDelayMs));
    if (opts.entriesFail) {
      await route.fulfill({ status: 500, body: 'fail' });
      return;
    }
    const records = reqMonth === '2026-06' ? juneEntries : storedEntries.filter((e) => e.date.startsWith(reqMonth));
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ records, summary: { income: 0, expense: -68, balance: -68, prev_expense: 0 } }),
    });
  };

  const fulfillBudgetGet = async (route: Route, reqMonth: string) => {
    counts.budgetGet += 1;
    if (opts.budgetFail) {
      await route.fulfill({ status: 500, body: 'fail' });
      return;
    }
    const amount = reqMonth === '2026-06' ? juneBudgetAmount : budgetAmount;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ amount }) });
  };

  await page.route('**/api/posts/summary', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_SUMMARY) });
  });
  await page.route('**/api/books/current', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CURRENT_BOOK) });
  });

  await page.route('**/api/ledger**', async (route) => {
    const url = route.request().url();
    if (!url.includes('/api/ledger')) {
      await failUnhandled(route, url);
      return;
    }

    const pathname = ledgerPathname(url);
    const method = route.request().method();
    const search = new URL(url).searchParams;

    if (pathname === '/api/ledger/trend' && method === 'GET') {
      counts.trendGet += 1;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(TREND_PAYLOAD) });
      return;
    }

    if (pathname === '/api/ledger/budget' && method === 'GET') {
      const reqMonth = search.get('month') || month;
      await fulfillBudgetGet(route, reqMonth);
      return;
    }

    if (pathname === '/api/ledger/budget' && method === 'POST') {
      counts.budgetPost += 1;
      if (opts.budgetPostFail) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ ok: false }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }

    if (pathname === '/api/ledger' && method === 'GET') {
      const reqMonth = search.get('month') || month;
      await fulfillEntriesGet(route, reqMonth);
      return;
    }

    if (pathname === '/api/ledger' && method === 'POST') {
      counts.post += 1;
      if (opts.postFail) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ ok: false }) });
        return;
      }
      const body = route.request().postDataJSON() as { note?: string };
      const id = Date.now();
      storedEntries = [
        {
          id,
          amount: -10,
          category: '餐饮',
          note: body.note || '新记录',
          date: `${month}-15`,
          author: 'both',
          meta: JSON.stringify({ who: 'both', reason: '必需' }),
        },
        ...storedEntries,
      ];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, id }) });
      return;
    }

    const entryMatch = pathname.match(/^\/api\/ledger\/(\d+)$/);
    if (entryMatch && method === 'PATCH') {
      counts.patch += 1;
      if (opts.patchDelayMs) await new Promise((r) => setTimeout(r, opts.patchDelayMs));
      patchBodies.push(route.request().postDataJSON());
      if (opts.patchFail) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ ok: false }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }

    if (entryMatch && method === 'DELETE') {
      counts.delete += 1;
      if (opts.deleteFail) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ ok: false }) });
        return;
      }
      const id = Number(entryMatch[1]);
      storedEntries = storedEntries.filter((e) => e.id !== id);
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }

    await failUnhandled(route, `${method} ${pathname}`);
  });

  return {
    counts,
    assertExpected(expected: ExpectedLedgerRequests, opts?: { exact?: Array<keyof LedgerRequestCounts> }) {
      const exact = new Set(opts?.exact ?? ['post', 'patch', 'delete', 'budgetPost']);
      for (const [key, value] of Object.entries(expected) as Array<[keyof LedgerRequestCounts, number]>) {
        if (exact.has(key)) {
          expect(counts[key], `expected ${key}=${value}`).toBe(value);
        } else {
          expect(counts[key], `expected ${key}>=${value}`).toBeGreaterThanOrEqual(value);
        }
      }
    },
    getPatchBodies: () => patchBodies,
    getStoredEntries: () => storedEntries,
  };
}

/** Default ledger page-load traffic: one entries GET, one budget GET, one trend GET. */
export const LEDGER_PAGE_LOAD_REQUESTS: ExpectedLedgerRequests = {
  entriesGet: 1,
  budgetGet: 1,
  trendGet: 1,
};

export async function gotoLedger(page: Page) {
  await page.goto('/dash/ledger');
  await page.getByTestId('ledger-month-gauge').waitFor({ state: 'visible' });
}
