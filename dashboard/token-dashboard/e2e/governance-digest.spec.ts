import { test, expect } from '@playwright/test';

test.describe('Governance digest page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/operator/governance');
  });

  test('page loads without errors', async ({ page }) => {
    await expect(page).not.toHaveTitle(/error/i);
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.waitForLoadState('networkidle');
    expect(errors).toHaveLength(0);
  });

  test('digest content renders or shows no-data message', async ({ page }) => {
    await page.waitForLoadState('networkidle');
    // Either digest content is shown or a "no data" / empty-state message
    const hasContent = await page.locator('h2, h3, [data-testid="digest-content"]').count() > 0;
    const hasNoData = await page.getByText(/no data|no digest|empty|nothing/i).count() > 0;
    expect(hasContent || hasNoData).toBe(true);
  });
});
