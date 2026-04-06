import { test, expect } from '@playwright/test';

test.describe('Session control', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/operator', { waitUntil: 'networkidle' });
  });

  test('main page loads without errors', async ({ page }) => {
    await expect(page).not.toHaveTitle(/error/i);
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.waitForLoadState('networkidle');
    expect(errors).toHaveLength(0);
  });

  test('session control buttons are visible', async ({ page }) => {
    const anyBtn = page.locator('[data-testid="btn-start"], [data-testid="btn-stop"], [data-testid="btn-attach"]');
    await expect(anyBtn.first()).toBeVisible({ timeout: 10000 });

    const startCount = await page.getByTestId('btn-start').count();
    const stopCount = await page.getByTestId('btn-stop').count();
    const attachCount = await page.getByTestId('btn-attach').count();

    expect(startCount + stopCount + attachCount).toBeGreaterThan(0);
  });

  test('start or stop button exists in DOM', async ({ page }) => {
    const anyBtn = page.locator('[data-testid="btn-start"], [data-testid="btn-stop"]');
    await expect(anyBtn.first()).toBeVisible({ timeout: 10000 });

    const startOrStop = await anyBtn.count();
    expect(startOrStop).toBeGreaterThan(0);
  });
});
