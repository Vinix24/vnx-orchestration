'use client';

import Link from 'next/link';
import { ChevronRight, FileText, CheckCircle2, Clock, AlertCircle } from 'lucide-react';
import type { DispatchSummary } from '@/lib/types';
import DispatchStageBadge from './dispatch-stage-badge';

interface Props {
  dispatches: DispatchSummary[];
}

function ReceiptDot({ status }: { status: string | null }) {
  if (!status) {
    return (
      <span title="No receipt" aria-label="No receipt">
        <Clock size={12} style={{ color: 'var(--color-muted)', opacity: 0.5 }} />
      </span>
    );
  }
  const ok =
    status === 'success' ||
    status === 'completed' ||
    status === 'passed' ||
    status === 'pass';
  const Icon = ok ? CheckCircle2 : AlertCircle;
  const color = ok ? '#22c55e' : '#f97316';
  return (
    <span title={`Receipt: ${status}`} aria-label={`Receipt: ${status}`}>
      <Icon size={12} style={{ color }} />
    </span>
  );
}

function trackColor(track: string): string {
  if (track === 'A') return '#22d3ee';
  if (track === 'B') return '#a855f7';
  if (track === 'C') return '#f97316';
  return 'var(--color-muted)';
}

export default function DispatchList({ dispatches }: Props) {
  if (dispatches.length === 0) {
    return (
      <div
        style={{
          padding: '40px 20px',
          textAlign: 'center',
          color: 'var(--color-muted)',
          fontSize: 13,
          background: 'rgba(255,255,255,0.02)',
          borderRadius: 12,
          border: '1px dashed rgba(255,255,255,0.08)',
        }}
      >
        No dispatches match the current filters.
      </div>
    );
  }

  return (
    <div
      role="table"
      aria-label="Dispatches"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
      }}
    >
      <div
        role="row"
        style={{
          display: 'grid',
          gridTemplateColumns: '90px 1fr 60px 90px 120px 80px 24px',
          gap: 12,
          padding: '8px 14px',
          fontSize: 10,
          fontWeight: 700,
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          color: 'var(--color-muted)',
          borderBottom: '1px solid rgba(255,255,255,0.05)',
        }}
      >
        <span role="columnheader">Stage</span>
        <span role="columnheader">Dispatch</span>
        <span role="columnheader">Track</span>
        <span role="columnheader">Terminal</span>
        <span role="columnheader">Role</span>
        <span role="columnheader">Age</span>
        <span role="columnheader" aria-label="Receipt" />
      </div>
      {dispatches.map(d => (
        <Link
          key={`${d.id}-${d.stage}`}
          href={`/operator/dispatches/${encodeURIComponent(d.id)}`}
          role="row"
          data-testid={`dispatch-row-${d.id}`}
          style={{
            display: 'grid',
            gridTemplateColumns: '90px 1fr 60px 90px 120px 80px 24px',
            gap: 12,
            alignItems: 'center',
            padding: '10px 14px',
            borderRadius: 8,
            background: 'rgba(255,255,255,0.02)',
            border: '1px solid rgba(255,255,255,0.04)',
            textDecoration: 'none',
            color: 'var(--color-foreground)',
            transition: 'all 0.15s',
          }}
          className="dispatch-row"
        >
          <DispatchStageBadge stage={d.stage} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
            <FileText size={12} style={{ color: 'var(--color-muted)', flexShrink: 0 }} />
            <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
              <span
                style={{
                  fontSize: 12,
                  fontFamily: 'var(--font-mono, monospace)',
                  color: 'var(--color-foreground)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {d.id}
              </span>
              {d.gate && d.gate !== '—' && (
                <span style={{ fontSize: 10, color: 'var(--color-muted)', marginTop: 1 }}>
                  {d.gate}
                </span>
              )}
            </div>
          </div>
          <span
            style={{
              fontSize: 11,
              fontWeight: 700,
              color: trackColor(d.track),
            }}
          >
            {d.track}
          </span>
          <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>{d.terminal}</span>
          <span
            style={{
              fontSize: 11,
              color: 'var(--color-muted)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {d.role}
          </span>
          <span style={{ fontSize: 10, color: 'var(--color-muted)' }}>{d.duration_label}</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <ReceiptDot status={d.receipt_status} />
            <ChevronRight size={13} style={{ color: 'var(--color-muted)', opacity: 0.5 }} />
          </div>
        </Link>
      ))}
      <style jsx>{`
        :global(.dispatch-row:hover) {
          background: rgba(255,255,255,0.05) !important;
          border-color: rgba(249, 115, 22, 0.3) !important;
        }
      `}</style>
    </div>
  );
}
