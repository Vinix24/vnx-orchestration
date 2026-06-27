/**
 * Tests for app/operator/health/page.tsx — surfaces /api/operator/system-health.
 *
 * - Renders overall status + score + per-component status badges
 * - Loading + error states render
 * - Unknown status strings never throw (fall back to muted)
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({
  useSystemHealth: jest.fn(),
}));

import { useSystemHealth } from '@/lib/hooks';
import SystemHealthPage from '@/app/operator/health/page';
import type { SystemHealthEnvelope } from '@/lib/types';

const mockUseSystemHealth = useSystemHealth as jest.MockedFunction<typeof useSystemHealth>;

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

function renderWith(data: SystemHealthEnvelope | undefined, opts: { isLoading?: boolean; error?: Error } = {}) {
  mockUseSystemHealth.mockReturnValue({
    data,
    isLoading: opts.isLoading ?? false,
    error: opts.error,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useSystemHealth>);
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
});
