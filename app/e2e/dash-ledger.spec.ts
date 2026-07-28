import { expect, test } from '@playwright/test';
import { installLedgerRoutes } from './ledger-helpers';

async function gotoDash(page: import('@playwright/test').Page) {
  await page.goto('/dash/');
  await page.getByTestId('dash-ledger-card').waitFor({ state: 'visible' });
}

test.describe('DashScreen ledger widget', () => {
  test('loading and error states show dashes, not zero', async ({ page }) => {
    const routes = await installLedgerRoutes(page, { entriesFail: true, budgetFail: true });
    await gotoDash(page);
    await expect(page.getByTestId('dash-ledger-spent')).toContainText('—');
    await expect(page.getByTestId('dash-ledger-budget')).toContainText('—');
    await expect(page.getByTestId('dash-ledger-hint')).toContainText('账本暂不可用');
    await expect(page.getByText('¥0')).toHaveCount(0);
    expect(routes.counts.entriesGet).toBeGreaterThan(0);
    expect(routes.counts.budgetGet).toBeGreaterThan(0);
  });

  test('success with unset budget shows spent and 未设置', async ({ page }) => {
    const routes = await installLedgerRoutes(page, { budgetAmount: null });
    await gotoDash(page);
    await expect(page.getByTestId('dash-ledger-spent')).toContainText('68');
    await expect(page.getByTestId('dash-ledger-budget')).toContainText('未设置');
    await expect(page.getByTestId('dash-ledger-hint')).toContainText('预算未设置');
    await expect(page.getByTestId('dash-ledger-hint')).not.toContainText('账本暂不可用');
    expect(routes.counts.entriesGet).toBeGreaterThan(0);
    expect(routes.counts.budgetGet).toBeGreaterThan(0);
  });
});
