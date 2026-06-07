import { test, expect } from '@playwright/test';

// Lanes are discovered from /api/agent-stream/status (no fixed T0-T3 tabs).
// Mock it so the dropdown populates deterministically; T1 is newest → default.
const STATUS_BODY = {
  lanes: [
    { id: 'T1', event_count: 5, last_timestamp: '2026-04-06T12:00:10Z', provider: 'claude' },
    { id: 'T2', event_count: 2, last_timestamp: '2026-04-06T11:00:00Z', provider: 'claude' },
    { id: 'T3', event_count: 1, last_timestamp: '2026-04-06T10:00:00Z', provider: 'kimi' },
  ],
  terminals: {
    T1: { event_count: 5, last_timestamp: '2026-04-06T12:00:10Z' },
    T2: { event_count: 2, last_timestamp: '2026-04-06T11:00:00Z' },
    T3: { event_count: 1, last_timestamp: '2026-04-06T10:00:00Z' },
  },
};

test.describe('Agent Stream Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/agent-stream/status', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(STATUS_BODY) });
    });
  });

  test('page loads with correct heading', async ({ page }) => {
    await page.goto('/agent-stream');
    const heading = page.locator('h2');
    await expect(heading).toHaveText('Agent Stream');
  });

  test('shows subtitle text', async ({ page }) => {
    await page.goto('/agent-stream');
    await expect(page.getByText('Real-time event stream from worker lanes')).toBeVisible();
  });

  test('lane selector lists discovered lanes as options', async ({ page }) => {
    await page.goto('/agent-stream');
    const select = page.locator('[data-testid="agent-selector"] select');
    await expect(select).toBeVisible();
    for (const t of ['T1', 'T2', 'T3']) {
      await expect(select.locator('option', { hasText: t })).toHaveCount(1);
    }
  });

  test('lane selector switches active lane', async ({ page }) => {
    await page.goto('/agent-stream');
    const select = page.locator('[data-testid="agent-selector"] select');
    // T1 is the default (newest); switch to T2
    await select.selectOption('T2');
    await expect(select).toHaveValue('T2');
  });

  test('shows empty state when no events', async ({ page }) => {
    await page.goto('/agent-stream');
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
    const status = page.getByText(/Connected|Disconnected/);
    await expect(status).toBeVisible();
  });

  test('event type badges render with correct colors', async ({ page }) => {
    // T1 is the default lane (newest in STATUS_BODY) → SSE connects to it.
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
