/**
 * Tests for app/operator/config/page.tsx — the config control-plane UI over /api/operator/config.
 *
 * - Renders category sections + bool toggle / string input / locked (planned) rows
 * - A non-approval toggle posts the flipped value directly
 * - An approval-required toggle opens the modal; confirm posts with the approval_id
 * - Audit drawer toggles + lists recent changes
 * - Loading + error states
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';

jest.mock('@/lib/hooks', () => ({
  useConfig: jest.fn(),
  useConfigAudit: jest.fn(),
}));
jest.mock('@/lib/operator-api', () => ({
  postConfigSet: jest.fn(),
}));

import { useConfig, useConfigAudit } from '@/lib/hooks';
import { postConfigSet } from '@/lib/operator-api';
import ConfigPage from '@/app/operator/config/page';
import type { ConfigEntryRow, ConfigEnvelope, ConfigAuditEnvelope } from '@/lib/types';

const mockUseConfig = useConfig as jest.MockedFunction<typeof useConfig>;
const mockUseAudit = useConfigAudit as jest.MockedFunction<typeof useConfigAudit>;
const mockPost = postConfigSet as jest.MockedFunction<typeof postConfigSet>;

function entry(over: Partial<ConfigEntryRow>): ConfigEntryRow {
  return {
    key: 'VNX_X', type: 'bool', category: 'intelligence', description: 'desc',
    default: '0', value: '0', is_default: true, writable_from_ui: true,
    requires_approval: false, planned: false, ...over,
  };
}

const ROWS: ConfigEntryRow[] = [
  entry({ key: 'VNX_SCOUT_PREPASS', category: 'intelligence', description: 'scout pre-pass' }),
  entry({ key: 'VNX_TAGGER_PROVIDER', type: 'string', category: 'intelligence', default: 'deepseek', value: 'deepseek', description: 'tagger provider' }),
  entry({ key: 'VNX_USE_FEDERATION', category: 'intelligence', planned: true, writable_from_ui: false, description: 'federation' }),
  entry({ key: 'VNX_CI_GATE_REQUIRED', category: 'gate', requires_approval: true, description: 'require CI gate' }),
];

function configEnv(rows = ROWS): ConfigEnvelope {
  return { project_id: 'vnx-dev', config: rows, queried_at: '2026-06-27T15:00:00Z' };
}

function auditEnv(rows: ConfigAuditEnvelope['audit'] = []): ConfigAuditEnvelope {
  return { project_id: 'vnx-dev', audit: rows, queried_at: '2026-06-27T15:00:00Z' };
}

function setup(opts: {
  data?: ConfigEnvelope; isLoading?: boolean; error?: Error; audit?: ConfigAuditEnvelope;
} = {}) {
  mockUseConfig.mockReturnValue({
    data: opts.data, isLoading: opts.isLoading ?? false, error: opts.error,
    mutate: jest.fn(), isValidating: false,
  } as ReturnType<typeof useConfig>);
  mockUseAudit.mockReturnValue({
    data: opts.audit ?? auditEnv(), isLoading: false, error: undefined,
    mutate: jest.fn(), isValidating: false,
  } as ReturnType<typeof useConfigAudit>);
  return render(<ConfigPage />);
}

beforeEach(() => {
  jest.clearAllMocks();
  mockPost.mockResolvedValue({ status: 'success', new_value: '1', timestamp: 't' });
});

describe('ConfigPage', () => {
  test('renders category sections + the row controls', () => {
    setup({ data: configEnv() });
    expect(screen.getByTestId('config-section-intelligence')).toBeInTheDocument();
    expect(screen.getByTestId('config-section-gate')).toBeInTheDocument();
    expect(screen.getByTestId('config-toggle-VNX_SCOUT_PREPASS')).toHaveTextContent('OFF');
    expect(screen.getByTestId('config-input-VNX_TAGGER_PROVIDER')).toHaveValue('deepseek');
    expect(screen.getByTestId('config-locked-VNX_USE_FEDERATION')).toBeInTheDocument();
  });

  test('non-approval toggle posts the flipped value directly', async () => {
    setup({ data: configEnv() });
    fireEvent.click(screen.getByTestId('config-toggle-VNX_SCOUT_PREPASS'));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith({ key: 'VNX_SCOUT_PREPASS', value: '1' }));
    expect(screen.queryByTestId('config-approval-modal')).not.toBeInTheDocument();
  });

  test('approval-required toggle opens the modal and posts with approval_id on confirm', async () => {
    setup({ data: configEnv() });
    fireEvent.click(screen.getByTestId('config-toggle-VNX_CI_GATE_REQUIRED'));
    expect(screen.getByTestId('config-approval-modal')).toBeInTheDocument();
    // confirm is disabled until an approval id is entered
    expect(screen.getByTestId('config-approval-confirm')).toBeDisabled();
    fireEvent.change(screen.getByTestId('config-approval-input'), { target: { value: 'appr-7' } });
    fireEvent.click(screen.getByTestId('config-approval-confirm'));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith({ key: 'VNX_CI_GATE_REQUIRED', value: '1', approval_id: 'appr-7' }),
    );
  });

  test('approval modal cancel does not post', () => {
    setup({ data: configEnv() });
    fireEvent.click(screen.getByTestId('config-toggle-VNX_CI_GATE_REQUIRED'));
    fireEvent.click(screen.getByTestId('config-approval-cancel'));
    expect(screen.queryByTestId('config-approval-modal')).not.toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('string save posts the edited value', async () => {
    setup({ data: configEnv() });
    fireEvent.change(screen.getByTestId('config-input-VNX_TAGGER_PROVIDER'), { target: { value: 'kimi' } });
    fireEvent.click(screen.getByTestId('config-save-VNX_TAGGER_PROVIDER'));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith({ key: 'VNX_TAGGER_PROVIDER', value: 'kimi' }));
  });

  test('audit drawer toggles and lists changes', () => {
    setup({
      data: configEnv(),
      audit: auditEnv([
        { config_key: 'VNX_SCOUT_PREPASS', old_value: '0', new_value: '1', changed_by: 'op', changed_at: 't', approval_id: null, event_id: 'evt-1' },
      ]),
    });
    expect(screen.queryByTestId('config-audit-drawer')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('config-audit-toggle'));
    expect(screen.getByTestId('config-audit-drawer')).toBeInTheDocument();
    expect(screen.getByTestId('config-audit-row-evt-1')).toHaveTextContent('VNX_SCOUT_PREPASS');
  });

  test('loading and error states', () => {
    const { rerender } = setup({ isLoading: true });
    expect(screen.getByTestId('config-loading')).toBeInTheDocument();
    mockUseConfig.mockReturnValue({ data: undefined, isLoading: false, error: new Error('x'), mutate: jest.fn(), isValidating: false } as ReturnType<typeof useConfig>);
    rerender(<ConfigPage />);
    expect(screen.getByTestId('config-error')).toBeInTheDocument();
  });
});
