/**
 * Tests for app/operator/governance/page.tsx
 *
 * Quality gate: gate_pr3_governance_page
 * - Governance page renders recurrence table under test
 * - Recommendation cards show advisory-only badge under test
 * - Signal timeline chart renders under test
 * - Sidebar shows governance link
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

// Mock next/navigation used by sidebar / Link components
jest.mock('next/navigation', () => ({
  usePathname: () => '/operator/governance',
}));

// Mock next/image used by sidebar
jest.mock('next/image', () => ({
  __esModule: true,
  default: (props: Record<string, unknown>) => {
    // eslint-disable-next-line @next/next/no-img-element, jsx-a11y/alt-text
    return <img {...props} />;
  },
}));

// Mock recharts — avoid canvas/SVG errors in jsdom
jest.mock('recharts', () => {
  const React = require('react');
  return {
    BarChart: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="bar-chart">{children}</div>
    ),
    Bar: () => <div data-testid="bar" />,
    Cell: () => null,
    XAxis: () => null,
    YAxis: () => null,
    CartesianGrid: () => null,
    Tooltip: () => null,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
  };
});

jest.mock('@/lib/hooks', () => ({
  useGovernanceDigest: jest.fn(),
}));

import { useGovernanceDigest } from '@/lib/hooks';
import GovernancePage from '@/app/operator/governance/page';
import Sidebar from '@/components/sidebar';
import type { GovernanceDigestEnvelope, DigestRecurrenceRecord, DigestRecommendation } from '@/lib/types';

const mockUseGovernanceDigest = useGovernanceDigest as jest.MockedFunction<typeof useGovernanceDigest>;

// ---- Fixtures ----

const PATTERN_BLOCKER: DigestRecurrenceRecord = {
  defect_family: 'session_failed:timeout',
  count: 5,
  representative_content: 'session_failed: timeout after 300s',
  severity: 'blocker',
  signal_types: ['session_failure'],
  impacted_features: ['F17', 'F18'],
  impacted_prs: ['PR-2', 'PR-3'],
  impacted_sessions: ['sess-001', 'sess-002'],
  evidence_pointers: ['dispatch-abc', 'gate_pr2_session'],
  providers: ['gemini'],
};

const PATTERN_WARN: DigestRecurrenceRecord = {
  defect_family: 'gate_fail:missing_report',
  count: 3,
  representative_content: 'gate failed: report not found',
  severity: 'warn',
  signal_types: ['gate_failure'],
  impacted_features: ['F16'],
  impacted_prs: ['PR-1'],
  impacted_sessions: [],
  evidence_pointers: ['gate_pr1_lifecycle'],
  providers: [],
};

const REC_BLOCKER: DigestRecommendation = {
  category: 'operational_defect',
  content: 'Session timeout pattern is recurring — investigate provider timeout configuration.',
  advisory_only: true,
  evidence_basis: ['dispatch-abc', 'gate_pr2_session'],
  severity: 'blocker',
  recurrence_count: 5,
  defect_family: 'session_failed:timeout',
};

const REC_INFO: DigestRecommendation = {
  category: 'governance_health',
  content: 'Consider increasing report retention window for long-running sessions.',
  advisory_only: true,
  evidence_basis: ['gate_pr1_lifecycle'],
  severity: 'info',
  recurrence_count: 3,
  defect_family: 'gate_fail:missing_report',
};

function makeEnvelope(overrides: Partial<GovernanceDigestEnvelope> = {}): GovernanceDigestEnvelope {
  return {
    view: 'GovernanceDigestView',
    queried_at: new Date().toISOString(),
    source_freshness: { governance_digest: new Date().toISOString() },
    staleness_seconds: 30,
    degraded: false,
    degraded_reasons: [],
    data: {
      runner_version: '1.0',
      generated_at: new Date().toISOString(),
      total_signals_processed: 12,
      recurring_pattern_count: 2,
      single_occurrence_count: 4,
      recurring_patterns: [PATTERN_BLOCKER, PATTERN_WARN],
      recommendations: [REC_BLOCKER, REC_INFO],
      source_records: { gate_results: 8, queue_anomalies: 4 },
    },
    ...overrides,
  };
}

function setupHook(overrides: Partial<GovernanceDigestEnvelope> = {}) {
  mockUseGovernanceDigest.mockReturnValue({
    data: makeEnvelope(overrides),
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useGovernanceDigest>);
}

function setupLoading() {
  mockUseGovernanceDigest.mockReturnValue({
    data: undefined,
    isLoading: true,
    error: undefined,
    mutate: jest.fn(),
    isValidating: true,
  } as ReturnType<typeof useGovernanceDigest>);
}

function setupError() {
  mockUseGovernanceDigest.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: new Error('fetch failed'),
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useGovernanceDigest>);
}

function setupEmpty() {
  mockUseGovernanceDigest.mockReturnValue({
    data: makeEnvelope({
      data: {
        total_signals_processed: 0,
        recurring_patterns: [],
        recommendations: [],
        single_occurrence_count: 0,
      },
    }),
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useGovernanceDigest>);
}

// ============================================================
// Recurrence table tests
// ============================================================

describe('GovernancePage — recurrence table', () => {
  test('renders recurrence table', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('recurrence-table')).toBeInTheDocument();
  });

  test('renders a row for each recurring pattern', () => {
    setupHook();
    render(<GovernancePage />);
    const rows = screen.getAllByTestId('recurrence-row');
    expect(rows).toHaveLength(2);
  });

  test('recurrence row shows defect family name', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByText('session_failed:timeout')).toBeInTheDocument();
  });

  test('recurrence row shows count', () => {
    setupHook();
    render(<GovernancePage />);
    const counts = screen.getAllByTestId('recurrence-count');
    expect(counts[0]).toHaveTextContent('5');
  });

  test('recurrence row shows severity badge', () => {
    setupHook();
    render(<GovernancePage />);
    const badges = screen.getAllByTestId('severity-badge');
    const severities = badges.map(b => b.textContent);
    expect(severities).toContain('blocker');
    expect(severities).toContain('warn');
  });

  test('recurrence row shows impacted features', () => {
    setupHook();
    render(<GovernancePage />);
    const features = screen.getAllByTestId('recurrence-features');
    expect(features[0]).toHaveTextContent('F17');
  });

  test('recurrence row shows impacted PRs', () => {
    setupHook();
    render(<GovernancePage />);
    const prs = screen.getAllByTestId('recurrence-prs');
    expect(prs[0]).toHaveTextContent('PR-2');
  });

  test('shows "No recurring patterns" when empty', () => {
    setupEmpty();
    render(<GovernancePage />);
    expect(screen.getByText('No recurring patterns detected.')).toBeInTheDocument();
  });
});

// ============================================================
// Recommendation cards tests
// ============================================================

describe('GovernancePage — recommendation cards', () => {
  test('renders recommendations list', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('recommendations-list')).toBeInTheDocument();
  });

  test('renders a card for each recommendation', () => {
    setupHook();
    render(<GovernancePage />);
    const cards = screen.getAllByTestId('recommendation-card');
    expect(cards).toHaveLength(2);
  });

  test('recommendation card shows advisory-only badge', () => {
    setupHook();
    render(<GovernancePage />);
    const badges = screen.getAllByTestId('advisory-only-badge');
    // Both recommendations are advisory_only: true
    expect(badges.length).toBeGreaterThanOrEqual(1);
    expect(badges[0]).toHaveTextContent('advisory only');
  });

  test('recommendation card shows category badge', () => {
    setupHook();
    render(<GovernancePage />);
    const catBadges = screen.getAllByTestId('category-badge');
    expect(catBadges[0]).toHaveTextContent('operational defect');
  });

  test('recommendation card shows content text', () => {
    setupHook();
    render(<GovernancePage />);
    const contents = screen.getAllByTestId('recommendation-content');
    expect(contents[0]).toHaveTextContent(
      'Session timeout pattern is recurring',
    );
  });

  test('recommendation card shows evidence pointers', () => {
    setupHook();
    render(<GovernancePage />);
    const evidenceBlocks = screen.getAllByTestId('evidence-pointers');
    expect(evidenceBlocks.length).toBeGreaterThanOrEqual(1);
    expect(evidenceBlocks[0]).toHaveTextContent('dispatch-abc');
  });

  test('shows "No recommendations" when empty', () => {
    setupEmpty();
    render(<GovernancePage />);
    expect(screen.getByText('No recommendations at this time.')).toBeInTheDocument();
  });
});

// ============================================================
// Signal timeline chart tests
// ============================================================

describe('GovernancePage — signal timeline chart', () => {
  test('renders signal timeline chart section', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('signal-timeline-chart')).toBeInTheDocument();
  });

  test('renders recharts responsive container', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
  });

  test('renders bar chart element', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('bar-chart')).toBeInTheDocument();
  });

  test('shows "No signal data" when patterns are empty', () => {
    setupEmpty();
    render(<GovernancePage />);
    expect(screen.getByText('No signal data available.')).toBeInTheDocument();
  });
});

// ============================================================
// KPI strip tests
// ============================================================

describe('GovernancePage — KPI strip', () => {
  test('shows total signals count', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('kpi-total-signals')).toHaveTextContent('12');
  });

  test('shows recurring patterns count', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('kpi-recurring-patterns')).toHaveTextContent('2');
  });

  test('shows blocker recs count', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('kpi-blocker-recs')).toHaveTextContent('1');
  });

  test('shows single occurrences count', () => {
    setupHook();
    render(<GovernancePage />);
    expect(screen.getByTestId('kpi-single-occurrences')).toHaveTextContent('4');
  });
});

// ============================================================
// Loading and error states
// ============================================================

describe('GovernancePage — loading state', () => {
  test('renders page structure while loading', () => {
    setupLoading();
    render(<GovernancePage />);
    // Heading is always present
    expect(screen.getByText('Governance Digest')).toBeInTheDocument();
  });

  test('does not render recommendation list while loading', () => {
    setupLoading();
    render(<GovernancePage />);
    expect(screen.queryByTestId('recommendations-list')).not.toBeInTheDocument();
  });

  test('does not render recurrence rows while loading', () => {
    setupLoading();
    render(<GovernancePage />);
    expect(screen.queryAllByTestId('recurrence-row')).toHaveLength(0);
  });
});

describe('GovernancePage — error state', () => {
  test('shows degraded banner on fetch error', () => {
    setupError();
    render(<GovernancePage />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/Failed to load governance digest/)).toBeInTheDocument();
  });
});

describe('GovernancePage — degraded state', () => {
  test('shows degraded banner when envelope.degraded is true', () => {
    setupHook({
      degraded: true,
      degraded_reasons: ['governance_digest.json not found'],
    });
    render(<GovernancePage />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('governance_digest.json not found')).toBeInTheDocument();
  });
});

// ============================================================
// SWR hook usage
// ============================================================

describe('GovernancePage — hook usage', () => {
  test('useGovernanceDigest is called on mount', () => {
    setupHook();
    render(<GovernancePage />);
    expect(mockUseGovernanceDigest).toHaveBeenCalled();
  });
});

// ============================================================
// Sidebar — governance link
// ============================================================

describe('Sidebar — governance link', () => {
  test('sidebar shows governance link in Operator section', () => {
    render(<Sidebar />);
    const link = screen.getByText('Governance');
    expect(link).toBeInTheDocument();
  });

  test('governance link href points to /operator/governance', () => {
    render(<Sidebar />);
    const link = screen.getByText('Governance').closest('a');
    expect(link).toHaveAttribute('href', '/operator/governance');
  });

  test('governance link is active when pathname matches', () => {
    render(<Sidebar />);
    const link = screen.getByText('Governance').closest('a');
    // Pathname is mocked to '/operator/governance' at top of file
    expect(link).toHaveStyle('font-weight: 600');
  });
});
