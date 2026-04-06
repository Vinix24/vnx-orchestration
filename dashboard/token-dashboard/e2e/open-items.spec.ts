import { test, expect } from '@playwright/test';

test.describe('Open Items page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/operator/open-items');
  });

  test('page loads without errors', async ({ page }) => {
    await expect(page).not.toHaveTitle(/error/i);
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.waitForLoadState('networkidle');
    expect(errors).toHaveLength(0);
  });

  test('severity filter chips are visible', async ({ page }) => {
    await page.waitForLoadState('networkidle');
    // Expect severity filter chips: blocker, warn, info
    const blockerChip = page.getByText(/blocker/i).first();
    await expect(blockerChip).toBeVisible();

    const warnChip = page.getByText(/warn/i).first();
    await expect(warnChip).toBeVisible();

    const infoChip = page.getByText(/info/i).first();
    await expect(infoChip).toBeVisible();
  });
});
