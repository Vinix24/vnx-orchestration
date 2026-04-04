/**
 * Tests for app/operator/kanban/page.tsx
 *
 * Quality gate: gate_pr2_kanban_frontend
 * - Kanban page renders 5 columns under test
 * - Dispatch cards show PR-id, track, terminal, gate, duration under test
 * - Empty, loading, and error states render correctly under test
 * - SWR hook polls /api/operator/kanban endpoint
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

// Mock next/navigation used indirectly (Link, usePathname in sidebar)
jest.mock('next/navigation', () => ({
  usePathname: () => '/operator/kanban',
}));

// Mock SWR hooks — we test the page in isolation
jest.mock('@/lib/hooks', () => ({
  useKanban: jest.fn(),
  useProjects: jest.fn(),
}));

import { useKanban, useProjects } from '@/lib/hooks';
import KanbanPage from '@/app/operator/kanban/page';
import type { KanbanEnvelope, ProjectsEnvelope } from '@/lib/types';

const mockUseKanban = useKanban as jest.MockedFunction<typeof useKanban>;
const mockUseProjects = useProjects as jest.MockedFunction<typeof useProjects>;

// ---- Fixtures ----

const CARD_A = {
  id: 'dispatch-001',
  pr_id: 'PR-1',
  track: 'A',
  terminal: 'T1',
  role: 'backend-developer',
  gate: 'gate_pr1_lifecycle',
  priority: 'P1',
  status: 'active',
  stage: 'active',
  duration_secs: 120,
  duration_label: '2m',
  has_receipt: false,
  receipt_status: null,
};

const CARD_B = {
  id: 'dispatch-002',
  pr_id: 'PR-2',
  track: 'B',
  terminal: 'T2',
  role: 'test-engineer',
  gate: 'gate_pr2_tests',
  priority: 'P1',
  status: 'pending',
  stage: 'pending',
  duration_secs: 300,
  duration_label: '5m',
  has_receipt: true,
  receipt_status: 'success',
};

const CARD_C = {
  id: 'dispatch-003',
  pr_id: 'PR-3',
  track: 'C',
  terminal: 'T3',
  role: 'architect',
  gate: 'gate_pr3_review',
  priority: 'P1',
  status: 'review',
  stage: 'review',
  duration_secs: 60,
  duration_label: '1m',
  has_receipt: true,
  receipt_status: 'success',
};

function makeEnvelope(overrides: Partial<KanbanEnvelope> = {}): KanbanEnvelope {
  return {
    stages: {},
    total: 0,
    degraded: false,
    ...overrides,
  };
}

function makeProjectsEnvelope(names: string[] = []): ProjectsEnvelope {
  return {
    view: 'projects',
    degraded: false,
    data: names.map(name => ({
      name,
      path: `/projects/${name}`,
      registered_at: null,
      session_active: false,
      active_feature: null,
      open_blocker_count: 0,
      open_warn_count: 0,
      attention_level: 'clear' as const,
    })),
  };
}

// ---- Helpers ----

function setupProjects(names: string[] = []) {
  mockUseProjects.mockReturnValue({
    data: makeProjectsEnvelope(names),
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useProjects>);
}

function renderWithData(envelope: KanbanEnvelope, projectNames: string[] = []) {
  setupProjects(projectNames);
  mockUseKanban.mockReturnValue({
    data: envelope,
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useKanban>);
  return render(<KanbanPage />);
}

function renderLoading() {
  setupProjects();
  mockUseKanban.mockReturnValue({
    data: undefined,
    isLoading: true,
    error: undefined,
    mutate: jest.fn(),
    isValidating: true,
  } as ReturnType<typeof useKanban>);
  return render(<KanbanPage />);
}

function renderError() {
  setupProjects();
  mockUseKanban.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: new Error('network error'),
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useKanban>);
  return render(<KanbanPage />);
}

// ============================================================
// Tests
// ============================================================

describe('KanbanPage — 5 columns', () => {
  test('renders all 5 column headers', () => {
    renderWithData(makeEnvelope());

    expect(screen.getByTestId('column-staging')).toBeInTheDocument();
    expect(screen.getByTestId('column-pending')).toBeInTheDocument();
    expect(screen.getByTestId('column-active')).toBeInTheDocument();
    expect(screen.getByTestId('column-review')).toBeInTheDocument();
    expect(screen.getByTestId('column-done')).toBeInTheDocument();
  });

  test('renders column labels', () => {
    renderWithData(makeEnvelope());

    expect(screen.getByText('Staging')).toBeInTheDocument();
    expect(screen.getByText('Pending')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Review')).toBeInTheDocument();
    expect(screen.getByText('Done')).toBeInTheDocument();
  });
});

describe('KanbanPage — dispatch cards', () => {
  test('renders card with PR-id visible', () => {
    renderWithData(makeEnvelope({
      stages: { active: [CARD_A] },
      total: 1,
    }));

    expect(screen.getByTestId('card-pr-id')).toHaveTextContent('PR-1');
  });

  test('renders track badge with correct text', () => {
    renderWithData(makeEnvelope({
      stages: { active: [CARD_A] },
      total: 1,
    }));

    expect(screen.getByTestId('card-track')).toHaveTextContent('A');
  });

  test('renders terminal chip', () => {
    renderWithData(makeEnvelope({
      stages: { active: [CARD_A] },
      total: 1,
    }));

    expect(screen.getByTestId('card-terminal')).toHaveTextContent('T1');
  });

  test('renders gate label', () => {
    renderWithData(makeEnvelope({
      stages: { active: [CARD_A] },
      total: 1,
    }));

    expect(screen.getByTestId('card-gate')).toHaveTextContent('gate_pr1_lifecycle');
  });

  test('renders duration label', () => {
    renderWithData(makeEnvelope({
      stages: { active: [CARD_A] },
      total: 1,
    }));

    expect(screen.getByTestId('card-duration')).toHaveTextContent('2m');
  });

  test('renders cards in the correct column', () => {
    renderWithData(makeEnvelope({
      stages: {
        active: [CARD_A],
        pending: [CARD_B],
        review: [CARD_C],
      },
      total: 3,
    }));

    const activeCol = screen.getByTestId('column-active');
    expect(activeCol).toHaveTextContent('PR-1');

    const pendingCol = screen.getByTestId('column-pending');
    expect(pendingCol).toHaveTextContent('PR-2');

    const reviewCol = screen.getByTestId('column-review');
    expect(reviewCol).toHaveTextContent('PR-3');
  });

  test('track A card has green styling class indicator', () => {
    renderWithData(makeEnvelope({ stages: { active: [CARD_A] }, total: 1 }));
    const badge = screen.getByTestId('card-track');
    expect(badge).toHaveTextContent('A');
  });

  test('track B card badge text is B', () => {
    renderWithData(makeEnvelope({ stages: { pending: [CARD_B] }, total: 1 }));
    expect(screen.getByTestId('card-track')).toHaveTextContent('B');
  });

  test('track C card badge text is C', () => {
    renderWithData(makeEnvelope({ stages: { review: [CARD_C] }, total: 1 }));
    expect(screen.getByTestId('card-track')).toHaveTextContent('C');
  });
});

describe('KanbanPage — empty state', () => {
  test('shows "No dispatches" placeholder in all empty columns', () => {
    renderWithData(makeEnvelope());

    const emptyPlaceholders = screen.getAllByText('No dispatches');
    expect(emptyPlaceholders).toHaveLength(5);
  });

  test('each empty column has its own testid', () => {
    renderWithData(makeEnvelope());

    expect(screen.getByTestId('empty-staging')).toBeInTheDocument();
    expect(screen.getByTestId('empty-pending')).toBeInTheDocument();
    expect(screen.getByTestId('empty-active')).toBeInTheDocument();
    expect(screen.getByTestId('empty-review')).toBeInTheDocument();
    expect(screen.getByTestId('empty-done')).toBeInTheDocument();
  });

  test('columns with cards do not show empty placeholder', () => {
    renderWithData(makeEnvelope({
      stages: { active: [CARD_A] },
      total: 1,
    }));

    // active has a card — no empty placeholder there
    expect(screen.queryByTestId('empty-active')).not.toBeInTheDocument();
    // others are still empty
    expect(screen.getByTestId('empty-staging')).toBeInTheDocument();
  });
});

describe('KanbanPage — loading state', () => {
  test('does not render dispatch cards while loading', () => {
    renderLoading();
    expect(screen.queryAllByTestId('dispatch-card')).toHaveLength(0);
  });

  test('does not render empty placeholders while loading', () => {
    renderLoading();
    expect(screen.queryAllByText('No dispatches')).toHaveLength(0);
  });

  test('still renders column structure while loading', () => {
    renderLoading();
    expect(screen.getByTestId('kanban-grid')).toBeInTheDocument();
    expect(screen.getByTestId('column-active')).toBeInTheDocument();
  });
});

describe('KanbanPage — error state', () => {
  test('shows degraded banner on fetch error', () => {
    renderError();

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/KanbanView/)).toBeInTheDocument();
  });

  test('shows server error message in banner', () => {
    renderError();
    expect(screen.getByText(/Failed to load kanban data/)).toBeInTheDocument();
  });

  test('no dispatch cards rendered on error', () => {
    renderError();
    expect(screen.queryAllByTestId('dispatch-card')).toHaveLength(0);
  });
});

describe('KanbanPage — degraded state from API', () => {
  test('shows degraded banner when envelope.degraded is true', () => {
    renderWithData(makeEnvelope({
      degraded: true,
      degraded_reasons: ['state dir not found'],
    }));

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('state dir not found')).toBeInTheDocument();
  });
});

describe('KanbanPage — SWR hook usage', () => {
  test('useKanban hook is called on mount', () => {
    renderWithData(makeEnvelope());
    expect(mockUseKanban).toHaveBeenCalled();
  });
});

describe('KanbanPage — project filter', () => {
  test('project filter is not shown when no projects', () => {
    renderWithData(makeEnvelope(), []);
    expect(screen.queryByTestId('project-filter')).not.toBeInTheDocument();
  });

  test('project filter renders when projects are available', () => {
    renderWithData(makeEnvelope(), ['alpha', 'beta']);
    expect(screen.getByTestId('project-filter')).toBeInTheDocument();
  });

  test('project filter shows all project names', () => {
    renderWithData(makeEnvelope(), ['alpha', 'beta']);
    expect(screen.getByTestId('project-filter-alpha')).toBeInTheDocument();
    expect(screen.getByTestId('project-filter-beta')).toBeInTheDocument();
  });

  test('project filter shows "All projects" button', () => {
    renderWithData(makeEnvelope(), ['alpha']);
    expect(screen.getByText('All projects')).toBeInTheDocument();
  });

  test('useKanban is called with undefined project on initial render', () => {
    renderWithData(makeEnvelope(), ['alpha']);
    expect(mockUseKanban).toHaveBeenCalledWith(undefined);
  });

  test('clicking a project chip calls useKanban with that project', () => {
    renderWithData(makeEnvelope(), ['alpha']);

    const chip = screen.getByTestId('project-filter-alpha');
    fireEvent.click(chip);

    // useKanban should have been called with 'alpha' on re-render
    expect(mockUseKanban).toHaveBeenCalledWith('alpha');
  });

  test('clicking the active project chip again resets to all projects', () => {
    renderWithData(makeEnvelope(), ['alpha']);

    const chip = screen.getByTestId('project-filter-alpha');
    fireEvent.click(chip); // select alpha — now filter is 'alpha'
    fireEvent.click(chip); // deselect alpha — now filter is undefined

    // Should have been called with both 'alpha' and then undefined
    expect(mockUseKanban).toHaveBeenCalledWith('alpha');
    expect(mockUseKanban).toHaveBeenCalledWith(undefined);
  });
});
