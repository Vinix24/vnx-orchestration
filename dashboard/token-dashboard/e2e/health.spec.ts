import { test, expect } from '@playwright/test';

test.describe('API health endpoints', () => {
  test('GET /api/health returns 200 with valid JSON', async ({ request }) => {
    const response = await request.get('/api/health');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toBeDefined();
    expect(typeof body).toBe('object');
  });

  test('GET /api/operator/kanban returns valid JSON with stages', async ({ request }) => {
    const response = await request.get('/api/operator/kanban');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body).toBeDefined();
    expect(typeof body).toBe('object');
    expect(body).toHaveProperty('stages');
    expect(typeof body.total).toBe('number');
  });
});
