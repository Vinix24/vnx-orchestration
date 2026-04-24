/**
 * Tests for app/operator/dispatches/page.tsx — dispatch viewer list page.
 *
 * Quality gate: f59-pr4-dashboard-viewer
 * - List page renders all dispatches across stages with counts
 * - Stage tabs filter the visible dispatches
 * - Search box filters by id, gate, PR id, role
 * - Track and terminal filters narrow the result set
 * - Empty, loading, and error states render correctly
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('next/navigation', () => ({
  usePathname: () => '/operator/dispatches',
}));

jest.mock('@/lib/hooks', () => ({
  useDispatches: jest.fn(),
}));

import { useDispatches } from '@/lib/hooks';
import DispatchesPage from '@/app/operator/dispatches/page';
import type { DispatchesResponse, DispatchSummary, DispatchStage } from '@/lib/types';

const mockUseDispatches = useDispatches as jest.MockedFunction<typeof useDispatches>;

function card(id: string, stage: DispatchStage, overrides: Partial<DispatchSummary> = {}): DispatchSummary {
  return {
    id,
    file: `${id}.md`,
    pr_id: 'PR-42',
    track: 'A',
    terminal: 'T1',
    role: 'backend-developer',
    gate: 'gate_x',
    priority: 'P1',
    status: 'active',
    reason: '—',
    domain: 'coding',
    dir: stage,
    stage,
    duration_secs: 120,
    duration_label: '2m',
    has_receipt: false,
    receipt_status: null,
    ...overrides,
  };
}

function envelope(cards: DispatchSummary[]): DispatchesResponse {
  const stages: Record<DispatchStage, DispatchSummary[]> = {
    staging: [],
    pending: [],
    active: [],
    review: [],
    done: [],
  };
  for (const c of cards) stages[c.stage].push(c);
  return { stages, total: cards.length };
}

function mockResponse(data: DispatchesResponse | undefined, opts: { isLoading?: boolean; error?: unknown } = {}) {
  mockUseDispatches.mockReturnValue({
    data,
    error: opts.error,
    isLoading: opts.isLoading ?? false,
    isValidating: false,
    mutate: jest.fn(),
  } as ReturnType<typeof useDispatches>);
}

describe('DispatchesPage', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders loading skeleton when isLoading is true', () => {
    mockResponse(undefined, { isLoading: true });
    render(<DispatchesPage />);
    expect(screen.getByText(/Loading dispatches/i)).toBeInTheDocument();
  });

  test('renders error message when fetch fails', () => {
    mockResponse(undefined, { error: new Error('network down') });
    render(<DispatchesPage />);
    expect(screen.getByText(/Failed to load dispatches/i)).toBeInTheDocument();
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });

  test('renders empty state when no dispatches match filters', () => {
    mockResponse(envelope([]));
    render(<DispatchesPage />);
    expect(screen.getByText(/No dispatches match/i)).toBeInTheDocument();
  });

  test('renders dispatches with per-stage counts in the tab chips', () => {
    const data = envelope([
      card('d-1', 'pending'),
      card('d-2', 'active'),
      card('d-3', 'review'),
      card('d-4', 'done'),
    ]);
    mockResponse(data);
    render(<DispatchesPage />);

    // All tab shows 4
    const allTab = screen.getByTestId('stage-tab-all');
    expect(allTab).toHaveTextContent(/All/);
    expect(allTab).toHaveTextContent('4');

    // Each dispatch row is present
    expect(screen.getByTestId('dispatch-row-d-1')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-row-d-2')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-row-d-3')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-row-d-4')).toBeInTheDocument();
  });

  test('clicking a stage tab filters the list to dispatches in that stage', () => {
    const data = envelope([
      card('p-1', 'pending'),
      card('a-1', 'active'),
      card('a-2', 'active'),
    ]);
    mockResponse(data);
    render(<DispatchesPage />);

    fireEvent.click(screen.getByTestId('stage-tab-active'));

    expect(screen.queryByTestId('dispatch-row-p-1')).not.toBeInTheDocument();
    expect(screen.getByTestId('dispatch-row-a-1')).toBeInTheDocument();
    expect(screen.getByTestId('dispatch-row-a-2')).toBeInTheDocument();
  });

  test('search box filters rows by dispatch id substring', () => {
    const data = envelope([
      card('alpha-001', 'pending'),
      card('beta-002', 'pending'),
    ]);
    mockResponse(data);
    render(<DispatchesPage />);

    fireEvent.change(screen.getByTestId('dispatches-search'), {
      target: { value: 'alpha' },
    });

    expect(screen.getByTestId('dispatch-row-alpha-001')).toBeInTheDocument();
    expect(screen.queryByTestId('dispatch-row-beta-002')).not.toBeInTheDocument();
  });

  test('track filter limits the list to the selected track', () => {
    const data = envelope([
      card('a-1', 'pending', { track: 'A' }),
      card('c-1', 'pending', { track: 'C' }),
    ]);
    mockResponse(data);
    render(<DispatchesPage />);

    fireEvent.change(screen.getByTestId('track-filter'), {
      target: { value: 'C' },
    });

    expect(screen.queryByTestId('dispatch-row-a-1')).not.toBeInTheDocument();
    expect(screen.getByTestId('dispatch-row-c-1')).toBeInTheDocument();
  });

  test('terminal filter limits the list to the selected terminal', () => {
    const data = envelope([
      card('t1-1', 'pending', { terminal: 'T1' }),
      card('t3-1', 'pending', { terminal: 'T3' }),
    ]);
    mockResponse(data);
    render(<DispatchesPage />);

    fireEvent.change(screen.getByTestId('terminal-filter'), {
      target: { value: 'T3' },
    });

    expect(screen.queryByTestId('dispatch-row-t1-1')).not.toBeInTheDocument();
    expect(screen.getByTestId('dispatch-row-t3-1')).toBeInTheDocument();
  });

  test('receipt_status="pass" is classified as a successful receipt', () => {
    // Governance emits receipt_status="pass" for gates that passed; the
    // list view must render a green success dot, not an orange alert.
    const data = envelope([
      card('pass-1', 'done', { has_receipt: true, receipt_status: 'pass' }),
    ]);
    mockResponse(data);
    render(<DispatchesPage />);

    const row = screen.getByTestId('dispatch-row-pass-1');
    const dot = row.querySelector('[aria-label="Receipt: pass"]');
    expect(dot).not.toBeNull();
    // lucide renders inline SVG; the success icon (CheckCircle2) carries
    // the distinctive green color set inline — jsdom normalizes the hex
    // (#22c55e) into its rgb() equivalent.
    const svg = dot!.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute('style')).toMatch(/rgb\(34,\s*197,\s*94\)|#22c55e/);
  });

  test('dispatch row links to the detail page with encoded id', () => {
    const id = '20260424-020100-f59-pr4-dashboard-viewer-C';
    const data = envelope([card(id, 'active')]);
    mockResponse(data);
    render(<DispatchesPage />);

    const row = screen.getByTestId(`dispatch-row-${id}`);
    expect(row).toHaveAttribute(
      'href',
      `/operator/dispatches/${encodeURIComponent(id)}`,
    );
  });
});
