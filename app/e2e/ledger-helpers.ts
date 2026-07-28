import type { Page, Route } from '@playwright/test';

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

export async function installLedgerRoutes(page: Page, opts: LedgerRouteOptions = {}) {
  const month = opts.month ?? '2026-07';
  const entries = opts.entries ?? [MOCK_ENTRY];
  const juneEntries = opts.juneEntries ?? [MOCK_ENTRY_JUNE];
  const budgetAmount = opts.budgetAmount === undefined ? 5000 : opts.budgetAmount;
  const juneBudgetAmount = opts.juneBudgetAmount === undefined ? 4000 : opts.juneBudgetAmount;
  let storedEntries = [...entries];
  const patchBodies: unknown[] = [];

  await page.route('**/api/posts/summary', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_SUMMARY) });
  });
  await page.route('**/api/books/current', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CURRENT_BOOK) });
  });
  await page.route('**/api/ledger/trend', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { month: '2026-02', expense: 100 },
        { month: '2026-03', expense: 120 },
        { month: '2026-04', expense: 130 },
        { month: '2026-05', expense: 140 },
        { month: '2026-06', expense: 150 },
        { month: '2026-07', expense: 68 },
      ]),
    });
  });

  const ledgerGet = async (route: Route) => {
    const url = new URL(route.request().url());
    const reqMonth = url.searchParams.get('month') || month;
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

  await page.route('**/api/ledger?*', ledgerGet);
  await page.route('**/api/ledger/budget?*', async (route) => {
    const url = new URL(route.request().url());
    const reqMonth = url.searchParams.get('month') || month;
    if (opts.budgetFail) {
      await route.fulfill({ status: 500, body: 'fail' });
      return;
    }
    const amount = reqMonth === '2026-06' ? juneBudgetAmount : budgetAmount;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ amount }) });
  });

  await page.route('**/api/ledger', async (route) => {
    if (route.request().method() === 'GET') return ledgerGet(route);
    if (route.request().method() === 'POST') {
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
    await route.continue();
  });

  await page.route('**/api/ledger/*', async (route) => {
    const method = route.request().method();
    if (method === 'PATCH') {
      if (opts.patchDelayMs) await new Promise((r) => setTimeout(r, opts.patchDelayMs));
      patchBodies.push(route.request().postDataJSON());
      if (opts.patchFail) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ ok: false }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }
    if (method === 'DELETE') {
      if (opts.deleteFail) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ ok: false }) });
        return;
      }
      const id = Number(route.request().url().split('/').pop());
      storedEntries = storedEntries.filter((e) => e.id !== id);
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }
    await route.continue();
  });

  await page.route('**/api/ledger/budget', async (route) => {
    if (route.request().method() === 'POST') {
      if (opts.budgetPostFail) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ ok: false }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }
    await route.continue();
  });

  return {
    getPatchBodies: () => patchBodies,
    getStoredEntries: () => storedEntries,
  };
}

export async function gotoLedger(page: Page) {
  await page.goto('/dash/ledger');
  await page.getByTestId('ledger-month-gauge').waitFor({ state: 'visible' });
}
