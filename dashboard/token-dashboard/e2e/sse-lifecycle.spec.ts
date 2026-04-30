/**
 * SSE + timer unmount lifecycle tests (CFX-13)
 *
 * Verifies that EventSource connections are properly opened on mount and closed
 * on unmount/navigation. Uses a window spy injected via addInitScript so the
 * same JS context is maintained during SPA navigation.
 *
 * @integration — requires a running dev server (npm run dev)
 */
import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Types (runtime shape tracked by the browser-side spy)
// ---------------------------------------------------------------------------

interface EsEntry {
  url: string;
  closed: boolean;
}

// ---------------------------------------------------------------------------
// Spy injection — must be called before page.goto()
// ---------------------------------------------------------------------------

async function injectEventSourceSpy(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const instances: { url: string; closed: boolean }[] = [];
    (window as unknown as { __esInstances: typeof instances }).__esInstances = instances;

    const OrigES = window.EventSource;

    class SpyEventSource extends OrigES {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        const entry = { url: String(url), closed: false };
        instances.push(entry);
        const origClose = this.close.bind(this);
        this.close = () => {
          entry.closed = true;
          origClose();
        };
      }
    }

    window.EventSource = SpyEventSource as typeof EventSource;
  });
}

async function getEsInstances(page: Page): Promise<EsEntry[]> {
  return page.evaluate(
    () =>
      (window as Window & { __esInstances?: { url: string; closed: boolean }[] })
        .__esInstances ?? []
  );
}

// ---------------------------------------------------------------------------
// SSE stub — stable empty stream (no onerror, no data)
// ---------------------------------------------------------------------------

async function stubSseEndpoints(page: Page): Promise<void> {
  await page.route('**/api/agent-stream/**', async (route) => {
    if (route.request().url().includes('/status')) {
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ terminals: {} }),
      });
      return;
    }
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
// Tests
// ---------------------------------------------------------------------------

test.describe('SSE + timer unmount lifecycle', () => {
  test('A: EventSource opens on mount', async ({ page }) => {
    await injectEventSourceSpy(page);
    await stubSseEndpoints(page);

    await page.goto('/agent-stream');
    await page.waitForLoadState('domcontentloaded');

    const instances = await getEsInstances(page);

    expect(instances.length, 'At least one EventSource must open on mount').toBeGreaterThan(0);
    expect(
      instances.some((es) => !es.closed),
      'At least one EventSource should be open after mount'
    ).toBe(true);
  });

  test('B: timer cleared on SPA navigation away (no zombie polling)', async ({ page }) => {
    await injectEventSourceSpy(page);
    await stubSseEndpoints(page);

    // Track fetch calls to /api/agent-stream/status to verify polling stops
    const statusRequests: string[] = [];
    page.on('request', (req) => {
      if (req.url().includes('/agent-stream/status')) {
        statusRequests.push(req.url());
      }
    });

    await page.goto('/agent-stream');
    await page.waitForLoadState('domcontentloaded');

    const countBefore = statusRequests.length;

    // SPA navigation away — the setInterval cleanup runs
    await page.click('a[href="/"]');
    await page.waitForLoadState('domcontentloaded');

    // Wait 6 seconds (> 5s polling interval) — no new status requests should appear
    await page.waitForTimeout(6000);
    const countAfter = statusRequests.length;

    expect(
      countAfter,
      `Status polling continued after unmount: ${countAfter - countBefore} new requests after navigation`
    ).toBe(countBefore);
  });

  test('C: no zombie EventSources after terminal switch and navigation', async ({ page }) => {
    await injectEventSourceSpy(page);
    await stubSseEndpoints(page);

    await page.goto('/agent-stream');
    await page.waitForLoadState('domcontentloaded');

    // Switch terminal: T1 → T2 → T3
    const t2Btn = page.locator('[data-testid="agent-selector"] button', { hasText: 'T2' }).first();
    await t2Btn.click();
    await page.waitForTimeout(200);

    const t3Btn = page.locator('[data-testid="agent-selector"] button', { hasText: 'T3' }).first();
    await t3Btn.click();
    await page.waitForTimeout(200);

    // After switching terminals, only one connection should be open
    const midInstances = await getEsInstances(page);
    const openMid = midInstances.filter((es) => !es.closed);
    expect(
      openMid.length,
      `Expected exactly 1 open EventSource after terminal switch, got ${openMid.length}: ${JSON.stringify(openMid)}`
    ).toBe(1);

    // Navigate away via SPA — all connections must close
    await page.click('a[href="/"]');
    await page.waitForLoadState('domcontentloaded');

    const afterNav = await getEsInstances(page);
    const streamInstances = afterNav.filter((es) => es.url.includes('agent-stream'));
    const stillOpen = streamInstances.filter((es) => !es.closed);

    expect(
      stillOpen.length,
      `Zombie EventSources found after navigation: ${JSON.stringify(stillOpen)}`
    ).toBe(0);
  });

  test('D: re-mount creates a fresh EventSource, not a stale reuse', async ({ page }) => {
    await injectEventSourceSpy(page);
    await stubSseEndpoints(page);

    // First mount
    await page.goto('/agent-stream');
    await page.waitForLoadState('domcontentloaded');
    const afterFirstMount = await getEsInstances(page);
    expect(afterFirstMount.length, 'Expected at least one EventSource on first mount').toBeGreaterThan(0);

    // Navigate away (SPA — spy persists)
    await page.click('a[href="/"]');
    await page.waitForLoadState('domcontentloaded');

    const afterNavAway = await getEsInstances(page);
    const closedAfterNav = afterNavAway.filter((es) => es.url.includes('agent-stream') && es.closed);
    expect(closedAfterNav.length, 'Previous connection must be closed after unmount').toBeGreaterThan(0);

    // Navigate back (SPA)
    await page.click('a[href="/agent-stream"]');
    await page.waitForLoadState('domcontentloaded');

    const afterRemount = await getEsInstances(page);

    // New connection must have been created (total count grew)
    expect(
      afterRemount.length,
      `Expected new EventSource on re-mount. Was ${afterFirstMount.length}, now ${afterRemount.length}`
    ).toBeGreaterThan(afterFirstMount.length);

    // The latest instance must be open (not stale/closed)
    const latest = afterRemount[afterRemount.length - 1];
    expect(latest.closed, 'Freshly created EventSource after re-mount must be open').toBe(false);
  });
});
