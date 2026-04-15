# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: agent-stream.spec.ts >> Agent Stream Page >> pause/resume button toggles
- Location: e2e/agent-stream.spec.ts:39:7

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: locator('button').filter({ hasText: 'Resume' })
Expected: visible
Timeout: 5000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 5000ms
  - waiting for locator('button').filter({ hasText: 'Resume' })

```

# Page snapshot

```yaml
- generic [ref=e1]:
  - complementary [ref=e2]:
    - generic [ref=e4]:
      - img "VNX" [ref=e5]
      - generic [ref=e6]:
        - heading "Token Dashboard" [level=1] [ref=e7]
        - paragraph [ref=e8]: Session Analytics
    - navigation [ref=e9]:
      - generic [ref=e10]:
        - button "All" [ref=e11] [cursor=pointer]
        - button "Coding" [ref=e12] [cursor=pointer]
        - button "Content" [ref=e13] [cursor=pointer]
        - button "Marketing" [ref=e14] [cursor=pointer]
        - button "Research" [ref=e15] [cursor=pointer]
      - generic [ref=e17]:
        - paragraph [ref=e18]: Operator
        - link "Control Surface" [ref=e19] [cursor=pointer]:
          - /url: /operator
          - img [ref=e20]
          - text: Control Surface
        - link "Open Items" [ref=e26] [cursor=pointer]:
          - /url: /operator/open-items
          - img [ref=e27]
          - text: Open Items
        - link "Kanban Board" [ref=e29] [cursor=pointer]:
          - /url: /operator/kanban
          - img [ref=e30]
          - text: Kanban Board
        - link "Governance" [ref=e31] [cursor=pointer]:
          - /url: /operator/governance
          - img [ref=e32]
          - text: Governance
        - link "Agent Stream" [ref=e34] [cursor=pointer]:
          - /url: /agent-stream
          - img [ref=e36]
          - text: Agent Stream
      - paragraph [ref=e39]: Analytics
      - link "Overview" [ref=e40] [cursor=pointer]:
        - /url: /
        - img [ref=e41]
        - text: Overview
      - link "Conversations" [ref=e46] [cursor=pointer]:
        - /url: /conversations
        - img [ref=e47]
        - text: Conversations
      - link "Token Analysis" [ref=e49] [cursor=pointer]:
        - /url: /tokens
        - img [ref=e50]
        - text: Token Analysis
      - link "Terminals" [ref=e55] [cursor=pointer]:
        - /url: /terminals
        - img [ref=e56]
        - text: Terminals
      - link "Models" [ref=e58] [cursor=pointer]:
        - /url: /models
        - img [ref=e59]
        - text: Models
      - link "Usage & Costs" [ref=e62] [cursor=pointer]:
        - /url: /usage
        - img [ref=e63]
        - text: Usage & Costs
    - generic [ref=e65]: Claude Code Analytics
  - main [ref=e66]:
    - generic [ref=e67]:
      - generic [ref=e68]:
        - generic [ref=e71]:
          - heading "Agent Stream" [level=2] [ref=e72]
          - paragraph [ref=e73]: Real-time event stream from worker terminals
        - generic [ref=e74]:
          - generic [ref=e75]:
            - img [ref=e76]
            - text: Disconnected
          - button "Pause" [active] [ref=e78] [cursor=pointer]:
            - img [ref=e79]
            - text: Pause
          - generic [ref=e82]:
            - button "T1" [ref=e83] [cursor=pointer]:
              - generic [ref=e84]: T1
            - button "T2" [ref=e85] [cursor=pointer]:
              - generic [ref=e86]: T2
            - button "T3" [ref=e87] [cursor=pointer]:
              - generic [ref=e88]: T3
      - generic [ref=e90]:
        - img [ref=e91]
        - generic [ref=e93]: No events for T1
```

# Test source

```ts
  1   | import { test, expect } from '@playwright/test';
  2   | 
  3   | test.describe('Agent Stream Page', () => {
  4   |   test('page loads with correct heading', async ({ page }) => {
  5   |     await page.goto('/agent-stream');
  6   |     const heading = page.locator('h2');
  7   |     await expect(heading).toHaveText('Agent Stream');
  8   |   });
  9   | 
  10  |   test('shows subtitle text', async ({ page }) => {
  11  |     await page.goto('/agent-stream');
  12  |     await expect(page.getByText('Real-time event stream from worker terminals')).toBeVisible();
  13  |   });
  14  | 
  15  |   test('terminal selector buttons T1 T2 T3 exist', async ({ page }) => {
  16  |     await page.goto('/agent-stream');
  17  |     for (const t of ['T1', 'T2', 'T3']) {
  18  |       const btn = page.locator('button', { hasText: t });
  19  |       await expect(btn).toBeVisible();
  20  |     }
  21  |   });
  22  | 
  23  |   test('terminal selector switches active terminal', async ({ page }) => {
  24  |     await page.goto('/agent-stream');
  25  |     // T1 is default — click T2
  26  |     const t2Btn = page.locator('button', { hasText: 'T2' }).first();
  27  |     await t2Btn.click();
  28  |     // T2 button should become active (fontWeight 600)
  29  |     await expect(t2Btn).toHaveCSS('font-weight', '600');
  30  |   });
  31  | 
  32  |   test('shows empty state when no events', async ({ page }) => {
  33  |     await page.goto('/agent-stream');
  34  |     // The empty state shows either "Waiting for events..." or "No events for T1"
  35  |     const emptyMsg = page.getByText(/Waiting for events|No events for/);
  36  |     await expect(emptyMsg).toBeVisible();
  37  |   });
  38  | 
  39  |   test('pause/resume button toggles', async ({ page }) => {
  40  |     await page.goto('/agent-stream');
  41  |     const pauseBtn = page.locator('button', { hasText: 'Pause' });
  42  |     await expect(pauseBtn).toBeVisible();
  43  |     await pauseBtn.click();
  44  |     const resumeBtn = page.locator('button', { hasText: 'Resume' });
> 45  |     await expect(resumeBtn).toBeVisible();
      |                             ^ Error: expect(locator).toBeVisible() failed
  46  |     await resumeBtn.click();
  47  |     await expect(page.locator('button', { hasText: 'Pause' })).toBeVisible();
  48  |   });
  49  | 
  50  |   test('connection status indicator is visible', async ({ page }) => {
  51  |     await page.goto('/agent-stream');
  52  |     // Either "Connected" or "Disconnected" should display
  53  |     const status = page.getByText(/Connected|Disconnected/);
  54  |     await expect(status).toBeVisible();
  55  |   });
  56  | 
  57  |   test('event type badges render with correct colors', async ({ page }) => {
  58  |     // Mock the SSE endpoint to return fixture events
  59  |     await page.route('**/api/agent-stream/T1', async (route) => {
  60  |       const events = [
  61  |         { type: 'init', timestamp: '2026-04-06T12:00:00Z', terminal: 'T1', sequence: 1, dispatch_id: 'test-001', data: { session_id: 'test-123' } },
  62  |         { type: 'thinking', timestamp: '2026-04-06T12:00:01Z', terminal: 'T1', sequence: 2, dispatch_id: 'test-001', data: { thinking: 'Analyzing...' } },
  63  |         { type: 'tool_use', timestamp: '2026-04-06T12:00:02Z', terminal: 'T1', sequence: 3, dispatch_id: 'test-001', data: { name: 'Read', input: { path: 'test.py' } } },
  64  |         { type: 'tool_result', timestamp: '2026-04-06T12:00:03Z', terminal: 'T1', sequence: 4, dispatch_id: 'test-001', data: { output: 'file contents here' } },
  65  |         { type: 'result', timestamp: '2026-04-06T12:00:04Z', terminal: 'T1', sequence: 5, dispatch_id: 'test-001', data: { text: 'Done!' } },
  66  |       ];
  67  |       const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
  68  |       await route.fulfill({
  69  |         status: 200,
  70  |         headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
  71  |         body,
  72  |       });
  73  |     });
  74  | 
  75  |     await page.goto('/agent-stream');
  76  | 
  77  |     // Wait for events to render
  78  |     await expect(page.getByText('INIT')).toBeVisible({ timeout: 5000 });
  79  |     await expect(page.getByText('THINKING')).toBeVisible();
  80  |     await expect(page.getByText('TOOL_USE')).toBeVisible();
  81  |     await expect(page.getByText('TOOL_RESULT')).toBeVisible();
  82  |     await expect(page.getByText('RESULT')).toBeVisible();
  83  |   });
  84  | 
  85  |   test('event content renders correctly', async ({ page }) => {
  86  |     await page.route('**/api/agent-stream/T1', async (route) => {
  87  |       const events = [
  88  |         { type: 'init', timestamp: '2026-04-06T12:00:00Z', terminal: 'T1', sequence: 1, dispatch_id: 'test-001', data: { session_id: 'sess-abc' } },
  89  |         { type: 'tool_use', timestamp: '2026-04-06T12:00:01Z', terminal: 'T1', sequence: 2, dispatch_id: 'test-001', data: { name: 'Read' } },
  90  |       ];
  91  |       const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
  92  |       await route.fulfill({
  93  |         status: 200,
  94  |         headers: { 'Content-Type': 'text/event-stream' },
  95  |         body,
  96  |       });
  97  |     });
  98  | 
  99  |     await page.goto('/agent-stream');
  100 |     await expect(page.getByText('Session started: sess-abc')).toBeVisible({ timeout: 5000 });
  101 |     await expect(page.getByText('Read(...)')).toBeVisible();
  102 |   });
  103 | });
  104 | 
  105 | test.describe('Sidebar Navigation', () => {
  106 |   test('Agent Stream link exists in sidebar', async ({ page }) => {
  107 |     await page.goto('/agent-stream');
  108 |     const link = page.locator('a[href="/agent-stream"]');
  109 |     await expect(link).toBeVisible();
  110 |     await expect(link).toContainText('Agent Stream');
  111 |   });
  112 | });
  113 | 
```