'use client';

import React, { useState } from 'react';
import { useConfig, useConfigAudit } from '@/lib/hooks';
import { postConfigSet } from '@/lib/operator-api';
import type { ConfigEntryRow, ConfigSetRequest } from '@/lib/types';

const CATEGORY_ORDER = ['intelligence', 'dispatch', 'gate'];
const CATEGORY_LABEL: Record<string, string> = {
  intelligence: 'Intelligence',
  dispatch: 'Dispatch',
  gate: 'Gates',
};

const PANEL = 'linear-gradient(135deg, rgba(10,20,48,0.9) 0%, rgba(10,20,48,0.7) 100%)';

function isOn(row: ConfigEntryRow): boolean {
  return row.value === '1';
}

type Msg = { kind: 'ok' | 'err'; text: string } | null;

function ConfigRow({
  row,
  busy,
  edit,
  onEdit,
  onApply,
}: {
  row: ConfigEntryRow;
  busy: boolean;
  edit: string | undefined;
  onEdit: (v: string) => void;
  onApply: (value: string) => void;
}) {
  const locked = row.planned || !row.writable_from_ui;
  const current = row.value ?? '';
  return (
    <div
      data-testid={`config-row-${row.key}`}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 12,
        padding: '10px 12px',
        borderRadius: 8,
        background: PANEL,
        border: '1px solid rgba(255,255,255,0.08)',
      }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <code style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-foreground)' }}>{row.key}</code>
          {row.is_default ? (
            <span data-testid={`config-default-${row.key}`} style={{ fontSize: 9, color: 'var(--color-muted)', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 5, padding: '1px 5px' }}>
              default
            </span>
          ) : (
            <span data-testid={`config-changed-${row.key}`} style={{ fontSize: 9, color: 'var(--color-accent, #f97316)', border: '1px solid var(--color-accent, #f97316)', borderRadius: 5, padding: '1px 5px' }}>
              modified
            </span>
          )}
          {row.requires_approval && (
            <span style={{ fontSize: 9, color: 'var(--color-warning, #facc15)', border: '1px solid var(--color-warning, #facc15)', borderRadius: 5, padding: '1px 5px' }}>
              approval
            </span>
          )}
          {row.planned && (
            <span style={{ fontSize: 9, color: 'var(--color-muted)', border: '1px dashed rgba(255,255,255,0.2)', borderRadius: 5, padding: '1px 5px' }}>
              planned
            </span>
          )}
        </div>
        <span style={{ fontSize: 11, color: 'var(--color-muted)', overflow: 'hidden', textOverflow: 'ellipsis' }}>{row.description}</span>
      </div>

      <div style={{ flexShrink: 0 }}>
        {locked ? (
          <span data-testid={`config-locked-${row.key}`} style={{ fontSize: 11, color: 'var(--color-muted)' }}>
            {row.planned ? 'not yet available' : 'read-only'} · {current || '—'}
          </span>
        ) : row.type === 'bool' ? (
          <button
            data-testid={`config-toggle-${row.key}`}
            disabled={busy}
            onClick={() => onApply(isOn(row) ? '0' : '1')}
            style={{
              cursor: busy ? 'wait' : 'pointer',
              fontSize: 11,
              fontWeight: 700,
              padding: '4px 12px',
              borderRadius: 6,
              border: `1px solid ${isOn(row) ? 'var(--color-success, #50fa7b)' : 'rgba(255,255,255,0.2)'}`,
              color: isOn(row) ? 'var(--color-success, #50fa7b)' : 'var(--color-muted)',
              background: 'transparent',
            }}
          >
            {isOn(row) ? 'ON' : 'OFF'}
          </button>
        ) : (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input
              data-testid={`config-input-${row.key}`}
              value={edit ?? current}
              onChange={(e) => onEdit(e.target.value)}
              style={{ fontSize: 11, padding: '4px 8px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.2)', background: 'rgba(0,0,0,0.3)', color: 'var(--color-foreground)', width: 120 }}
            />
            <button
              data-testid={`config-save-${row.key}`}
              disabled={busy}
              onClick={() => onApply(edit ?? current)}
              style={{ cursor: busy ? 'wait' : 'pointer', fontSize: 11, fontWeight: 700, padding: '4px 10px', borderRadius: 6, border: '1px solid var(--color-accent, #f97316)', color: 'var(--color-accent, #f97316)', background: 'transparent' }}
            >
              Save
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default function ConfigPage() {
  const { data, isLoading, error, mutate } = useConfig();
  const auditQuery = useConfigAudit();
  const [pending, setPending] = useState<{ row: ConfigEntryRow; value: string } | null>(null);
  const [approvalId, setApprovalId] = useState('');
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const [auditOpen, setAuditOpen] = useState(false);
  const [edits, setEdits] = useState<Record<string, string>>({});

  async function submit(row: ConfigEntryRow, value: string, approval_id?: string) {
    setBusyKey(row.key);
    setMsg(null);
    try {
      const req: ConfigSetRequest = { key: row.key, value, ...(approval_id ? { approval_id } : {}) };
      const res = await postConfigSet(req);
      if (res.status === 'success') {
        setMsg({ kind: 'ok', text: `${row.key} → ${res.new_value}` });
        mutate();
        auditQuery.mutate();
      } else {
        setMsg({ kind: 'err', text: res.message || 'change rejected' });
      }
    } catch (e) {
      setMsg({ kind: 'err', text: e instanceof Error ? e.message : 'request failed' });
    } finally {
      setBusyKey(null);
    }
  }

  function onApply(row: ConfigEntryRow, value: string) {
    if (row.requires_approval) {
      setPending({ row, value });
      setApprovalId('');
    } else {
      submit(row, value);
    }
  }

  if (isLoading) {
    return <div data-testid="config-loading" style={{ padding: 24, color: 'var(--color-muted)' }}>Loading config…</div>;
  }
  if (error || !data) {
    return <div data-testid="config-error" style={{ padding: 24, color: 'var(--color-danger, #ff5555)' }}>Failed to load config.</div>;
  }

  const byCat: Record<string, ConfigEntryRow[]> = {};
  for (const r of data.config) (byCat[r.category] ??= []).push(r);
  const categories = [
    ...CATEGORY_ORDER.filter((c) => byCat[c]),
    ...Object.keys(byCat).filter((c) => !CATEGORY_ORDER.includes(c)),
  ];
  const auditRows = auditQuery.data?.audit ?? [];

  return (
    <div data-testid="config-page" style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>Config</h1>
        <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>{data.project_id}</span>
        <button
          data-testid="config-audit-toggle"
          onClick={() => setAuditOpen((v) => !v)}
          style={{ marginLeft: 'auto', cursor: 'pointer', fontSize: 11, fontWeight: 700, padding: '4px 12px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.2)', color: 'var(--color-foreground)', background: 'transparent' }}
        >
          {auditOpen ? 'Hide audit' : 'Audit log'}
        </button>
      </div>

      {msg && (
        <div
          data-testid="config-msg"
          style={{ fontSize: 12, color: msg.kind === 'ok' ? 'var(--color-success, #50fa7b)' : 'var(--color-danger, #ff5555)' }}
        >
          {msg.text}
        </div>
      )}

      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
          {categories.map((cat) => (
            <section key={cat} data-testid={`config-section-${cat}`} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-foreground)', borderBottom: '2px solid rgba(107,138,230,0.6)', paddingBottom: 6 }}>
                {CATEGORY_LABEL[cat] ?? cat} <span style={{ color: 'var(--color-muted)', fontWeight: 400 }}>({byCat[cat].length})</span>
              </div>
              {byCat[cat].map((row) => (
                <ConfigRow
                  key={row.key}
                  row={row}
                  busy={busyKey === row.key}
                  edit={edits[row.key]}
                  onEdit={(v) => setEdits((e) => ({ ...e, [row.key]: v }))}
                  onApply={(value) => onApply(row, value)}
                />
              ))}
            </section>
          ))}
        </div>

        {auditOpen && (
          <aside
            data-testid="config-audit-drawer"
            style={{ width: 320, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 8, padding: 12, borderRadius: 10, background: PANEL, border: '1px solid rgba(255,255,255,0.08)' }}
          >
            <div style={{ fontSize: 12, fontWeight: 700 }}>Recent changes</div>
            {auditRows.length === 0 ? (
              <div data-testid="config-audit-empty" style={{ fontSize: 11, color: 'var(--color-muted)' }}>No changes recorded.</div>
            ) : (
              auditRows.map((a) => (
                <div key={a.event_id} data-testid={`config-audit-row-${a.event_id}`} style={{ fontSize: 11, borderBottom: '1px solid rgba(255,255,255,0.06)', paddingBottom: 6 }}>
                  <code style={{ fontWeight: 700 }}>{a.config_key}</code>
                  <span style={{ color: 'var(--color-muted)' }}> {a.old_value ?? '∅'} → {a.new_value}</span>
                  <div style={{ color: 'var(--color-muted)' }}>{a.changed_by}{a.approval_id ? ` · ${a.approval_id}` : ''} · {a.changed_at}</div>
                </div>
              ))
            )}
          </aside>
        )}
      </div>

      {pending && (
        <div
          data-testid="config-approval-modal"
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}
        >
          <div style={{ width: 380, padding: 20, borderRadius: 12, background: 'rgba(10,20,48,0.98)', border: '1px solid rgba(255,255,255,0.12)', display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ fontSize: 14, fontWeight: 700 }}>Approval required</div>
            <div style={{ fontSize: 12, color: 'var(--color-muted)' }}>
              <code style={{ color: 'var(--color-foreground)' }}>{pending.row.key}</code> → <strong>{pending.value}</strong>. This flag changes governed behaviour; enter an approval reference to proceed.
            </div>
            <input
              data-testid="config-approval-input"
              value={approvalId}
              placeholder="approval id / reference"
              onChange={(e) => setApprovalId(e.target.value)}
              style={{ fontSize: 12, padding: '6px 10px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.2)', background: 'rgba(0,0,0,0.3)', color: 'var(--color-foreground)' }}
            />
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                data-testid="config-approval-cancel"
                onClick={() => setPending(null)}
                style={{ cursor: 'pointer', fontSize: 11, fontWeight: 700, padding: '6px 14px', borderRadius: 6, border: '1px solid rgba(255,255,255,0.2)', color: 'var(--color-muted)', background: 'transparent' }}
              >
                Cancel
              </button>
              <button
                data-testid="config-approval-confirm"
                disabled={!approvalId.trim()}
                onClick={() => {
                  const p = pending;
                  setPending(null);
                  submit(p.row, p.value, approvalId.trim());
                }}
                style={{ cursor: approvalId.trim() ? 'pointer' : 'not-allowed', fontSize: 11, fontWeight: 700, padding: '6px 14px', borderRadius: 6, border: '1px solid var(--color-warning, #facc15)', color: 'var(--color-warning, #facc15)', background: 'transparent', opacity: approvalId.trim() ? 1 : 0.5 }}
              >
                Confirm change
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
