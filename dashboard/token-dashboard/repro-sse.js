const { chromium } = require('@playwright/test');
(async() => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({ baseURL: 'http://localhost:3100' });
  await page.addInitScript(() => {
    const instances = [];
    window.__esInstances = instances;
    const OrigES = window.EventSource;
    class SpyEventSource extends OrigES {
      constructor(url, init) {
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
    window.EventSource = SpyEventSource;
  });
  await page.route('**/api/agent-stream/**', async (route) => {
    if (route.request().url().includes('/status')) {
      await route.fulfill({ status: 200, headers: { 'Content-Type':'application/json' }, body: JSON.stringify({ terminals: {} }) });
      return;
    }
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type':'text/event-stream', 'Cache-Control':'no-cache', Connection:'keep-alive' },
      body: ''
    });
  });
  const get = () => page.evaluate(() => window.__esInstances ?? []);
  await page.goto('/agent-stream');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(300);
  console.log('after first mount', await get());
  await page.click('a[href="/"]');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(300);
  console.log('after nav away', await get());
  await page.click('a[href="/agent-stream"]');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(300);
  console.log('after remount', await get());
  await browser.close();
})();
