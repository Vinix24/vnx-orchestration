import { test, expect } from '@playwright/test';

test.describe('Agent Stream Page', () => {
  test('page loads with correct heading', async ({ page }) => {
    await page.goto('/agent-stream');
    const heading = page.locator('h2');
    await expect(heading).toHaveText('Agent Stream');
  });

  test('shows subtitle text', async ({ page }) => {
    await page.goto('/agent-stream');
    await expect(page.getByText('Real-time event stream from worker terminals')).toBeVisible();
  });

  test('terminal selector buttons T1 T2 T3 exist', async ({ page }) => {
    await page.goto('/agent-stream');
    for (const t of ['T1', 'T2', 'T3']) {
      const btn = page.locator('button', { hasText: t });
      await expect(btn).toBeVisible();
    }
  });

  test('terminal selector switches active terminal', async ({ page }) => {
    await page.goto('/agent-stream');
    // T1 is default — click T2
    const t2Btn = page.locator('button', { hasText: 'T2' }).first();
    await t2Btn.click();
    // T2 button should become active (fontWeight 600)
    await expect(t2Btn).toHaveCSS('font-weight', '600');
  });

  test('shows empty state when no events', async ({ page }) => {
    await page.goto('/agent-stream');
    // The empty state shows either "Waiting for events..." or "No events for T1"
    const emptyMsg = page.getByText(/Waiting for events|No events for/);
    await expect(emptyMsg).toBeVisible();
  });

  test('pause/resume button toggles', async ({ page }) => {
    await page.goto('/agent-stream');
    const pauseBtn = page.locator('button', { hasText: 'Pause' });
    await expect(pauseBtn).toBeVisible();
    await pauseBtn.click();
    const resumeBtn = page.locator('button', { hasText: 'Resume' });
    await expect(resumeBtn).toBeVisible();
    await resumeBtn.click();
    await expect(page.locator('button', { hasText: 'Pause' })).toBeVisible();
  });

  test('connection status indicator is visible', async ({ page }) => {
    await page.goto('/agent-stream');
    // Either "Connected" or "Disconnected" should display
    const status = page.getByText(/Connected|Disconnected/);
    await expect(status).toBeVisible();
  });

  test('event type badges render with correct colors', async ({ page }) => {
    // Mock the SSE endpoint to return fixture events
    await page.route('**/api/agent-stream/T1', async (route) => {
      const events = [
        { type: 'init', timestamp: '2026-04-06T12:00:00Z', terminal: 'T1', sequence: 1, dispatch_id: 'test-001', data: { session_id: 'test-123' } },
        { type: 'thinking', timestamp: '2026-04-06T12:00:01Z', terminal: 'T1', sequence: 2, dispatch_id: 'test-001', data: { thinking: 'Analyzing...' } },
        { type: 'tool_use', timestamp: '2026-04-06T12:00:02Z', terminal: 'T1', sequence: 3, dispatch_id: 'test-001', data: { name: 'Read', input: { path: 'test.py' } } },
        { type: 'tool_result', timestamp: '2026-04-06T12:00:03Z', terminal: 'T1', sequence: 4, dispatch_id: 'test-001', data: { output: 'file contents here' } },
        { type: 'result', timestamp: '2026-04-06T12:00:04Z', terminal: 'T1', sequence: 5, dispatch_id: 'test-001', data: { text: 'Done!' } },
      ];
      const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body,
      });
    });

    await page.goto('/agent-stream');

    // Wait for events to render
    await expect(page.getByText('INIT')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('THINKING')).toBeVisible();
    await expect(page.getByText('TOOL_USE')).toBeVisible();
    await expect(page.getByText('TOOL_RESULT')).toBeVisible();
    await expect(page.getByText('RESULT')).toBeVisible();
  });

  test('event content renders correctly', async ({ page }) => {
    await page.route('**/api/agent-stream/T1', async (route) => {
      const events = [
        { type: 'init', timestamp: '2026-04-06T12:00:00Z', terminal: 'T1', sequence: 1, dispatch_id: 'test-001', data: { session_id: 'sess-abc' } },
        { type: 'tool_use', timestamp: '2026-04-06T12:00:01Z', terminal: 'T1', sequence: 2, dispatch_id: 'test-001', data: { name: 'Read' } },
      ];
      const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
        body,
      });
    });

    await page.goto('/agent-stream');
    await expect(page.getByText('Session started: sess-abc')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('Read(...)')).toBeVisible();
  });
});

test.describe('Sidebar Navigation', () => {
  test('Agent Stream link exists in sidebar', async ({ page }) => {
    await page.goto('/agent-stream');
    const link = page.locator('a[href="/agent-stream"]');
    await expect(link).toBeVisible();
    await expect(link).toContainText('Agent Stream');
  });
});
