/**
 * Tests for app/operator/dispatches/[id]/page.tsx — dispatch detail page.
 *
 * Quality gate: f59-pr4-dashboard-viewer (codex follow-up)
 * - Malformed URL-encoded id does not crash the page with URIError;
 *   an inline error state is rendered instead.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

// Synchronously unwrap the params promise so the component renders without
// needing a Suspense boundary. React 19's `use(promise)` requires a tracked
// thenable; in unit tests we just hand back the resolved value directly.
jest.mock('react', () => {
  const actual = jest.requireActual('react');
  return {
    ...actual,
    use: (value: unknown) => {
      if (value && typeof (value as { __resolved?: unknown }).__resolved !== 'undefined') {
        return (value as { __resolved: unknown }).__resolved;
      }
      return value;
    },
  };
});

jest.mock('next/link', () => ({
  __esModule: true,
  default: ({ children, ...rest }: React.PropsWithChildren<Record<string, unknown>>) => (
    <a {...(rest as Record<string, string>)}>{children}</a>
  ),
}));

jest.mock('@/lib/hooks', () => ({
  useDispatchDetail: jest.fn(),
  useDispatchEvents: jest.fn(),
  useDispatchResult: jest.fn(),
}));

import {
  useDispatchDetail,
  useDispatchEvents,
  useDispatchResult,
} from '@/lib/hooks';
import DispatchDetailPage from '@/app/operator/dispatches/[id]/page';

const mockDetail = useDispatchDetail as jest.MockedFunction<typeof useDispatchDetail>;
const mockEvents = useDispatchEvents as jest.MockedFunction<typeof useDispatchEvents>;
const mockResult = useDispatchResult as jest.MockedFunction<typeof useDispatchResult>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function idle<T extends (...a: any[]) => any>(): ReturnType<T> {
  return {
    data: undefined,
    error: undefined,
    isLoading: false,
    isValidating: false,
    mutate: jest.fn(),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

function paramsOf(id: string) {
  // Stub shape consumed by the mocked `use()` above.
  return { __resolved: { id } } as unknown as Promise<{ id: string }>;
}

describe('DispatchDetailPage', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockDetail.mockReturnValue(idle<typeof useDispatchDetail>());
    mockEvents.mockReturnValue(idle<typeof useDispatchEvents>());
    mockResult.mockReturnValue(idle<typeof useDispatchResult>());
  });

  test('malformed percent-encoded id renders an inline error, not a URIError crash', () => {
    // `%E0%A4%A` is an incomplete UTF-8 escape — decodeURIComponent throws URIError.
    expect(() => render(<DispatchDetailPage params={paramsOf('%E0%A4%A')} />)).not.toThrow();

    const err = screen.getByTestId('dispatch-detail-error');
    expect(err).toHaveTextContent(/Invalid dispatch id/i);
    // Tabs must NOT render when decode failed.
    expect(screen.queryByTestId('tab-overview')).not.toBeInTheDocument();
  });

  test('valid id renders the tab row and does not show the error state', () => {
    mockDetail.mockReturnValue({
      ...idle<typeof useDispatchDetail>(),
      data: {
        dispatch_id: '20260424-abc-C',
        stage: 'done',
        file: 'x.md',
        metadata: {},
        instruction: 'hello',
      },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any);

    render(<DispatchDetailPage params={paramsOf(encodeURIComponent('20260424-abc-C'))} />);

    expect(screen.queryByTestId('dispatch-detail-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('tab-overview')).toBeInTheDocument();
  });
});
