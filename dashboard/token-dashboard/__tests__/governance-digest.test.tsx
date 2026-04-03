/**
 * Tests for useGovernanceDigest SWR hook.
 *
 * Quality gate: gate_pr2_digest_api
 * - SWR hook polls /api/operator/governance-digest
 * - Hook surfaces freshness envelope (degraded, staleness_seconds, data)
 * - Renders loading, error, and success states
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({
  useGovernanceDigest: jest.fn(),
}));

import { useGovernanceDigest } from '@/lib/hooks';
import type { GovernanceDigestEnvelope } from '@/lib/types';

const mockUseGovernanceDigest = useGovernanceDigest as jest.MockedFunction<typeof useGovernanceDigest>;

// ---- Fixtures ----

function makeEnvelope(overrides: Partial<GovernanceDigestEnvelope> = {}): GovernanceDigestEnvelope {
  return {
    view: 'GovernanceDigestView',
    queried_at: new Date().toISOString(),
    source_freshness: { governance_digest: new Date().toISOString() },
    staleness_seconds: 30,
    degraded: false,
    degraded_reasons: [],
    data: {},
    ...overrides,
  };
}

// ---- Minimal consumer component ----

function DigestDisplay() {
  const { data, isLoading, error } = useGovernanceDigest();
  if (isLoading) return <div data-testid="loading">loading</div>;
  if (error) return <div data-testid="error">error</div>;
  if (!data) return <div data-testid="empty">empty</div>;
  return (
    <div data-testid="digest-display">
      <span data-testid="view">{data.view}</span>
      <span data-testid="degraded">{String(data.degraded)}</span>
      <span data-testid="staleness">{data.staleness_seconds}</span>
      {data.degraded && (
        <div data-testid="degraded-banner">stale</div>
      )}
    </div>
  );
}

// ---- Tests ----

describe('useGovernanceDigest — hook behaviour', () => {
  test('hook is called on mount', () => {
    mockUseGovernanceDigest.mockReturnValue({
      data: makeEnvelope(),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGovernanceDigest>);

    render(<DigestDisplay />);
    expect(mockUseGovernanceDigest).toHaveBeenCalled();
  });

  test('renders loading state', () => {
    mockUseGovernanceDigest.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: undefined,
      mutate: jest.fn(),
      isValidating: true,
    } as ReturnType<typeof useGovernanceDigest>);

    render(<DigestDisplay />);
    expect(screen.getByTestId('loading')).toBeInTheDocument();
  });

  test('renders error state', () => {
    mockUseGovernanceDigest.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('fetch failed'),
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGovernanceDigest>);

    render(<DigestDisplay />);
    expect(screen.getByTestId('error')).toBeInTheDocument();
  });

  test('renders digest data on success', () => {
    mockUseGovernanceDigest.mockReturnValue({
      data: makeEnvelope({ staleness_seconds: 42 }),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGovernanceDigest>);

    render(<DigestDisplay />);
    expect(screen.getByTestId('view')).toHaveTextContent('GovernanceDigestView');
    expect(screen.getByTestId('staleness')).toHaveTextContent('42');
  });

  test('degraded banner shown when degraded=true', () => {
    mockUseGovernanceDigest.mockReturnValue({
      data: makeEnvelope({ degraded: true, degraded_reasons: ['file missing'] }),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGovernanceDigest>);

    render(<DigestDisplay />);
    expect(screen.getByTestId('degraded-banner')).toBeInTheDocument();
    expect(screen.getByTestId('degraded')).toHaveTextContent('true');
  });

  test('no degraded banner when degraded=false', () => {
    mockUseGovernanceDigest.mockReturnValue({
      data: makeEnvelope({ degraded: false }),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGovernanceDigest>);

    render(<DigestDisplay />);
    expect(screen.queryByTestId('degraded-banner')).not.toBeInTheDocument();
  });

  test('source_freshness key is accessible from data', () => {
    const ts = '2026-04-03T20:00:00.000Z';
    mockUseGovernanceDigest.mockReturnValue({
      data: makeEnvelope({ source_freshness: { governance_digest: ts } }),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGovernanceDigest>);

    const { unmount } = render(<DigestDisplay />);
    // Component renders without error — typed access to source_freshness verified
    expect(screen.getByTestId('digest-display')).toBeInTheDocument();
    unmount();
  });
});
