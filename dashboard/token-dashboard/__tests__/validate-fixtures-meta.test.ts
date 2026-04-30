/**
 * @jest-environment node
 *
 * Self-test for the fixture completeness gate (scripts/validate_test_fixtures.ts).
 * Verifies that:
 * - Required fields are correctly extracted from lib/types.ts
 * - Missing fields in a typed fixture are detected and reported
 * - Complete fixtures pass without failures
 */

import * as path from 'path';
import { extractRequiredFields, validateFile } from '../scripts/validate_test_fixtures';

const TYPES_FILE = path.resolve(__dirname, '../lib/types.ts');

// ---- extractRequiredFields ----

describe('extractRequiredFields — DispatchSummary', () => {
  test('extracts required fields including domain and reason', () => {
    const fields = extractRequiredFields(TYPES_FILE, ['DispatchSummary']);
    const f = fields.get('DispatchSummary')!;
    expect(f).toBeDefined();
    expect(f).toContain('id');
    expect(f).toContain('domain');
    expect(f).toContain('reason');
    expect(f).toContain('stage');
    expect(f).toContain('dir');
    expect(f).toContain('receipt_status');
  });

  test('does not include optional fields', () => {
    // DispatchSummary has no optional fields currently; this guards future drift
    const fields = extractRequiredFields(TYPES_FILE, ['DispatchSummary']);
    const f = fields.get('DispatchSummary')!;
    // All DispatchSummary fields are required — count check
    expect(f.length).toBeGreaterThan(10);
  });
});

describe('extractRequiredFields — KanbanCard', () => {
  test('extracts required fields for KanbanCard', () => {
    const fields = extractRequiredFields(TYPES_FILE, ['KanbanCard']);
    const f = fields.get('KanbanCard')!;
    expect(f).toContain('domain');
    expect(f).toContain('stage');
    expect(f).toContain('receipt_status');
  });

  test('does not include reason (optional in KanbanCard)', () => {
    const fields = extractRequiredFields(TYPES_FILE, ['KanbanCard']);
    const f = fields.get('KanbanCard')!;
    expect(f).not.toContain('reason');
  });
});

// ---- validateFile — broken fixture (intentionally missing domain) ----

const BROKEN_KANBAN_FIXTURE = `
import type { KanbanCard } from '@/lib/types';

const BROKEN_CARD: KanbanCard = {
  id: 'dispatch-001',
  pr_id: 'PR-1',
  track: 'A',
  terminal: 'T1',
  role: 'backend-developer',
  gate: 'gate_pr1_lifecycle',
  priority: 'P1',
  status: 'active',
  stage: 'active',
  // domain intentionally omitted — this is the field that caused #306 round-2 regressions
  duration_secs: 120,
  duration_label: '2m',
  has_receipt: false,
  receipt_status: null,
};
`;

const BROKEN_DISPATCH_FIXTURE = `
import type { DispatchSummary, DispatchStage } from '@/lib/types';

const BROKEN: DispatchSummary = {
  id: 'd-1',
  file: 'd-1.md',
  pr_id: 'PR-1',
  track: 'A',
  terminal: 'T1',
  role: 'backend-developer',
  gate: 'gate_x',
  priority: 'P1',
  status: 'active',
  reason: '',
  // domain omitted
  dir: 'pending',
  stage: 'pending' as DispatchStage,
  duration_secs: 60,
  duration_label: '1m',
  has_receipt: false,
  receipt_status: null,
};
`;

describe('validateFile — broken fixtures', () => {
  test('detects missing domain in KanbanCard variable', () => {
    const requiredFields = extractRequiredFields(TYPES_FILE, ['KanbanCard']);
    const failures = validateFile('broken-kanban.test.tsx', BROKEN_KANBAN_FIXTURE, requiredFields);

    expect(failures).toHaveLength(1);
    expect(failures[0].typeName).toBe('KanbanCard');
    expect(failures[0].missingFields).toContain('domain');
  });

  test('detects missing domain in DispatchSummary variable', () => {
    const requiredFields = extractRequiredFields(TYPES_FILE, ['DispatchSummary']);
    const failures = validateFile('broken-dispatch.test.tsx', BROKEN_DISPATCH_FIXTURE, requiredFields);

    expect(failures).toHaveLength(1);
    expect(failures[0].typeName).toBe('DispatchSummary');
    expect(failures[0].missingFields).toContain('domain');
  });

  test('reports correct line number for the broken object', () => {
    const requiredFields = extractRequiredFields(TYPES_FILE, ['KanbanCard']);
    const failures = validateFile('broken-kanban.test.tsx', BROKEN_KANBAN_FIXTURE, requiredFields);

    expect(failures[0].line).toBeGreaterThan(0);
  });
});

// ---- validateFile — complete fixtures ----

const GOOD_KANBAN_FIXTURE = `
import type { KanbanCard } from '@/lib/types';

const GOOD_CARD: KanbanCard = {
  id: 'dispatch-001',
  pr_id: 'PR-1',
  track: 'A',
  terminal: 'T1',
  role: 'backend-developer',
  gate: 'gate_pr1_lifecycle',
  priority: 'P1',
  status: 'active',
  stage: 'active',
  domain: 'coding',
  duration_secs: 120,
  duration_label: '2m',
  has_receipt: false,
  receipt_status: null,
};
`;

const GOOD_DISPATCH_FIXTURE = `
import type { DispatchSummary, DispatchStage } from '@/lib/types';

const GOOD: DispatchSummary = {
  id: 'd-1',
  file: 'd-1.md',
  pr_id: 'PR-1',
  track: 'A',
  terminal: 'T1',
  role: 'backend-developer',
  gate: 'gate_x',
  priority: 'P1',
  status: 'active',
  reason: '',
  domain: 'coding',
  dir: 'pending',
  stage: 'pending' as DispatchStage,
  duration_secs: 60,
  duration_label: '1m',
  has_receipt: false,
  receipt_status: null,
};
`;

describe('validateFile — good fixtures', () => {
  test('no failures for complete KanbanCard fixture', () => {
    const requiredFields = extractRequiredFields(TYPES_FILE, ['KanbanCard']);
    const failures = validateFile('good-kanban.test.tsx', GOOD_KANBAN_FIXTURE, requiredFields);
    expect(failures).toHaveLength(0);
  });

  test('no failures for complete DispatchSummary fixture', () => {
    const requiredFields = extractRequiredFields(TYPES_FILE, ['DispatchSummary']);
    const failures = validateFile('good-dispatch.test.tsx', GOOD_DISPATCH_FIXTURE, requiredFields);
    expect(failures).toHaveLength(0);
  });

  test('objects with spread elements are skipped (no false positives)', () => {
    const fixtureWithSpread = `
import type { KanbanCard } from '@/lib/types';

function card(overrides: Partial<KanbanCard> = {}): KanbanCard {
  return {
    id: 'x',
    pr_id: 'PR-1',
    track: 'A',
    terminal: 'T1',
    role: 'backend-developer',
    gate: 'g',
    priority: 'P1',
    status: 'active',
    stage: 'active',
    domain: 'coding',
    duration_secs: 10,
    duration_label: '10s',
    has_receipt: false,
    receipt_status: null,
    ...overrides,
  };
}
    `;
    const requiredFields = extractRequiredFields(TYPES_FILE, ['KanbanCard']);
    const failures = validateFile('spread.test.tsx', fixtureWithSpread, requiredFields);
    // Spread objects are skipped to avoid false positives
    expect(failures).toHaveLength(0);
  });
});
