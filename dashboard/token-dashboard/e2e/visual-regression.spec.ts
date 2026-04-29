import { test, expect, Page } from '@playwright/test';

// Disable all CSS animations/transitions to eliminate motion-driven flakiness.
async function freezeAnimations(page: Page): Promise<void> {
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation-duration: 0s !important;
        animation-delay: 0s !important;
        transition-duration: 0s !important;
        transition-delay: 0s !important;
      }
    `,
  });
}

// Wait for fonts. We use domcontentloaded rather than networkidle because SSE
// routes keep an open network connection that prevents networkidle from firing.
async function waitForFonts(page: Page): Promise<void> {
  await page.waitForLoadState('domcontentloaded');
  await page.evaluate(() => document.fonts.ready);
}

const SNAPSHOT_OPTS = {
  maxDiffPixelRatio: 0.01,
  fullPage: true,
  // Give Playwright enough time to capture two consecutive stable frames.
  timeout: 15000,
  animations: 'disabled' as const,
};

// ---------------------------------------------------------------------------
// Happy-path route snapshots
// ---------------------------------------------------------------------------

test.describe('Visual regression — happy path', () => {
  test('/ (root redirect / operator overview)', async ({ page }) => {
    await page.goto('/');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('root.png', SNAPSHOT_OPTS);
  });

  test('/operator (session control)', async ({ page }) => {
    await page.goto('/operator');
    await freezeAnimations(page);
    await waitForFonts(page);
    // Wait for a session-control button so the component tree is rendered.
    await page.locator('[data-testid="btn-start"], [data-testid="btn-stop"], [data-testid="btn-attach"], h1, h2').first().waitFor({ timeout: 10000 });
    await expect(page).toHaveScreenshot('operator.png', SNAPSHOT_OPTS);
  });

  test('/operator/kanban (kanban board)', async ({ page }) => {
    await page.goto('/operator/kanban');
    await freezeAnimations(page);
    await waitForFonts(page);
    // Wait for at least one kanban column header before snapshotting.
    await page.getByText(/staging|pending|active|review|done/i).first().waitFor({ timeout: 10000 });
    await expect(page).toHaveScreenshot('kanban.png', SNAPSHOT_OPTS);
  });

  test('/operator/governance (governance digest)', async ({ page }) => {
    await page.goto('/operator/governance');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('governance.png', SNAPSHOT_OPTS);
  });

  test('/operator/open-items (open items)', async ({ page }) => {
    await page.goto('/operator/open-items');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('open-items.png', SNAPSHOT_OPTS);
  });

  test('/agent-stream (agent stream)', async ({ page }) => {
    await page.goto('/agent-stream');
    await freezeAnimations(page);
    await waitForFonts(page);
    // SSE keeps network open; wait for the page heading instead of networkidle.
    await page.locator('h1, h2').first().waitFor({ timeout: 10000 });
    await expect(page).toHaveScreenshot('agent-stream.png', SNAPSHOT_OPTS);
  });
});

// ---------------------------------------------------------------------------
// Error state snapshots — mock API 500 responses
// ---------------------------------------------------------------------------

test.describe('Visual regression — error states', () => {
  test('kanban 500 error UI', async ({ page }) => {
    await page.route('**/api/operator/kanban', async (route) => {
      await route.fulfill({ status: 500, body: JSON.stringify({ error: 'Internal Server Error' }) });
    });
    await page.goto('/operator/kanban');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('kanban-error.png', SNAPSHOT_OPTS);
  });

  test('governance 500 error UI', async ({ page }) => {
    await page.route('**/api/operator/governance**', async (route) => {
      await route.fulfill({ status: 500, body: JSON.stringify({ error: 'Internal Server Error' }) });
    });
    await page.goto('/operator/governance');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('governance-error.png', SNAPSHOT_OPTS);
  });

  test('open-items 500 error UI', async ({ page }) => {
    await page.route('**/api/operator/open-items**', async (route) => {
      await route.fulfill({ status: 500, body: JSON.stringify({ error: 'Internal Server Error' }) });
    });
    await page.goto('/operator/open-items');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('open-items-error.png', SNAPSHOT_OPTS);
  });

  test('agent-stream SSE 500 error UI', async ({ page }) => {
    await page.route('**/api/agent-stream/**', async (route) => {
      await route.fulfill({ status: 500, body: JSON.stringify({ error: 'Stream unavailable' }) });
    });
    await page.goto('/agent-stream');
    await freezeAnimations(page);
    await waitForFonts(page);
    await page.locator('h1, h2').first().waitFor({ timeout: 10000 });
    await expect(page).toHaveScreenshot('agent-stream-error.png', SNAPSHOT_OPTS);
  });
});

// ---------------------------------------------------------------------------
// Empty state snapshots — mock empty/null API responses
// ---------------------------------------------------------------------------

test.describe('Visual regression — empty states', () => {
  test('kanban empty state UI', async ({ page }) => {
    await page.route('**/api/operator/kanban', async (route) => {
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stages: {}, total: 0 }),
      });
    });
    await page.goto('/operator/kanban');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('kanban-empty.png', SNAPSHOT_OPTS);
  });

  test('open-items empty state UI', async ({ page }) => {
    await page.route('**/api/operator/open-items**', async (route) => {
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify([]),
      });
    });
    await page.goto('/operator/open-items');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('open-items-empty.png', SNAPSHOT_OPTS);
  });

  test('governance empty state UI', async ({ page }) => {
    await page.route('**/api/operator/governance**', async (route) => {
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: [], summary: null }),
      });
    });
    await page.goto('/operator/governance');
    await freezeAnimations(page);
    await waitForFonts(page);
    await expect(page).toHaveScreenshot('governance-empty.png', SNAPSHOT_OPTS);
  });

  test('agent-stream empty events state UI', async ({ page }) => {
    // Respond with valid SSE headers but no event data — triggers the "no events" empty state.
    await page.route('**/api/agent-stream/**', async (route) => {
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: '',
      });
    });
    await page.goto('/agent-stream');
    await freezeAnimations(page);
    await waitForFonts(page);
    await page.locator('h1, h2').first().waitFor({ timeout: 10000 });
    await expect(page).toHaveScreenshot('agent-stream-empty.png', SNAPSHOT_OPTS);
  });
});
