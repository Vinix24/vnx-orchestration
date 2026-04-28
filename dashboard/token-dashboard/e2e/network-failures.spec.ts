import { test, expect, type Page } from '@playwright/test';

// ---- Centralized network condition helpers ----

async function stubApiError(page: Page, urlPattern: string, status = 500): Promise<void> {
  await page.route(urlPattern, (route) =>
    route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'Simulated server error' }),
    })
  );
}

async function stubApiAbort(page: Page, urlPattern: string): Promise<void> {
  await page.route(urlPattern, (route) => route.abort('failed'));
}

async function stubApiDelay(
  page: Page,
  urlPattern: string,
  delayMs: number,
  responseBody: unknown = {}
): Promise<void> {
  await page.route(urlPattern, async (route) => {
    await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(responseBody),
    });
  });
}

// ---- Kanban board ----

test.describe('Network failures — Kanban board', () => {
  test('5xx on kanban API shows degraded error banner, no white screen', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiError(page, '**/api/operator/kanban');

    await page.goto('/operator/kanban');
    await page.waitForLoadState('networkidle');

    const alert = page.locator('[role="alert"][aria-live="polite"]');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/failed to load kanban|degraded/i);
    await expect(page).not.toHaveTitle(/error/i);
    await expect(page.getByRole('heading', { name: /kanban/i })).toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test('aborted request (offline) on kanban shows error banner', async ({ page }) => {
    await stubApiAbort(page, '**/api/operator/kanban');

    await page.goto('/operator/kanban');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[role="alert"][aria-live="polite"]')).toBeVisible();
    await expect(page).not.toHaveTitle(/error/i);
  });

  test('slow 3G response on kanban: skeleton renders, then content appears', async ({ page }) => {
    const emptyKanban = {
      stages: { staging: [], pending: [], active: [], review: [], done: [] },
      total: 0,
      degraded: false,
    };
    await stubApiDelay(page, '**/api/operator/kanban', 2000, emptyKanban);

    await page.goto('/operator/kanban');
    await page.waitForLoadState('domcontentloaded');

    // During the 2-second delay skeleton cards (aria-hidden) are visible
    await expect(page.locator('[aria-hidden="true"]').first()).toBeVisible({ timeout: 3000 });

    // After the delay the empty-column placeholders replace the skeleton
    await expect(page.locator('[data-testid^="empty-"]').first()).toBeVisible({ timeout: 8000 });
  });

  test('request timeout (delayed abort) on kanban shows error banner', async ({ page }) => {
    await page.route('**/api/operator/kanban', async (route) => {
      await new Promise<void>((resolve) => setTimeout(resolve, 2500));
      await route.abort('timedout');
    });

    await page.goto('/operator/kanban');
    await expect(page.locator('[role="alert"]')).toBeVisible({ timeout: 7000 });
  });

  test('partial failure: kanban widget errors, page structure remains intact', async ({ page }) => {
    await stubApiError(page, '**/api/operator/kanban');

    await page.goto('/operator/kanban');
    await page.waitForLoadState('networkidle');

    // Kanban-specific error banner visible
    await expect(page.locator('[role="alert"][aria-live="polite"]')).toBeVisible();

    // Page heading and kanban grid scaffold still render
    await expect(page.getByRole('heading', { name: /kanban/i })).toBeVisible();
    await expect(page.locator('[data-testid="kanban-grid"]')).toBeVisible();

    // Domain filter (client-side static UI) remains functional
    await expect(page.locator('[data-testid="domain-filter"]')).toBeVisible();
  });

  test('recovery after 5xx: Refresh button clears error when backend recovers', async ({ page }) => {
    let requestCount = 0;
    await page.route('**/api/operator/kanban', async (route) => {
      requestCount += 1;
      if (requestCount === 1) {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'Simulated' }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            stages: { staging: [], pending: [], active: [], review: [], done: [] },
            total: 0,
            degraded: false,
          }),
        });
      }
    });

    await page.goto('/operator/kanban');
    await page.waitForLoadState('networkidle');

    // Error banner appears after the first (failed) fetch
    const alert = page.locator('[role="alert"][aria-live="polite"]');
    await expect(alert).toBeVisible();

    // Clicking Refresh triggers SWR mutate() → second fetch → 200 OK
    await page.click('[aria-label="Refresh kanban board"]');

    // Error banner disappears after successful recovery
    await expect(alert).not.toBeVisible({ timeout: 8000 });
  });
});

// ---- Governance digest ----

test.describe('Network failures — Governance digest', () => {
  test('5xx on governance-digest shows degraded banner, no white screen', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiError(page, '**/api/operator/governance-digest');

    await page.goto('/operator/governance');
    await page.waitForLoadState('networkidle');

    const alert = page.locator('[role="alert"][aria-live="polite"]');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/failed to load governance|degraded/i);
    await expect(page).not.toHaveTitle(/error/i);
    await expect(page.getByRole('heading', { name: /governance/i })).toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test('aborted request on governance-digest shows error banner', async ({ page }) => {
    await stubApiAbort(page, '**/api/operator/governance-digest');

    await page.goto('/operator/governance');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('[role="alert"][aria-live="polite"]')).toBeVisible();
    await expect(page).not.toHaveTitle(/error/i);
  });

  test('slow 3G response on governance-digest: skeleton shows, then empty state loads', async ({ page }) => {
    await stubApiDelay(
      page,
      '**/api/operator/governance-digest',
      2000,
      { data: null, degraded: false }
    );

    await page.goto('/operator/governance');
    await page.waitForLoadState('domcontentloaded');

    // Skeleton rows visible during the delay
    await expect(page.locator('[aria-hidden="true"]').first()).toBeVisible({ timeout: 3000 });

    // After delay: recurrence table renders in empty state
    await expect(page.locator('[data-testid="recurrence-table"]')).toBeVisible({ timeout: 8000 });
  });

  test('recovery after 5xx on governance: Refresh clears error banner', async ({ page }) => {
    let requestCount = 0;
    await page.route('**/api/operator/governance-digest', async (route) => {
      requestCount += 1;
      if (requestCount === 1) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"sim"}' });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ data: null, degraded: false }),
        });
      }
    });

    await page.goto('/operator/governance');
    await page.waitForLoadState('networkidle');

    const alert = page.locator('[role="alert"][aria-live="polite"]');
    await expect(alert).toBeVisible();

    await page.click('[aria-label="Refresh governance digest"]');
    await expect(alert).not.toBeVisible({ timeout: 8000 });
  });
});

// ---- Open items ----

test.describe('Network failures — Open items', () => {
  test('5xx on aggregate open-items: no white screen, page structure intact', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiError(page, '**/api/operator/open-items/aggregate');

    await page.goto('/operator/open-items');
    await page.waitForLoadState('networkidle');

    await expect(page).not.toHaveTitle(/error/i);
    await expect(page.getByRole('heading', { name: /open items/i })).toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test('aborted request on open-items: page does not crash', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiAbort(page, '**/api/operator/open-items/aggregate');

    await page.goto('/operator/open-items');
    await page.waitForLoadState('networkidle');

    await expect(page).not.toHaveTitle(/error/i);
    await expect(page.getByRole('heading', { name: /open items/i })).toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test('slow 3G on open-items: skeleton shows, then layout loads', async ({ page }) => {
    const emptyData = {
      data: {
        items: [],
        total_summary: { blocker_count: 0, warn_count: 0, info_count: 0 },
        per_project_subtotals: {},
      },
      degraded: false,
    };
    await stubApiDelay(page, '**/api/operator/open-items/aggregate', 2000, emptyData);

    await page.goto('/operator/open-items');
    await page.waitForLoadState('domcontentloaded');

    // Skeleton rows visible during the 2-second delay
    await expect(page.locator('[aria-hidden="true"]').first()).toBeVisible({ timeout: 3000 });

    // After delay: page heading and filter row are visible
    await expect(page.getByRole('heading', { name: /open items/i })).toBeVisible({ timeout: 8000 });
  });

  test('partial failure: open-items aggregate fails, projects API intact, page remains usable', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiError(page, '**/api/operator/open-items/aggregate');

    await page.goto('/operator/open-items');
    await page.waitForLoadState('networkidle');

    // Page heading still renders
    await expect(page.getByRole('heading', { name: /open items/i })).toBeVisible();
    // No unhandled JS exceptions
    expect(errors).toHaveLength(0);
    // Title does not indicate crash
    await expect(page).not.toHaveTitle(/error/i);
  });
});

// ---- Agent stream ----

test.describe('Network failures — Agent stream', () => {
  test('5xx on SSE endpoint shows Disconnected status, no white screen', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiError(page, '**/api/agent-stream/T1');

    await page.goto('/agent-stream');
    await page.waitForLoadState('networkidle');

    await expect(page).not.toHaveTitle(/error/i);
    await expect(page.getByRole('heading', { name: /agent stream/i })).toBeVisible();
    await expect(page.getByText('Disconnected')).toBeVisible();
    expect(errors).toHaveLength(0);
  });

  test('aborted SSE connection shows Disconnected status', async ({ page }) => {
    await stubApiAbort(page, '**/api/agent-stream/T1');

    await page.goto('/agent-stream');
    await page.waitForLoadState('networkidle');

    await expect(page.getByText('Disconnected')).toBeVisible();
    await expect(page).not.toHaveTitle(/error/i);
  });

  test('slow 3G on SSE: Disconnected during delay, Disconnected after timeout abort', async ({ page }) => {
    await page.route('**/api/agent-stream/T1', async (route) => {
      await new Promise<void>((resolve) => setTimeout(resolve, 2500));
      await route.abort('timedout');
    });

    await page.goto('/agent-stream');
    // Disconnected is the initial state and also the error state
    await expect(page.getByText('Disconnected')).toBeVisible({ timeout: 6000 });
    await expect(page).not.toHaveTitle(/error/i);
  });

  test('partial failure: SSE fails but agent selector and controls remain functional', async ({ page }) => {
    await stubApiError(page, '**/api/agent-stream/T1');

    await page.goto('/agent-stream');
    await page.waitForLoadState('networkidle');

    // SSE error: Disconnected shown
    await expect(page.getByText('Disconnected')).toBeVisible();

    // UI controls unaffected by SSE failure
    await expect(page.locator('[data-testid="agent-selector"]')).toBeVisible();
    await expect(page.locator('button', { hasText: 'Pause' })).toBeVisible();
  });
});

// ---- Session control ----

test.describe('Network failures — Session control', () => {
  test('5xx on terminals API: session control page does not crash', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiError(page, '**/api/operator/terminals');

    await page.goto('/operator', { waitUntil: 'networkidle' });

    await expect(page).not.toHaveTitle(/error/i);
    expect(errors).toHaveLength(0);
  });

  test('aborted session API: session control page does not crash', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiAbort(page, '**/api/operator/session');

    await page.goto('/operator', { waitUntil: 'networkidle' });

    await expect(page).not.toHaveTitle(/error/i);
    expect(errors).toHaveLength(0);
  });

  test('5xx on all operator APIs: page still renders without crashing', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (err) => errors.push(err.message));
    await stubApiError(page, '**/api/operator/terminals');
    await stubApiError(page, '**/api/operator/session');
    await stubApiError(page, '**/api/operator/projects');

    await page.goto('/operator', { waitUntil: 'networkidle' });

    await expect(page).not.toHaveTitle(/error/i);
    expect(errors).toHaveLength(0);
  });
});
