/**
 * Tests for components/operator/event-timeline.tsx — execution replay viewer.
 *
 * Quality gate: f59-pr4-dashboard-viewer
 * - Renders empty state when no events present
 * - Renders tool_use events with phase label and timestamp offset
 * - Phase filter chips narrow the visible events
 * - Replay scrubber advances the active cursor
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

import EventTimeline from '@/components/operator/event-timeline';
import type { DispatchEvent } from '@/lib/types';

function explore(summary: string, offset = 0): DispatchEvent {
  return {
    type: 'tool_use',
    timestamp_offset: offset,
    tool_name: 'Read',
    file_path: summary,
    summary,
  };
}

function write(summary: string, offset = 0): DispatchEvent {
  return {
    type: 'tool_use',
    timestamp_offset: offset,
    tool_name: 'Write',
    file_path: summary,
    summary,
  };
}

function commit(offset = 0): DispatchEvent {
  return {
    type: 'tool_use',
    timestamp_offset: offset,
    tool_name: 'Bash',
    file_path: '',
    summary: 'git commit -m "feat: x"',
  };
}

describe('EventTimeline', () => {
  test('renders empty state when events array is empty', () => {
    render(<EventTimeline events={[]} />);
    expect(screen.getByText(/No tool events recorded/i)).toBeInTheDocument();
  });

  test('renders tool_use events with tool name and summary', () => {
    const events: DispatchEvent[] = [
      { type: 'phase_marker', phase: 'explore' },
      explore('src/lib/api.ts', 0),
      { type: 'phase_marker', phase: 'implement' },
      write('src/lib/new.ts', 12.5),
    ];
    render(<EventTimeline events={events} />);
    expect(screen.getByTestId('event-timeline')).toBeInTheDocument();
    expect(screen.getAllByText('Read').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Write').length).toBeGreaterThan(0);
    expect(screen.getByText('src/lib/api.ts')).toBeInTheDocument();
    expect(screen.getByText('src/lib/new.ts')).toBeInTheDocument();
  });

  test('phase filter narrows events to the selected phase', () => {
    const events: DispatchEvent[] = [
      { type: 'phase_marker', phase: 'explore' },
      explore('file-a', 0),
      { type: 'phase_marker', phase: 'implement' },
      write('file-b', 5),
      { type: 'phase_marker', phase: 'commit' },
      commit(10),
    ];
    render(<EventTimeline events={events} />);

    fireEvent.click(screen.getByTestId('phase-filter-commit'));

    expect(screen.queryByText('file-a')).not.toBeInTheDocument();
    expect(screen.queryByText('file-b')).not.toBeInTheDocument();
    expect(screen.getByText('git commit -m "feat: x"')).toBeInTheDocument();
  });

  test('replay step-forward button advances the cursor', () => {
    const events: DispatchEvent[] = [
      { type: 'phase_marker', phase: 'explore' },
      explore('a', 0),
      explore('b', 1),
      explore('c', 2),
    ];
    render(<EventTimeline events={events} />);

    // Initially cursor = null → label reads "0 / 3"
    expect(screen.getByText(/0 \/ 3/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('replay-forward'));
    expect(screen.getByText(/1 \/ 3/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('replay-forward'));
    expect(screen.getByText(/2 \/ 3/)).toBeInTheDocument();

    const active = screen.getByTestId('event-1');
    expect(active).toHaveAttribute('data-active', 'true');
  });

  test('replay reset clears the cursor back to pre-start', () => {
    const events: DispatchEvent[] = [
      { type: 'phase_marker', phase: 'explore' },
      explore('a', 0),
      explore('b', 1),
    ];
    render(<EventTimeline events={events} />);

    fireEvent.click(screen.getByTestId('replay-forward'));
    fireEvent.click(screen.getByTestId('replay-forward'));
    expect(screen.getByText(/2 \/ 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('replay-reset'));
    expect(screen.getByText(/0 \/ 2/)).toBeInTheDocument();
  });
});
