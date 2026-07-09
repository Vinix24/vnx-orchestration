/**
 * Tests for the self-learning proposals section on app/operator/improvements/page.tsx.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({
  useLearningProposals: jest.fn(),
  useProposals: jest.fn(),
  useConfidenceTrends: jest.fn(),
  useWeeklyDigest: jest.fn(),
}));

import { useLearningProposals, useProposals, useConfidenceTrends, useWeeklyDigest } from '@/lib/hooks';
import ImprovementsPage from '@/app/operator/improvements/page';
import type { LearningProposalsResponse, ProposalsResponse, ConfidenceTrendsResponse } from '@/lib/types';

const mockUseLearning = useLearningProposals as jest.MockedFunction<typeof useLearningProposals>;
const mockUseProposals = useProposals as jest.MockedFunction<typeof useProposals>;
const mockUseTrends = useConfidenceTrends as jest.MockedFunction<typeof useConfidenceTrends>;
const mockUseDigest = useWeeklyDigest as jest.MockedFunction<typeof useWeeklyDigest>;

function learningEnvelope(proposals: LearningProposalsResponse['proposals'] = []): LearningProposalsResponse {
  return { proposals };
}

function emptyProposals(): ProposalsResponse {
  return { proposals: [] };
}

function emptyTrends(): ConfidenceTrendsResponse {
  return { trends: [] };
}

function renderWith(
  learning: LearningProposalsResponse | undefined,
  opts: { isLoading?: boolean; error?: Error } = {}
) {
  mockUseLearning.mockReturnValue({
    data: learning,
    isLoading: opts.isLoading ?? false,
    error: opts.error,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useLearningProposals>);
  mockUseProposals.mockReturnValue({
    data: emptyProposals(),
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useProposals>);
  mockUseTrends.mockReturnValue({
    data: emptyTrends(),
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useConfidenceTrends>);
  mockUseDigest.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: undefined,
    mutate: jest.fn(),
    isValidating: false,
  } as ReturnType<typeof useWeeklyDigest>);
  return render(<ImprovementsPage />);
}

describe('LearningProposalsSection', () => {
  test('renders empty state when no proposals', () => {
    renderWith(learningEnvelope());
    expect(screen.getAllByText(/Self-Learning Proposals/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/No pending self-learning proposals/i).length).toBeGreaterThanOrEqual(1);
  });

  test('renders skill refinement, rule, and archival proposals', () => {
    renderWith(learningEnvelope([
      {
        id: 'skillref-debugger-20260709',
        type: 'skill_refinement',
        target: '.claude/skills/debugger/SKILL.md',
        summary: 'Rework rate is high.',
        rationale: 'Add rework checklist.',
        confidence: 0.45,
        created_at: '2026-07-09T10:00:00Z',
      },
      {
        id: 'rule-abc',
        type: 'rule',
        target: 'T1',
        summary: 'Agent not found error.',
        rationale: 'Validate agent exists.',
        confidence: 0.6,
        created_at: '2026-07-09T09:00:00Z',
      },
      {
        id: 'pattern-1',
        type: 'archival',
        target: 'Old auth helper',
        summary: 'Unused for 30+ days.',
        rationale: 'archive: Unused for 30+ days.',
        confidence: 0.15,
        created_at: '2026-07-09T08:00:00Z',
      },
    ]));

    expect(screen.getByText('.claude/skills/debugger/SKILL.md')).toBeInTheDocument();
    expect(screen.getByText('Skill refinement')).toBeInTheDocument();
    expect(screen.getByText('T1')).toBeInTheDocument();
    expect(screen.getByText('Prevention rule')).toBeInTheDocument();
    expect(screen.getByText('Old auth helper')).toBeInTheDocument();
    expect(screen.getByText('Archival / supersede')).toBeInTheDocument();
    expect(screen.getAllByText(/vnx learning skill-review/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/vnx learning review/i).length).toBeGreaterThanOrEqual(1);
  });

  test('renders loading state', () => {
    const { container } = renderWith(undefined, { isLoading: true });
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
  });

  test('renders error state', () => {
    renderWith(undefined, { error: new Error('boom') });
    expect(screen.getAllByText(/Failed to load self-learning proposals/i).length).toBeGreaterThanOrEqual(1);
  });
});
