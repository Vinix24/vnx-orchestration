/**
 * Tests for session control buttons in components/operator/project-card.tsx
 *
 * Quality gate: gate_pr3_session_control_ui
 * - Start button triggers session creation under test
 * - Stop button triggers session teardown under test
 * - Attach button triggers terminal attach under test
 * - Session state indicator reflects running/stopped under test
 * - Outcome toasts display correctly
 * - Buttons disabled during action execution
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
  actionAttachTerminal: jest.fn(),
  actionRefreshProjections: jest.fn(),
  postGateToggle: jest.fn(),
}));

import { useGateConfig } from '@/lib/hooks';
import {
  actionStartSession,
  actionStopSession,
  actionAttachTerminal,
  actionRefreshProjections,
} from '@/lib/operator-api';
import ProjectCard from '@/components/operator/project-card';
import type { ProjectEntry, ActionOutcome } from '@/lib/types';

const mockUseGateConfig = useGateConfig as jest.MockedFunction<typeof useGateConfig>;
const mockStartSession = actionStartSession as jest.MockedFunction<typeof actionStartSession>;
const mockStopSession = actionStopSession as jest.MockedFunction<typeof actionStopSession>;
const mockAttachTerminal = actionAttachTerminal as jest.MockedFunction<typeof actionAttachTerminal>;
const mockRefreshProjections = actionRefreshProjections as jest.MockedFunction<typeof actionRefreshProjections>;

// ---- Fixtures ----

const INACTIVE_PROJECT: ProjectEntry = {
  name: 'test-project',
  path: '/projects/test-project',
  registered_at: null,
  session_active: false,
  active_feature: null,
  open_blocker_count: 0,
  open_warn_count: 0,
  attention_level: 'clear',
};

const ACTIVE_PROJECT: ProjectEntry = {
  ...INACTIVE_PROJECT,
  session_active: true,
  active_feature: 'Feature 26',
};

function makeOutcome(overrides: Partial<ActionOutcome> = {}): ActionOutcome {
  return {
    action: 'start',
    project: '/projects/test-project',
    status: 'success',
    message: 'Session started successfully',
    timestamp: '2026-04-04T06:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  const mutate = jest.fn().mockResolvedValue(undefined);
  mockUseGateConfig.mockReturnValue({
    data: {
      project: '/projects/test-project',
      config_path: '/projects/test-project/.vnx/gate_config.json',
      queried_at: '2026-04-04T06:00:00Z',
      gates: { gemini_review: { enabled: true }, codex_gate: { enabled: true } },
    },
    isLoading: false,
    error: undefined,
    mutate,
    isValidating: false,
  } as ReturnType<typeof useGateConfig>);
});

function renderCard(project: ProjectEntry = INACTIVE_PROJECT, onActionComplete?: (o: ActionOutcome) => void) {
  return render(<ProjectCard project={project} onActionComplete={onActionComplete} />);
}

// ============================================================
// Session state indicator
// ============================================================

describe('ProjectCard session indicator', () => {
  test('shows "Inactive" badge when session_active is false', () => {
    renderCard(INACTIVE_PROJECT);
    const badge = screen.getByTestId('session-badge');
    expect(badge).toHaveTextContent('Inactive');
  });

  test('shows "Active" badge when session_active is true', () => {
    renderCard(ACTIVE_PROJECT);
    const badge = screen.getByTestId('session-badge');
    expect(badge).toHaveTextContent('Active');
  });
});

// ============================================================
// Start button
// ============================================================

describe('ProjectCard start button', () => {
  test('shows Start Session button when session is inactive', () => {
    renderCard(INACTIVE_PROJECT);
    expect(screen.getByTestId('btn-start')).toHaveTextContent('Start Session');
  });

  test('does not show Start button when session is active', () => {
    renderCard(ACTIVE_PROJECT);
    expect(screen.queryByTestId('btn-start')).not.toBeInTheDocument();
  });

  test('clicking Start calls actionStartSession with project path', async () => {
    mockStartSession.mockResolvedValue(makeOutcome({ action: 'start' }));
    renderCard(INACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-start'));

    await waitFor(() => {
      expect(mockStartSession).toHaveBeenCalledWith('/projects/test-project');
    });
  });

  test('Start button shows "Starting…" text while pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockStartSession.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(INACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-start'));

    expect(screen.getByTestId('btn-start')).toHaveTextContent('Starting…');

    resolve(makeOutcome());
    await waitFor(() => {
      expect(screen.getByTestId('btn-start')).toHaveTextContent('Start Session');
    });
  });

  test('Start button is disabled while action is pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockStartSession.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(INACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-start'));

    expect(screen.getByTestId('btn-start')).toBeDisabled();

    resolve(makeOutcome());
    await waitFor(() => {
      expect(screen.getByTestId('btn-start')).not.toBeDisabled();
    });
  });

  test('onActionComplete callback fires after start', async () => {
    const outcome = makeOutcome({ action: 'start', status: 'success' });
    mockStartSession.mockResolvedValue(outcome);
    const callback = jest.fn();
    renderCard(INACTIVE_PROJECT, callback);

    fireEvent.click(screen.getByTestId('btn-start'));

    await waitFor(() => {
      expect(callback).toHaveBeenCalledWith(outcome);
    });
  });
});

// ============================================================
// Stop button
// ============================================================

describe('ProjectCard stop button', () => {
  test('shows Stop Session button when session is active', () => {
    renderCard(ACTIVE_PROJECT);
    expect(screen.getByTestId('btn-stop')).toHaveTextContent('Stop Session');
  });

  test('does not show Stop button when session is inactive', () => {
    renderCard(INACTIVE_PROJECT);
    expect(screen.queryByTestId('btn-stop')).not.toBeInTheDocument();
  });

  test('clicking Stop calls actionStopSession with project path', async () => {
    mockStopSession.mockResolvedValue(makeOutcome({ action: 'stop' }));
    renderCard(ACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-stop'));

    await waitFor(() => {
      expect(mockStopSession).toHaveBeenCalledWith('/projects/test-project');
    });
  });

  test('Stop button shows "Stopping…" text while pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockStopSession.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(ACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-stop'));

    expect(screen.getByTestId('btn-stop')).toHaveTextContent('Stopping…');

    resolve(makeOutcome({ action: 'stop' }));
    await waitFor(() => {
      expect(screen.getByTestId('btn-stop')).toHaveTextContent('Stop Session');
    });
  });

  test('Stop button is disabled while action is pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockStopSession.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(ACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-stop'));

    expect(screen.getByTestId('btn-stop')).toBeDisabled();

    resolve(makeOutcome({ action: 'stop' }));
    await waitFor(() => {
      expect(screen.getByTestId('btn-stop')).not.toBeDisabled();
    });
  });
});

// ============================================================
// Attach button
// ============================================================

describe('ProjectCard attach button', () => {
  test('shows Attach button when session is active', () => {
    renderCard(ACTIVE_PROJECT);
    expect(screen.getByTestId('btn-attach')).toHaveTextContent('Attach');
  });

  test('does not show Attach button when session is inactive', () => {
    renderCard(INACTIVE_PROJECT);
    expect(screen.queryByTestId('btn-attach')).not.toBeInTheDocument();
  });

  test('clicking Attach calls actionAttachTerminal with project path and T0', async () => {
    mockAttachTerminal.mockResolvedValue(makeOutcome({ action: 'attach' }));
    renderCard(ACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-attach'));

    await waitFor(() => {
      expect(mockAttachTerminal).toHaveBeenCalledWith('/projects/test-project', 'T0');
    });
  });

  test('Attach button shows "Attaching…" text while pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockAttachTerminal.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(ACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-attach'));

    expect(screen.getByTestId('btn-attach')).toHaveTextContent('Attaching…');

    resolve(makeOutcome({ action: 'attach' }));
    await waitFor(() => {
      expect(screen.getByTestId('btn-attach')).toHaveTextContent('Attach');
    });
  });

  test('Attach button is disabled while another action is pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockStopSession.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(ACTIVE_PROJECT);

    // Start a stop action first
    fireEvent.click(screen.getByTestId('btn-stop'));

    // Attach should be disabled
    expect(screen.getByTestId('btn-attach')).toBeDisabled();

    resolve(makeOutcome({ action: 'stop' }));
    await waitFor(() => {
      expect(screen.getByTestId('btn-attach')).not.toBeDisabled();
    });
  });
});

// ============================================================
// Outcome display
// ============================================================

describe('ProjectCard outcome display', () => {
  test('shows outcome message after successful start', async () => {
    mockStartSession.mockResolvedValue(makeOutcome({
      action: 'start',
      status: 'success',
      message: 'Session started successfully',
    }));
    renderCard(INACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-start'));

    await waitFor(() => {
      expect(screen.getByTestId('action-outcome')).toHaveTextContent('Session started successfully');
    });
  });

  test('shows outcome message after failed action', async () => {
    mockStartSession.mockResolvedValue(makeOutcome({
      action: 'start',
      status: 'failed',
      message: 'tmux not found',
    }));
    renderCard(INACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-start'));

    await waitFor(() => {
      expect(screen.getByTestId('action-outcome')).toHaveTextContent('tmux not found');
    });
  });

  test('shows fallback outcome when API throws', async () => {
    mockStartSession.mockRejectedValue(new Error('Network error'));
    renderCard(INACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-start'));

    await waitFor(() => {
      expect(screen.getByTestId('action-outcome')).toHaveTextContent('Network error');
    });
  });

  test('no outcome displayed before any action', () => {
    renderCard(INACTIVE_PROJECT);
    expect(screen.queryByTestId('action-outcome')).not.toBeInTheDocument();
  });

  test('onActionComplete receives failed outcome on API rejection', async () => {
    mockStopSession.mockRejectedValue(new Error('Connection refused'));
    const callback = jest.fn();
    renderCard(ACTIVE_PROJECT, callback);

    fireEvent.click(screen.getByTestId('btn-stop'));

    await waitFor(() => {
      expect(callback).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'failed', message: 'Connection refused' })
      );
    });
  });
});

// ============================================================
// Cross-button disable (optimistic UI)
// ============================================================

describe('ProjectCard optimistic UI', () => {
  test('refresh button is disabled while start action is pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockStartSession.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(INACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-start'));

    expect(screen.getByTestId('btn-refresh')).toBeDisabled();

    resolve(makeOutcome());
    await waitFor(() => {
      expect(screen.getByTestId('btn-refresh')).not.toBeDisabled();
    });
  });

  test('refresh button is disabled while stop action is pending', async () => {
    let resolve!: (v: ActionOutcome) => void;
    mockStopSession.mockReturnValue(new Promise(r => { resolve = r; }));
    renderCard(ACTIVE_PROJECT);

    fireEvent.click(screen.getByTestId('btn-stop'));

    expect(screen.getByTestId('btn-refresh')).toBeDisabled();

    resolve(makeOutcome({ action: 'stop' }));
    await waitFor(() => {
      expect(screen.getByTestId('btn-refresh')).not.toBeDisabled();
    });
  });
});
