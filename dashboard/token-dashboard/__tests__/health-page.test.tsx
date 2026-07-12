/**
 * Tests for app/operator/health/page.tsx — surfaces /api/operator/system-health.
 *
 * - Renders overall status + score + per-component status badges
 * - Loading + error states render
 * - Unknown status strings never throw (fall back to muted)
 * - Subsystem effectiveness cards (PR-18) render independently via useHealthBeacons()
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({
  useSystemHealth: jest.fn(),
  useHealthBeacons: jest.fn(),
}));

import { useSystemHealth, useHealthBeacons } from '@/lib/hooks';
import SystemHealthPage from '@/app/operator/health/page';
import type { SystemHealthEnvelope, HealthBeaconEnvelope } from '@/lib/types';

const mockUseSystemHealth = useSystemHealth as jest.MockedFunction<typeof useSystemHealth>;
const mockUseHealthBeacons = useHealthBeacons as jest.MockedFunction<typeof useHealthBeacons>;

function envelope(overrides: Partial<SystemHealthEnvelope> = {}): SystemHealthEnvelope {
  return {
    status: 'healthy',
    queried_at: '2026-06-27T15:00:00Z',
    health_score: 0.9,
    components: {
      intelligence_db: { status: 'healthy', details: { rows: 240 } },
      governance_digest: { status: 'degraded', details: { reason: 'stale' } },
    },
    ...overrides,
  };
}

function beaconEnvelope(overrides: Partial<HealthBeaconEnvelope> = {}): HealthBeaconEnvelope {
  return {
    queried_at: '2026-06-27T15:00:00Z',
    data_dir: '/tmp/.vnx-data',
    overall: 'ok',
    counts: { ok: 1, stale: 0, fail: 0, corrupt: 0 },
    beacons: {},
    subsystems: [
      { subsystem: 'phantom_guard', health: 'ok', status: 'ok', last_signal: '2026-06-27T15:00:00Z', detail: {} },
      { subsystem: 'governance-enforcement-stack', health: 'unknown', status: 'unknown', last_signal: '', detail: {} },
    ],
    ...overrides,
  };
}

function renderWith(
  data: SystemHealthEnvelope | undefined,
  opts: { isLoading?: boolean; error?: Error } = {},
  beaconData: HealthBeaconEnvelope | undefined = beaconEnvelope(),
  beaconOpts: { isLoading?: boolean; error?: Error } = {},
) {
  mockUseSystemHealth.mockReturnValue({
    data,
    isLoading: opts.isLoading ?? false,
    error: opts.error,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useSystemHealth>);
  mockUseHealthBeacons.mockReturnValue({
    data: beaconData,
    isLoading: beaconOpts.isLoading ?? false,
    error: beaconOpts.error,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useHealthBeacons>);
  return render(<SystemHealthPage />);
}

describe('SystemHealthPage', () => {
  test('renders overall status, score, and component badges', () => {
    renderWith(envelope());

    expect(screen.getByTestId('health-overall')).toHaveTextContent('healthy');
    expect(screen.getByTestId('health-score')).toHaveTextContent('90%');
    expect(screen.getByTestId('health-component-intelligence_db')).toBeInTheDocument();
    expect(screen.getByTestId('health-status-governance_digest')).toHaveTextContent('degraded');
  });

  test('loading state', () => {
    renderWith(undefined, { isLoading: true });
    expect(screen.getByTestId('health-loading')).toBeInTheDocument();
  });

  test('error state', () => {
    renderWith(undefined, { error: new Error('boom') });
    expect(screen.getByTestId('health-error')).toBeInTheDocument();
  });

  test('unknown component status string does not throw', () => {
    renderWith(envelope({ components: { weird: { status: 'totally-new-status', details: {} } } }));
    expect(screen.getByTestId('health-status-weird')).toHaveTextContent('totally-new-status');
  });

  test('empty components renders placeholder', () => {
    renderWith(envelope({ components: {} }));
    expect(screen.getByTestId('health-empty')).toBeInTheDocument();
  });

  test('renders subsystem effectiveness cards with health badges', () => {
    renderWith(envelope());
    expect(screen.getByTestId('subsystems-grid')).toBeInTheDocument();
    expect(screen.getByTestId('subsystem-card-phantom_guard')).toBeInTheDocument();
    expect(screen.getByTestId('subsystem-health-phantom_guard')).toHaveTextContent('ok');
    expect(screen.getByTestId('subsystem-health-governance-enforcement-stack')).toHaveTextContent('unknown');
  });

  test('subsystems loading state renders independently of system health data', () => {
    renderWith(envelope(), {}, undefined, { isLoading: true });
    expect(screen.getByTestId('subsystems-loading')).toBeInTheDocument();
  });

  test('subsystems empty state renders placeholder', () => {
    renderWith(envelope(), {}, beaconEnvelope({ subsystems: [] }));
    expect(screen.getByTestId('subsystems-empty')).toBeInTheDocument();
  });
});
