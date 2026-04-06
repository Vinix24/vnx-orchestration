import { test, expect } from '@playwright/test';

const KANBAN_COLUMNS = ['staging', 'pending', 'active', 'review', 'done'];

test.describe('Kanban board page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/operator/kanban');
  });

  test('page loads without errors', async ({ page }) => {
    await expect(page).not.toHaveTitle(/error/i);
    // No unhandled JS errors
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await page.waitForLoadState('networkidle');
    expect(errors).toHaveLength(0);
  });

  test('5 kanban columns are visible', async ({ page }) => {
    await page.waitForLoadState('networkidle');
    for (const col of KANBAN_COLUMNS) {
      const column = page.getByText(new RegExp(col, 'i')).first();
      await expect(column).toBeVisible();
    }
  });
});
