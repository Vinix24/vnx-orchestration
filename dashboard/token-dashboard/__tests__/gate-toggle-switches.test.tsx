/**
 * Tests for gate toggle switches in components/operator/project-card.tsx
 *
 * Quality gate: gate_pr3_gate_toggle_switches
 * - Toggle switches render on project cards under test
 * - Toggle fires POST request and updates state under test
 * - Color indicators reflect current gate state
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('next/navigation', () => ({
  usePathname: () => '/operator',
}));

jest.mock('@/lib/hooks', () => ({
  useGateConfig: jest.fn(),
}));

jest.mock('@/lib/operator-api', () => ({
  actionStartSession: jest.fn(),
  actionStopSession: jest.fn(),
  actionRefreshProjections: jest.fn(),
  postGateToggle: jest.fn(),
}));

import { useGateConfig } from '@/lib/hooks';
import { postGateToggle } from '@/lib/operator-api';
import ProjectCard from '@/components/operator/project-card';
import type { ProjectEntry, GateConfigResponse } from '@/lib/types';

const mockUseGateConfig = useGateConfig as jest.MockedFunction<typeof useGateConfig>;
const mockPostGateToggle = postGateToggle as jest.MockedFunction<typeof postGateToggle>;

// ---- Fixtures ----

const BASE_PROJECT: ProjectEntry = {
  name: 'my-project',
  path: '/projects/my-project',
  registered_at: null,
  session_active: false,
  active_feature: null,
  open_blocker_count: 0,
  open_warn_count: 0,
  attention_level: 'clear',
};

function makeGateConfig(overrides: Partial<Record<string, { enabled: boolean }>> = {}): GateConfigResponse {
  return {
    project: '/projects/my-project',
    config_path: '/projects/my-project/.vnx/gate_config.json',
    queried_at: '2026-04-03T21:42:00Z',
    gates: {
      gemini_review: { enabled: true },
      codex_gate: { enabled: true },
      ...overrides,
    },
  };
}

function setupGateConfig(config: GateConfigResponse) {
  const mutate = jest.fn().mockResolvedValue(undefined);
  mockUseGateConfig.mockReturnValue({
    data: config,
    isLoading: false,
    error: undefined,
    mutate,
    isValidating: false,
  } as ReturnType<typeof useGateConfig>);
  return { mutate };
}

function renderCard(project: ProjectEntry = BASE_PROJECT) {
  return render(<ProjectCard project={project} />);
}

// ============================================================
// Toggle renders
// ============================================================

describe('ProjectCard gate toggles — rendering', () => {
  test('gate toggles section renders on the card', () => {
    setupGateConfig(makeGateConfig());
    renderCard();
    expect(screen.getByTestId('gate-toggles')).toBeInTheDocument();
  });

  test('gemini_review toggle switch renders', () => {
    setupGateConfig(makeGateConfig());
    renderCard();
    expect(screen.getByTestId('gate-toggle-gemini_review')).toBeInTheDocument();
  });

  test('codex_gate toggle switch renders', () => {
    setupGateConfig(makeGateConfig());
    renderCard();
    expect(screen.getByTestId('gate-toggle-codex_gate')).toBeInTheDocument();
  });

  test('gate labels are visible', () => {
    setupGateConfig(makeGateConfig());
    renderCard();
    expect(screen.getByText('Gemini Review')).toBeInTheDocument();
    expect(screen.getByText('Codex Gate')).toBeInTheDocument();
  });

  test('toggle has role="switch"', () => {
    setupGateConfig(makeGateConfig());
    renderCard();
    const switches = screen.getAllByRole('switch');
    expect(switches.length).toBeGreaterThanOrEqual(2);
  });
});

// ============================================================
// Color indicators
// ============================================================

describe('ProjectCard gate toggles — color indicators', () => {
  test('enabled gate indicator has aria-checked=true on switch', () => {
    setupGateConfig(makeGateConfig({ gemini_review: { enabled: true } }));
    renderCard();
    const toggle = screen.getByTestId('gate-toggle-gemini_review');
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  test('disabled gate indicator has aria-checked=false on switch', () => {
    setupGateConfig(makeGateConfig({ gemini_review: { enabled: false } }));
    renderCard();
    const toggle = screen.getByTestId('gate-toggle-gemini_review');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
  });

  test('codex_gate disabled shows aria-checked=false', () => {
    setupGateConfig(makeGateConfig({ codex_gate: { enabled: false } }));
    renderCard();
    expect(screen.getByTestId('gate-toggle-codex_gate')).toHaveAttribute('aria-checked', 'false');
  });

  test('gemini indicator is present for enabled gate', () => {
    setupGateConfig(makeGateConfig({ gemini_review: { enabled: true } }));
    renderCard();
    expect(screen.getByTestId('gate-indicator-gemini_review')).toBeInTheDocument();
  });

  test('codex indicator is present for enabled gate', () => {
    setupGateConfig(makeGateConfig({ codex_gate: { enabled: true } }));
    renderCard();
    expect(screen.getByTestId('gate-indicator-codex_gate')).toBeInTheDocument();
  });

  test('defaults to enabled when gate config is not loaded', () => {
    mockUseGateConfig.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: undefined,
      mutate: jest.fn(),
      isValidating: true,
    } as ReturnType<typeof useGateConfig>);
    renderCard();
    // Should still render — defaults to enabled
    const toggle = screen.getByTestId('gate-toggle-gemini_review');
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  test('defaults to enabled when gate key is missing from config', () => {
    setupGateConfig({ ...makeGateConfig(), gates: {} });
    renderCard();
    expect(screen.getByTestId('gate-toggle-gemini_review')).toHaveAttribute('aria-checked', 'true');
  });
});

// ============================================================
// Toggle interaction — fires POST and refreshes
// ============================================================

describe('ProjectCard gate toggles — toggle interaction', () => {
  test('clicking enabled toggle calls postGateToggle with enabled=false', async () => {
    mockPostGateToggle.mockResolvedValue({
      action: 'toggle',
      project: '/projects/my-project',
      gate: 'gemini_review',
      enabled: false,
      status: 'success',
      message: 'Gate disabled',
      timestamp: '2026-04-03T21:42:00Z',
    });
    setupGateConfig(makeGateConfig({ gemini_review: { enabled: true } }));
    renderCard();

    fireEvent.click(screen.getByTestId('gate-toggle-gemini_review'));

    await waitFor(() => {
      expect(mockPostGateToggle).toHaveBeenCalledWith({
        project: '/projects/my-project',
        gate: 'gemini_review',
        enabled: false,
      });
    });
  });

  test('clicking disabled toggle calls postGateToggle with enabled=true', async () => {
    mockPostGateToggle.mockResolvedValue({
      action: 'toggle',
      project: '/projects/my-project',
      gate: 'gemini_review',
      enabled: true,
      status: 'success',
      message: 'Gate enabled',
      timestamp: '2026-04-03T21:42:00Z',
    });
    setupGateConfig(makeGateConfig({ gemini_review: { enabled: false } }));
    renderCard();

    fireEvent.click(screen.getByTestId('gate-toggle-gemini_review'));

    await waitFor(() => {
      expect(mockPostGateToggle).toHaveBeenCalledWith({
        project: '/projects/my-project',
        gate: 'gemini_review',
        enabled: true,
      });
    });
  });

  test('clicking codex_gate toggle fires POST for codex_gate', async () => {
    mockPostGateToggle.mockResolvedValue({
      action: 'toggle',
      project: '/projects/my-project',
      gate: 'codex_gate',
      enabled: false,
      status: 'success',
      message: 'Gate disabled',
      timestamp: '2026-04-03T21:42:00Z',
    });
    setupGateConfig(makeGateConfig({ codex_gate: { enabled: true } }));
    renderCard();

    fireEvent.click(screen.getByTestId('gate-toggle-codex_gate'));

    await waitFor(() => {
      expect(mockPostGateToggle).toHaveBeenCalledWith({
        project: '/projects/my-project',
        gate: 'codex_gate',
        enabled: false,
      });
    });
  });

  test('mutate is called after successful toggle to refresh gate state', async () => {
    mockPostGateToggle.mockResolvedValue({
      action: 'toggle',
      project: '/projects/my-project',
      gate: 'gemini_review',
      enabled: false,
      status: 'success',
      message: 'ok',
      timestamp: '2026-04-03T21:42:00Z',
    });
    const { mutate } = setupGateConfig(makeGateConfig());
    renderCard();

    fireEvent.click(screen.getByTestId('gate-toggle-gemini_review'));

    await waitFor(() => {
      expect(mutate).toHaveBeenCalled();
    });
  });

  test('toggle button is disabled while POST is in flight', async () => {
    let resolveToggle!: () => void;
    mockPostGateToggle.mockReturnValue(
      new Promise(resolve => {
        resolveToggle = () => resolve({
          action: 'toggle',
          project: '/projects/my-project',
          gate: 'gemini_review',
          enabled: false,
          status: 'success',
          message: 'ok',
          timestamp: '2026-04-03T21:42:00Z',
        });
      })
    );
    setupGateConfig(makeGateConfig());
    renderCard();

    fireEvent.click(screen.getByTestId('gate-toggle-gemini_review'));

    // Button should become disabled while pending
    expect(screen.getByTestId('gate-toggle-gemini_review')).toBeDisabled();

    // Resolve the promise
    resolveToggle();
    await waitFor(() => {
      expect(screen.getByTestId('gate-toggle-gemini_review')).not.toBeDisabled();
    });
  });

  test('toggling one gate does not disable the other gate toggle', async () => {
    mockPostGateToggle.mockResolvedValue({
      action: 'toggle', project: '/projects/my-project', gate: 'gemini_review',
      enabled: false, status: 'success', message: 'ok', timestamp: '2026-04-03T21:42:00Z',
    });
    setupGateConfig(makeGateConfig());
    renderCard();

    fireEvent.click(screen.getByTestId('gate-toggle-gemini_review'));

    // codex_gate should still be enabled/clickable during gemini toggle
    expect(screen.getByTestId('gate-toggle-codex_gate')).not.toBeDisabled();
  });
});
