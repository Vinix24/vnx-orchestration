/**
 * Tests for app/operator/planning/page.tsx — surfaces /api/operator/planning.
 *
 * - Renders Now/Next/Later horizons with track cards (phase, deliverables, OIs, deps)
 * - Drift indicator + degraded banner
 * - Loading + error states; empty horizons render a placeholder
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({
  usePlanning: jest.fn(),
}));

import { usePlanning } from '@/lib/hooks';
import PlanningPage from '@/app/operator/planning/page';
import type { PlanningEnvelope, PlanningCard } from '@/lib/types';

const mockUsePlanning = usePlanning as jest.MockedFunction<typeof usePlanning>;

const TRACK_A: PlanningCard = {
  track_id: 'track-1',
  title: 'Config control-plane',
  phase: 'active',
  horizon: 'now',
  priority: 'P1',
  next_up: true,
  pr_ref: 'PR-100',
  dispatch_count: 2,
  depends_on: [{ to_track_id: 'track-0', to_project_id: null, kind: 'blocks', confidence: 1 }],
  deliverables: [{ deliverable_ref: 'del-1', output_kind: 'pr', derived_status: 'in_progress', dispatch_count: 1 }],
  open_items: [{ oi_id: 'oi-1', link_type: 'blocks', title: 'tenant guard missing', severity: 'blocker', status: 'open' }],
};

function envelope(overrides: Partial<PlanningEnvelope> = {}): PlanningEnvelope {
  return {
    queried_at: '2026-06-27T15:00:00Z',
    project_id: 'vnx-dev',
    horizons: { now: [TRACK_A], next: [], later: [] },
    total_tracks: 1,
    degraded: false,
    ...overrides,
  };
}

function renderWith(data: PlanningEnvelope | undefined, opts: { isLoading?: boolean; error?: Error } = {}) {
  mockUsePlanning.mockReturnValue({
    data,
    isLoading: opts.isLoading ?? false,
    error: opts.error,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof usePlanning>);
  return render(<PlanningPage />);
}

describe('PlanningPage', () => {
  test('renders horizons and a track card with phase', () => {
    renderWith(envelope());
    expect(screen.getByTestId('horizon-now')).toBeInTheDocument();
    expect(screen.getByTestId('horizon-next')).toBeInTheDocument();
    expect(screen.getByTestId('horizon-later')).toBeInTheDocument();
    expect(screen.getByTestId('track-track-1')).toBeInTheDocument();
    expect(screen.getByTestId('track-phase-track-1')).toHaveTextContent('active');
    expect(screen.getByTestId('track-nextup-track-1')).toBeInTheDocument();
  });

  test('renders drift indicator from divergent_count', () => {
    renderWith(envelope({
      drift: { generated_at: '2026-06-27T15:00:00Z', divergent_count: 2, total_tracks: 5, divergent: ['track-1', 'track-2'] },
    }));
    expect(screen.getByTestId('planning-drift')).toHaveTextContent('2 drift');
  });

  test('no drift indicator when divergent_count is 0', () => {
    renderWith(envelope({
      drift: { generated_at: null, divergent_count: 0, total_tracks: 5, divergent: [] },
    }));
    expect(screen.queryByTestId('planning-drift')).toBeNull();
  });

  test('renders degraded banner', () => {
    renderWith(envelope({ degraded: true, degraded_reasons: ['runtime_coordination.db not found'] }));
    expect(screen.getByTestId('planning-degraded')).toHaveTextContent('runtime_coordination.db not found');
  });

  test('empty horizons render placeholders', () => {
    renderWith(envelope({ horizons: { now: [], next: [], later: [] }, total_tracks: 0 }));
    expect(screen.getByTestId('horizon-empty-now')).toBeInTheDocument();
    expect(screen.getByTestId('horizon-empty-later')).toBeInTheDocument();
  });

  test('loading state', () => {
    renderWith(undefined, { isLoading: true });
    expect(screen.getByTestId('planning-loading')).toBeInTheDocument();
  });

  test('error state', () => {
    renderWith(undefined, { error: new Error('boom') });
    expect(screen.getByTestId('planning-error')).toBeInTheDocument();
  });
});
