import { expect, test } from '@playwright/test';
import { gotoLedger, installLedgerRoutes, MOCK_ENTRY_JUNE } from './ledger-helpers';

test.describe('LedgerScreen integration', () => {
  test('preserves old mem not in recent candidates after save', async ({ page }) => {
    const routes = await installLedgerRoutes(page);
    await gotoLedger(page);
    await page.getByTestId('ledger-entry-42').click();
    await page.getByText('修改').click();
    await page.getByTestId('ledger-save-button').click();
    await expect(page.getByTestId('ledger-drawer')).toHaveCount(0);
    const bodies = routes.getPatchBodies();
    expect(bodies.length).toBeGreaterThan(0);
    const meta = (bodies[0] as { meta?: { mem?: string } }).meta;
    expect(meta?.mem).toBe('旧记忆不在候选');
  });

  test('preserves old read when current co-read changed', async ({ page }) => {
    const routes = await installLedgerRoutes(page);
    await gotoLedger(page);
    await page.getByTestId('ledger-entry-42').click();
    await page.getByText('修改').click();
    await page.getByTestId('ledger-save-button').click();
    const meta = (routes.getPatchBodies()[0] as { meta?: { read?: string } }).meta;
    expect(meta?.read).toBe('《旧书》 · p.3');
    await expect(page.getByText('《新书》')).toHaveCount(0);
  });

  test('removes mem/read from PATCH after user cancels links', async ({ page }) => {
    const routes = await installLedgerRoutes(page);
    await gotoLedger(page);
    await page.getByTestId('ledger-entry-42').click();
    await page.getByText('修改').click();
    const drawer = page.getByTestId('ledger-drawer');
    await drawer.locator('div').filter({ hasText: '已关联记忆' }).getByText('取消').click();
    await drawer.locator('div').filter({ hasText: '《旧书》' }).getByText('取消').click();
    await page.getByTestId('ledger-save-button').click();
    const meta = (routes.getPatchBodies()[0] as { meta?: Record<string, string> }).meta || {};
    expect(meta.mem).toBeUndefined();
    expect(meta.read).toBeUndefined();
  });

  test('POST failure leaves no new record and keeps drawer open with error', async ({ page }) => {
    await installLedgerRoutes(page, { postFail: true });
    await gotoLedger(page);
    await page.getByText('＋ 记一笔').click();
    await page.locator('input[placeholder="0.00"]').fill('12');
    await page.getByTestId('ledger-save-button').click();
    await expect(page.getByTestId('ledger-error-banner')).toContainText('保存失败');
    await expect(page.getByTestId('ledger-drawer')).toBeVisible();
    await expect(page.getByText('小面和冰豆花')).toHaveCount(0);
    await expect(page.getByText('测试面')).toHaveCount(1);
  });

  test('PATCH failure restores previous entry content', async ({ page }) => {
    await installLedgerRoutes(page, { patchFail: true });
    await gotoLedger(page);
    await page.getByTestId('ledger-entry-42').click();
    await page.getByText('修改').click();
    await page.locator('input[placeholder="买了什么、为什么买"]').fill('不应留下');
    await page.getByTestId('ledger-save-button').click();
    await expect(page.getByTestId('ledger-error-banner')).toContainText('保存失败');
    await expect(page.getByText('测试面')).toBeVisible();
    await expect(page.getByText('不应留下')).toHaveCount(0);
  });

  test('DELETE failure keeps the record visible', async ({ page }) => {
    await installLedgerRoutes(page, { deleteFail: true });
    await gotoLedger(page);
    await page.getByTestId('ledger-entry-42').click();
    await page.getByText('删除').click();
    await expect(page.getByTestId('ledger-error-banner')).toContainText('删除失败');
    await expect(page.getByTestId('ledger-entry-42')).toBeVisible();
  });

  test('budget save failure keeps sheet open and shows error', async ({ page }) => {
    await installLedgerRoutes(page, { budgetPostFail: true });
    await gotoLedger(page);
    await page.getByTestId('ledger-adjust-budget').click();
    await page.getByTestId('ledger-budget-input').fill('4500');
    await page.getByTestId('ledger-budget-save-button').click();
    await expect(page.getByTestId('ledger-error-banner')).toContainText('预算保存失败');
    await expect(page.getByTestId('ledger-budget-sheet')).toBeVisible();
  });

  test('GET entries failure does not show mock demo records', async ({ page }) => {
    await installLedgerRoutes(page, { entriesFail: true, budgetAmount: 5000 });
    await gotoLedger(page);
    await expect(page.getByText('账目加载失败')).toBeVisible();
    await expect(page.getByText('小面和冰豆花')).toHaveCount(0);
    await expect(page.getByText('项目款到账')).toHaveCount(0);
  });

  test('GET budget failure hides ring, defaults, and old budget amounts', async ({ page }) => {
    await installLedgerRoutes(page, { budgetFail: true });
    await gotoLedger(page);
    await expect(page.getByTestId('ledger-budget-unavailable')).toBeVisible();
    await expect(page.getByTestId('ledger-budget-ring')).toHaveCount(0);
    await expect(page.getByText('3,000')).toHaveCount(0);
    await expect(page.getByText('还可以花')).toHaveCount(0);
    await expect(page.getByText('%')).toHaveCount(0);
  });

  test('stale July failure cannot overwrite June after month switch', async ({ page }) => {
    await installLedgerRoutes(page, {
      patchFail: true,
      patchDelayMs: 2500,
      juneEntries: [{ ...MOCK_ENTRY_JUNE }],
      juneBudgetAmount: 4000,
    });
    await gotoLedger(page);
    await expect(page.getByText('测试面')).toBeVisible();
    await page.getByTestId('ledger-entry-42').click();
    await page.getByText('修改').click();
    await page.locator('input[placeholder="买了什么、为什么买"]').fill('七月失败覆盖');
    const patchDone = page.waitForResponse((res) => res.url().includes('/api/ledger/42') && res.request().method() === 'PATCH');
    await page.getByTestId('ledger-save-button').click();
    await page.evaluate(() => {
      const drawer = document.querySelector('[data-testid="ledger-drawer"]');
      const overlay = drawer?.previousElementSibling as HTMLElement | null;
      overlay?.click();
    });
    await page.getByTestId('ledger-month-gauge').getByText('‹').click();
    await expect(page.getByText('2026.06')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('六月午餐')).toBeVisible({ timeout: 10_000 });
    await patchDone;
    await page.waitForTimeout(300);
    await expect(page.getByText('六月午餐')).toBeVisible();
    await expect(page.getByText('七月失败覆盖')).toHaveCount(0);
    await expect(page.getByText('测试面')).toHaveCount(0);
  });
});
