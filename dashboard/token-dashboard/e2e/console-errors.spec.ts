import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Centralized error listener
// ---------------------------------------------------------------------------

interface ErrorCollector {
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
}

/**
 * Attaches console/pageerror/requestfailed listeners before navigation.
 * Returns a live collector — assert its contents after page interactions.
 */
function attachErrorListeners(page: Page): ErrorCollector {
  const collected: ErrorCollector = {
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
  };

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();

    // FILTER: Next.js HMR / Fast Refresh — dev-server infrastructure reconnect noise (NOT a runtime error)
    if (/\[HMR\]|\[Fast Refresh\]|webpack-internal:\/\//.test(text)) return;
    // FILTER: Browser extension scripts injected into the page — outside application control
    if (/chrome-extension:\/\/|moz-extension:\/\//.test(text)) return;
    // FILTER: Next.js SSR→client hydration mismatch in dev mode — expected when server/client rendering diverges
    // NOTE: validateDOMNesting is intentionally NOT filtered — it signals invalid HTML structure (real bug)
    if (/Hydration|hydration|did not match/.test(text)) return;
    // FILTER: SWR fetch-failure messages for the external stats API (/api/token-stats proxy)
    // The app renders a graceful "Failed to load data" banner; these errors are expected in CI
    if (/Failed to fetch token stats|Failed to fetch sessions|Failed to fetch conversations/.test(text)) return;
    // FILTER: ResizeObserver loop — charting library artefact in headless mode when container dimensions are 0
    if (/ResizeObserver loop limit exceeded|ResizeObserver was delivered/.test(text)) return;
    // FILTER: Next.js styled-jsx prop warning in dev mode — internal implementation detail, not a bug
    // NOTE: "Warning: Each child in a list" (missing React key prop) is intentionally NOT filtered — it signals a real bug
    if (/styled-jsx/.test(text)) return;

    collected.consoleErrors.push(`[console.error] ${text}`);
  });

  page.on('pageerror', (err) => {
    // Uncaught JS exceptions — always a real problem
    collected.pageErrors.push(`[pageerror] ${err.message}`);
  });

  page.on('requestfailed', (request) => {
    const url = request.url();
    const errorText = request.failure()?.errorText ?? 'unknown';

    // Only flag network-level failures on this app's own routes (port 3100).
    // External services (external CDNs, fonts, other local ports) are out of scope.
    if (!/localhost:3100|127\.0\.0\.1:3100/.test(url)) return;
    // favicon.ico — browser always requests this; missing icon is benign
    if (/favicon\.ico/.test(url)) return;
    // SSE connections abort naturally on component unmount — not an application error
    if (errorText === 'net::ERR_ABORTED') return;

    collected.failedRequests.push(`[requestfailed] ${request.method()} ${url} — ${errorText}`);
  });

  return collected;
}

/**
 * Asserts all collected error buckets are empty.
 * Produces a readable failure message listing every violation.
 */
function assertNoErrors(collected: ErrorCollector): void {
  const all = [
    ...collected.consoleErrors,
    ...collected.pageErrors,
    ...collected.failedRequests,
  ];
  expect(all, `Unexpected browser errors:\n${all.join('\n')}`).toHaveLength(0);
}

/**
 * Verifies no Next.js runtime error overlay is visible.
 * This overlay appears when an error boundary catches an unhandled exception.
 */
async function assertNoRuntimeErrorOverlay(page: Page): Promise<void> {
  const overlay = page.locator('[data-nextjs-dialog-overlay]');
  await expect(overlay).toHaveCount(0);

  const crashBanner = page.getByText(/Application error: a client-side exception has occurred/i);
  await expect(crashBanner).toHaveCount(0);
}

// ---------------------------------------------------------------------------
// Shared SSE mock — prevents persistent SSE connections from blocking networkidle
// ---------------------------------------------------------------------------

// The agent-stream page opens an EventSource to /api/agent-stream/[terminal].
// Without this mock, that connection keeps the page in a non-idle state forever.
function mockSseEndpoints(page: Page): ReturnType<typeof page.route> {
  return page.route('**/api/agent-stream/**', async (route) => {
    await route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
      body: '',
    });
  });
}

// ---------------------------------------------------------------------------
// Console error tests — one test per dashboard route
// ---------------------------------------------------------------------------

test.describe('Console errors — all dashboard routes', () => {
  test.beforeEach(async ({ page }) => {
    await mockSseEndpoints(page);
  });

  test('/ (Overview) — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/agent-stream — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/agent-stream');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/conversations — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/conversations');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/models — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/models');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator/kanban — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator/kanban');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator/governance — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator/governance');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator/open-items — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator/open-items');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator/dispatches — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator/dispatches');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator/improvements — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator/improvements');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator/intelligence — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator/intelligence');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/operator/reports — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/operator/reports');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/terminals — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/terminals');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/tokens — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/tokens');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });

  test('/usage — no console errors or uncaught exceptions', async ({ page }) => {
    const errors = attachErrorListeners(page);
    await page.goto('/usage');
    await page.waitForLoadState('networkidle');
    assertNoErrors(errors);
    await assertNoRuntimeErrorOverlay(page);
  });
});

// ---------------------------------------------------------------------------
// Network failure detection — 5xx errors on in-app API routes (port 3100)
// ---------------------------------------------------------------------------

// These tests assert that the Next.js API routes themselves do not return 5xx.
// 4xx responses are intentionally excluded: missing data is expected in CI.
// This layer catches mis-wired route handlers and import errors in API routes.

const apiNetworkRoutes: Array<{ path: string; label: string }> = [
  // Core dashboard routes — these call /api/token-stats, /api/token-stats/sessions,
  // and /api/conversations whose fetch errors are suppressed in the console-error
  // listener above (graceful "Failed to load" banners). The 5xx check here ensures
  // the routes themselves don't regress to server-side failures silently.
  { path: '/', label: 'Overview' },
  { path: '/models', label: 'Models' },
  { path: '/terminals', label: 'Terminals' },
  { path: '/tokens', label: 'Tokens' },
  { path: '/usage', label: 'Usage' },
  { path: '/conversations', label: 'Conversations' },
  // Operator pages
  { path: '/operator/kanban', label: 'Kanban' },
  { path: '/operator/open-items', label: 'Open Items' },
  { path: '/operator', label: 'Session control' },
  { path: '/operator/dispatches', label: 'Dispatches' },
  { path: '/operator/governance', label: 'Governance digest' },
  { path: '/operator/reports', label: 'Reports' },
];

test.describe('Network failures — in-app API routes (port 3100)', () => {
  test.beforeEach(async ({ page }) => {
    await mockSseEndpoints(page);
  });

  for (const { path, label } of apiNetworkRoutes) {
    test(`${path} (${label}) — no 5xx on in-app API routes`, async ({ page }) => {
      const serverErrors: string[] = [];

      page.on('response', (response) => {
        const url = response.url();
        const status = response.status();
        if (!/localhost:3100|127\.0\.0\.1:3100/.test(url)) return;
        if (!url.includes('/api/')) return;
        if (status >= 500) {
          serverErrors.push(`${status} ${url}`);
        }
      });

      await page.goto(path);
      await page.waitForLoadState('networkidle');

      expect(
        serverErrors,
        `Server errors on API routes:\n${serverErrors.join('\n')}`
      ).toHaveLength(0);
    });
  }
});
