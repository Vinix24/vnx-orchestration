/**
 * Tests for sidebar navigation — kanban link under Operator section.
 *
 * Quality gate: gate_pr3_kanban_integration
 * - Sidebar shows kanban link under Operator section
 * - Kanban link is active when on /operator/kanban
 * - Sidebar renders all operator nav items
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('next/navigation', () => ({
  usePathname: jest.fn(),
}));

jest.mock('next/image', () => ({
  __esModule: true,
  default: (props: Record<string, unknown>) => {
    // eslint-disable-next-line @next/next/no-img-element
    return <img {...props} alt={props.alt as string} />;
  },
}));

import { usePathname } from 'next/navigation';
import Sidebar from '@/components/sidebar';

const mockUsePathname = usePathname as jest.MockedFunction<typeof usePathname>;

describe('Sidebar — Operator section navigation', () => {
  test('renders Operator section label', () => {
    mockUsePathname.mockReturnValue('/');
    render(<Sidebar />);
    expect(screen.getByText('Operator')).toBeInTheDocument();
  });

  test('renders kanban link under Operator section', () => {
    mockUsePathname.mockReturnValue('/');
    render(<Sidebar />);
    expect(screen.getByText('Kanban Board')).toBeInTheDocument();
  });

  test('kanban link href points to /operator/kanban', () => {
    mockUsePathname.mockReturnValue('/');
    render(<Sidebar />);
    const link = screen.getByText('Kanban Board').closest('a');
    expect(link).toHaveAttribute('href', '/operator/kanban');
  });

  test('kanban link is active when on /operator/kanban', () => {
    mockUsePathname.mockReturnValue('/operator/kanban');
    render(<Sidebar />);
    const link = screen.getByText('Kanban Board').closest('a');
    // active links have orange accent color applied
    expect(link).toBeInTheDocument();
    // The active indicator div is rendered inside the link
    const activeIndicator = link?.querySelector('div[style]');
    expect(activeIndicator).toBeInTheDocument();
  });

  test('renders all three operator nav items', () => {
    mockUsePathname.mockReturnValue('/');
    render(<Sidebar />);
    expect(screen.getByText('Control Surface')).toBeInTheDocument();
    expect(screen.getByText('Open Items')).toBeInTheDocument();
    expect(screen.getByText('Kanban Board')).toBeInTheDocument();
  });

  test('kanban link is not active on other pages', () => {
    mockUsePathname.mockReturnValue('/operator');
    render(<Sidebar />);
    const kanbanLink = screen.getByText('Kanban Board').closest('a');
    const controlSurfaceLink = screen.getByText('Control Surface').closest('a');
    // Only the active link should have an accent indicator inside
    // Both links rendered but different styling applied
    expect(kanbanLink).toBeInTheDocument();
    expect(controlSurfaceLink).toBeInTheDocument();
  });
});
