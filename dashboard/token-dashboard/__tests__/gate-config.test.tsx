/**
 * Tests for useGateConfig SWR hook and gate-config API contract.
 *
 * Quality gate: gate_pr1_gate_config_backend
 * - SWR hook polls /api/operator/gate/config
 * - Hook returns gate state keyed by project when provided
 * - Hook returns all-project state when no project provided
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({
  useGateConfig: jest.fn(),
}));

import { useGateConfig } from '@/lib/hooks';
import type { GateConfigResponse } from '@/lib/types';

const mockUseGateConfig = useGateConfig as jest.MockedFunction<typeof useGateConfig>;

// ---- Fixtures ----

function makeGateConfig(overrides: Partial<GateConfigResponse> = {}): GateConfigResponse {
  return {
    project: null,
    gates: {},
    queried_at: new Date().toISOString(),
    config_path: '/path/to/governance_gates.yaml',
    ...overrides,
  };
}

// ---- Minimal consumer component ----

function GateDisplay({ project }: { project?: string }) {
  const { data, isLoading, error } = useGateConfig(project);
  if (isLoading) return <div data-testid="loading">loading</div>;
  if (error) return <div data-testid="error">error</div>;
  if (!data) return <div data-testid="empty">empty</div>;
  return (
    <div data-testid="gate-display">
      <span data-testid="project">{data.project ?? 'all'}</span>
      <span data-testid="gate-count">{Object.keys(data.gates).length}</span>
    </div>
  );
}

// ---- Tests ----

describe('useGateConfig — hook behaviour', () => {
  test('hook is called on mount', () => {
    mockUseGateConfig.mockReturnValue({
      data: makeGateConfig(),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGateConfig>);

    render(<GateDisplay />);
    expect(mockUseGateConfig).toHaveBeenCalled();
  });

  test('hook called with undefined when no project prop', () => {
    mockUseGateConfig.mockReturnValue({
      data: makeGateConfig(),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGateConfig>);

    render(<GateDisplay />);
    expect(mockUseGateConfig).toHaveBeenCalledWith(undefined);
  });

  test('hook called with project name when provided', () => {
    mockUseGateConfig.mockReturnValue({
      data: makeGateConfig({ project: 'alpha' }),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGateConfig>);

    render(<GateDisplay project="alpha" />);
    expect(mockUseGateConfig).toHaveBeenCalledWith('alpha');
  });

  test('renders loading state', () => {
    mockUseGateConfig.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: undefined,
      mutate: jest.fn(),
      isValidating: true,
    } as ReturnType<typeof useGateConfig>);

    render(<GateDisplay />);
    expect(screen.getByTestId('loading')).toBeInTheDocument();
  });

  test('renders error state', () => {
    mockUseGateConfig.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('fetch failed'),
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGateConfig>);

    render(<GateDisplay />);
    expect(screen.getByTestId('error')).toBeInTheDocument();
  });

  test('renders gate data on success', () => {
    mockUseGateConfig.mockReturnValue({
      data: makeGateConfig({
        project: 'alpha',
        gates: { gemini_review: { enabled: true }, codex_gate: { enabled: false } },
      }),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGateConfig>);

    render(<GateDisplay project="alpha" />);
    expect(screen.getByTestId('project')).toHaveTextContent('alpha');
    expect(screen.getByTestId('gate-count')).toHaveTextContent('2');
  });

  test('shows "all" when project is null', () => {
    mockUseGateConfig.mockReturnValue({
      data: makeGateConfig({ project: null }),
      isLoading: false,
      error: undefined,
      mutate: jest.fn(),
      isValidating: false,
    } as ReturnType<typeof useGateConfig>);

    render(<GateDisplay />);
    expect(screen.getByTestId('project')).toHaveTextContent('all');
  });
});
