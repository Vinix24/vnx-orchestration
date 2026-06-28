/**
 * Tests for app/operator/observability/page.tsx — the governance/audit-trail panel over
 * /api/operator/observability (self-learning, tagging, provenance, runtime).
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({ useObservability: jest.fn() }));

import { useObservability } from '@/lib/hooks';
import ObservabilityPage from '@/app/operator/observability/page';
import type { ObservabilityEnvelope } from '@/lib/types';

const mockUse = useObservability as jest.MockedFunction<typeof useObservability>;

function envelope(over: Partial<ObservabilityEnvelope> = {}): ObservabilityEnvelope {
  return {
    project_id: 'vnx-dev',
    queried_at: '2026-06-28T10:00:00Z',
    self_learning: {
      events: [{ dispatch_id: 'D-1', outcome: 'success', confidence_change: 0.05, patterns_boosted: 2, patterns_decayed: 0, occurred_at: '2026-06-28T09:00:00Z' }],
      proposals: 3,
    },
    tagging: {
      events: [{ table_name: 'success_patterns', pattern_id: 7, pattern_title: 'fix auth bug', tags: ['security', 'testing'], provider: 'deepseek', tagged_at: '2026-06-28T09:30:00Z' }],
    },
    provenance: {
      by_status: { complete: 4, incomplete: 2 },
      recent: [{ dispatch_id: 'D-2', receipt_id: 'r2', commit_sha: 'abc12345def', pr_number: 99, chain_status: 'complete', gaps: [], registered_at: '2026-06-28T08:00:00Z' }],
    },
    runtime: {
      cron: [{ schedule: '0 4 * * *', command: 'nightly_intelligence_pipeline.sh', last_run: '2026-06-28T04:00:00Z' }],
      daemons: [{ pid: '4883', name: 'receipt_processor' }],
      daemons_running: 1,
    },
    ...over,
  };
}

function renderWith(data: ObservabilityEnvelope | undefined, opts: { isLoading?: boolean; error?: Error } = {}) {
  mockUse.mockReturnValue({ data, isLoading: opts.isLoading ?? false, error: opts.error, mutate: jest.fn(), isValidating: false } as ReturnType<typeof useObservability>);
  return render(<ObservabilityPage />);
}

describe('ObservabilityPage', () => {
  test('renders all four sections with data', () => {
    renderWith(envelope());
    expect(screen.getByTestId('observability-page')).toBeInTheDocument();
    expect(screen.getByTestId('obs-section-self-learning')).toBeInTheDocument();
    expect(screen.getByTestId('obs-section-tagging-agent')).toBeInTheDocument();
    expect(screen.getByTestId('obs-section-provenance')).toBeInTheDocument();
    expect(screen.getByTestId('obs-section-runtime-health')).toBeInTheDocument();
    expect(screen.getByTestId('obs-learning-row')).toHaveTextContent('D-1');
    expect(screen.getByTestId('obs-tagging-row')).toHaveTextContent('security');
    expect(screen.getByTestId('obs-chain-complete')).toHaveTextContent('complete: 4');
    expect(screen.getByTestId('obs-provenance-row')).toHaveTextContent('D-2');
    expect(screen.getByTestId('obs-daemons')).toHaveTextContent('1 daemon');
    expect(screen.getByTestId('obs-cron-row')).toHaveTextContent('nightly_intelligence_pipeline');
  });

  test('shows degraded flag + empty states', () => {
    renderWith(envelope({
      tagging: { events: [], degraded: true },
      provenance: { by_status: {}, recent: [], degraded: true },
      self_learning: { events: [], proposals: 0 },
      runtime: { cron: [], daemons: [], daemons_running: 0 },
    }));
    expect(screen.getByTestId('obs-degraded-tagging-agent')).toBeInTheDocument();
    expect(screen.getByTestId('obs-daemons')).toHaveTextContent('0 daemon');
  });

  test('loading and error states', () => {
    const { rerender } = renderWith(undefined, { isLoading: true });
    expect(screen.getByTestId('observability-loading')).toBeInTheDocument();
    mockUse.mockReturnValue({ data: undefined, isLoading: false, error: new Error('x'), mutate: jest.fn(), isValidating: false } as ReturnType<typeof useObservability>);
    rerender(<ObservabilityPage />);
    expect(screen.getByTestId('observability-error')).toBeInTheDocument();
  });
});
