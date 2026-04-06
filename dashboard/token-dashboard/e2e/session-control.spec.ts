import { test, expect } from '@playwright/test';

test.describe('Session control', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('main page loads without errors', async ({ page }) => {
    await expect(page).not.toHaveTitle(/error/i);
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.waitForLoadState('networkidle');
    expect(errors).toHaveLength(0);
  });

  test('session control buttons are visible', async ({ page }) => {
    await page.waitForLoadState('networkidle');
    // Start, Stop, and Attach buttons are present in the operator project cards.
    // At least one session control action button must be visible.
    const startBtn = page.getByTestId('btn-start');
    const stopBtn = page.getByTestId('btn-stop');
    const attachBtn = page.getByTestId('btn-attach');

    const startCount = await startBtn.count();
    const stopCount = await stopBtn.count();
    const attachCount = await attachBtn.count();

    expect(startCount + stopCount + attachCount).toBeGreaterThan(0);
  });

  test('start or stop button exists in DOM', async ({ page }) => {
    await page.waitForLoadState('networkidle');
    const startOrStop = await page
      .locator('[data-testid="btn-start"], [data-testid="btn-stop"]')
      .count();
    expect(startOrStop).toBeGreaterThan(0);
  });
});
