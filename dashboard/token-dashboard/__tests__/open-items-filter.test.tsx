/**
 * Tests for app/operator/open-items/page.tsx
 *
 * Quality gate: gate_pr2_open_items_filter
 * - Project selector dropdown filters open items by project under test
 * - Severity chips show counts and filter items under test
 * - Empty state renders when no items match filters
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('next/navigation', () => ({
  usePathname: () => '/operator/open-items',
}));

jest.mock('@/lib/hooks', () => ({
  useAggregateOpenItems: jest.fn(),
  useProjects: jest.fn(),
}));

import { useAggregateOpenItems, useProjects } from '@/lib/hooks';
import OpenItemsPage from '@/app/operator/open-items/page';
import type { AggregateOpenItemsEnvelope, ProjectsEnvelope, OpenItem } from '@/lib/types';

const mockUseAggregate = useAggregateOpenItems as jest.MockedFunction<typeof useAggregateOpenItems>;
const mockUseProjects = useProjects as jest.MockedFunction<typeof useProjects>;

// ---- Fixtures ----

function makeItem(overrides: Partial<OpenItem> & { id: string }): OpenItem {
  return {
    severity: 'info',
    status: 'open',
    title: `Item ${overrides.id}`,
    ...overrides,
  };
}

const BLOCKER_ITEM = makeItem({ id: 'b1', severity: 'blocker', title: 'Critical deploy failure' });
const BLOCKING_ITEM = makeItem({ id: 'b2', severity: 'blocking', title: 'Blocking merge' });
const WARN_ITEM = makeItem({ id: 'w1', severity: 'warn', title: 'Flaky test detected' });
const WARNING_ITEM = makeItem({ id: 'w2', severity: 'warning', title: 'Memory pressure' });
const INFO_ITEM = makeItem({ id: 'i1', severity: 'info', title: 'Docs need update' });

function makeEnvelope(items: OpenItem[]): AggregateOpenItemsEnvelope {
  const blocker_count = items.filter(i => i.severity === 'blocker' || i.severity === 'blocking').length;
  const warn_count = items.filter(i => i.severity === 'warn' || i.severity === 'warning').length;
  const info_count = items.filter(i => i.severity === 'info').length;
  return {
    view: 'aggregate-open-items',
    degraded: false,
    data: {
      items,
      per_project_subtotals: {},
      total_summary: { blocker_count, warn_count, info_count },
    },
  };
}

function makeProjects(names: string[]): ProjectsEnvelope {
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

function setupMocks(items: OpenItem[], projectNames: string[] = []) {
  mockUseAggregate.mockReturnValue({
    data: makeEnvelope(items),
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useAggregateOpenItems>);

  mockUseProjects.mockReturnValue({
    data: makeProjects(projectNames),
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useProjects>);
}

function renderPage() {
  return render(<OpenItemsPage />);
}

// ============================================================
// Project dropdown tests
// ============================================================

describe('OpenItemsPage — project dropdown', () => {
  test('dropdown is not shown when no projects are registered', () => {
    setupMocks([INFO_ITEM], []);
    renderPage();
    expect(screen.queryByTestId('project-dropdown')).not.toBeInTheDocument();
  });

  test('dropdown renders when projects are available', () => {
    setupMocks([INFO_ITEM], ['alpha', 'beta']);
    renderPage();
    expect(screen.getByTestId('project-dropdown')).toBeInTheDocument();
  });

  test('dropdown includes "All projects" option and each project name', () => {
    setupMocks([INFO_ITEM], ['alpha', 'beta']);
    renderPage();
    const select = screen.getByTestId('project-dropdown') as HTMLSelectElement;
    const options = Array.from(select.options).map(o => o.value);
    expect(options).toContain('');       // "All projects"
    expect(options).toContain('alpha');
    expect(options).toContain('beta');
  });

  test('selecting a project calls useAggregateOpenItems with that project', () => {
    setupMocks([INFO_ITEM], ['alpha', 'beta']);
    renderPage();

    fireEvent.change(screen.getByTestId('project-dropdown'), { target: { value: 'alpha' } });

    // Hook re-invoked with 'alpha' on state change
    const calls = mockUseAggregate.mock.calls.map(c => c[0]);
    expect(calls).toContain('alpha');
  });

  test('selecting "All projects" calls useAggregateOpenItems with undefined', () => {
    setupMocks([INFO_ITEM], ['alpha']);
    renderPage();

    // First select a project, then go back to all
    fireEvent.change(screen.getByTestId('project-dropdown'), { target: { value: 'alpha' } });
    fireEvent.change(screen.getByTestId('project-dropdown'), { target: { value: '' } });

    const calls = mockUseAggregate.mock.calls.map(c => c[0]);
    expect(calls).toContain(undefined);
  });
});

// ============================================================
// Severity chip tests
// ============================================================

describe('OpenItemsPage — severity chips', () => {
  test('severity filter section renders when not loading', () => {
    setupMocks([BLOCKER_ITEM, WARN_ITEM, INFO_ITEM]);
    renderPage();
    expect(screen.getByTestId('severity-filter')).toBeInTheDocument();
  });

  test('severity chip shows correct count for blockers', () => {
    setupMocks([BLOCKER_ITEM, BLOCKING_ITEM, WARN_ITEM]);
    renderPage();
    // blocker + blocking both normalize to 'blocker'
    expect(screen.getByTestId('severity-count-blocker')).toHaveTextContent('2');
  });

  test('severity chip shows correct count for warnings', () => {
    setupMocks([WARN_ITEM, WARNING_ITEM, INFO_ITEM]);
    renderPage();
    // warn + warning both normalize to 'warn'
    expect(screen.getByTestId('severity-count-warn')).toHaveTextContent('2');
  });

  test('severity chip shows correct count for info', () => {
    setupMocks([INFO_ITEM, BLOCKER_ITEM]);
    renderPage();
    expect(screen.getByTestId('severity-count-info')).toHaveTextContent('1');
  });

  test('clicking blocker chip shows only blocker items', () => {
    setupMocks([BLOCKER_ITEM, WARN_ITEM, INFO_ITEM]);
    renderPage();

    fireEvent.click(screen.getByTestId('severity-chip-blocker'));

    expect(screen.getByText('Critical deploy failure')).toBeInTheDocument();
    expect(screen.queryByText('Flaky test detected')).not.toBeInTheDocument();
    expect(screen.queryByText('Docs need update')).not.toBeInTheDocument();
  });

  test('clicking warn chip shows only warn items', () => {
    setupMocks([BLOCKER_ITEM, WARN_ITEM, INFO_ITEM]);
    renderPage();

    fireEvent.click(screen.getByTestId('severity-chip-warn'));

    expect(screen.getByText('Flaky test detected')).toBeInTheDocument();
    expect(screen.queryByText('Critical deploy failure')).not.toBeInTheDocument();
    expect(screen.queryByText('Docs need update')).not.toBeInTheDocument();
  });

  test('clicking info chip shows only info items', () => {
    setupMocks([BLOCKER_ITEM, WARN_ITEM, INFO_ITEM]);
    renderPage();

    fireEvent.click(screen.getByTestId('severity-chip-info'));

    expect(screen.getByText('Docs need update')).toBeInTheDocument();
    expect(screen.queryByText('Critical deploy failure')).not.toBeInTheDocument();
    expect(screen.queryByText('Flaky test detected')).not.toBeInTheDocument();
  });

  test('clicking active chip again deactivates filter and shows all items', () => {
    setupMocks([BLOCKER_ITEM, WARN_ITEM, INFO_ITEM]);
    renderPage();

    const chip = screen.getByTestId('severity-chip-blocker');
    fireEvent.click(chip);   // activate
    fireEvent.click(chip);   // deactivate

    // All items visible again
    expect(screen.getByText('Critical deploy failure')).toBeInTheDocument();
    expect(screen.getByText('Flaky test detected')).toBeInTheDocument();
    expect(screen.getByText('Docs need update')).toBeInTheDocument();
  });

  test('"Clear" button appears when severity filter is active', () => {
    setupMocks([BLOCKER_ITEM]);
    renderPage();

    expect(screen.queryByTestId('severity-clear')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('severity-chip-blocker'));
    expect(screen.getByTestId('severity-clear')).toBeInTheDocument();
  });

  test('"Clear" button resets severity filter', () => {
    setupMocks([BLOCKER_ITEM, INFO_ITEM]);
    renderPage();

    fireEvent.click(screen.getByTestId('severity-chip-blocker'));
    // Only blocker visible
    expect(screen.queryByText('Docs need update')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('severity-clear'));
    // All items back
    expect(screen.getByText('Docs need update')).toBeInTheDocument();
    expect(screen.getByText('Critical deploy failure')).toBeInTheDocument();
  });

  test('severity chip has aria-pressed=true when active', () => {
    setupMocks([BLOCKER_ITEM]);
    renderPage();

    const chip = screen.getByTestId('severity-chip-blocker');
    expect(chip).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(chip);
    expect(chip).toHaveAttribute('aria-pressed', 'true');
  });
});

// ============================================================
// Empty state tests
// ============================================================

describe('OpenItemsPage — empty state', () => {
  test('shows empty state when all items are filtered out by severity', () => {
    // Only warn items, but user selects blocker chip
    setupMocks([WARN_ITEM]);
    renderPage();

    fireEvent.click(screen.getByTestId('severity-chip-blocker'));

    // No items match → empty state
    const emptyText = screen.getByText(/No blockers/i);
    expect(emptyText).toBeInTheDocument();
  });

  test('empty state message reflects active severity filter', () => {
    setupMocks([BLOCKER_ITEM]);
    renderPage();

    fireEvent.click(screen.getByTestId('severity-chip-warn'));

    expect(screen.getByText(/No warnings/i)).toBeInTheDocument();
  });

  test('shows no-items message when data has zero items (no filter)', () => {
    setupMocks([]);
    renderPage();
    expect(screen.getByText(/No open items across any registered project/i)).toBeInTheDocument();
  });

  test('shows project-scoped empty message when project selected and zero items', () => {
    setupMocks([], ['myproject']);
    renderPage();

    fireEvent.change(screen.getByTestId('project-dropdown'), { target: { value: 'myproject' } });

    // hook re-invoked with 'myproject'; mock still returns empty
    expect(screen.getByText(/No open items for myproject/i)).toBeInTheDocument();
  });
});

// ============================================================
// Filter interaction tests
// ============================================================

describe('OpenItemsPage — filter interactions', () => {
  test('changing project dropdown resets severity filter', () => {
    setupMocks([BLOCKER_ITEM, INFO_ITEM], ['alpha']);
    renderPage();

    // Activate severity filter
    fireEvent.click(screen.getByTestId('severity-chip-blocker'));
    // Blocker chip active — info hidden
    expect(screen.queryByText('Docs need update')).not.toBeInTheDocument();

    // Change project → severity filter should reset
    fireEvent.change(screen.getByTestId('project-dropdown'), { target: { value: 'alpha' } });
    // clear button should be gone
    expect(screen.queryByTestId('severity-clear')).not.toBeInTheDocument();
  });

  test('all items visible with no active filters', () => {
    setupMocks([BLOCKER_ITEM, WARN_ITEM, INFO_ITEM]);
    renderPage();

    expect(screen.getByText('Critical deploy failure')).toBeInTheDocument();
    expect(screen.getByText('Flaky test detected')).toBeInTheDocument();
    expect(screen.getByText('Docs need update')).toBeInTheDocument();
  });
});
