'use client';

import { useState } from 'react';
import { RefreshCw, ChevronDown, ChevronRight, FileText } from 'lucide-react';
import { useReports, useReportContent, useProjects } from '@/lib/hooks';
import AgentSelector from '@/components/operator/agent-selector';
import FreshnessBadge from '@/components/operator/freshness-badge';
import type { Report } from '@/lib/types';

const TRACK_COLORS: Record<string, { color: string; bg: string; border: string }> = {
  A: { color: '#50fa7b', bg: 'rgba(80, 250, 123, 0.1)', border: 'rgba(80, 250, 123, 0.3)' },
  B: { color: '#facc15', bg: 'rgba(250, 204, 21, 0.1)', border: 'rgba(250, 204, 21, 0.3)' },
  C: { color: '#9B6BE6', bg: 'rgba(155, 107, 230, 0.1)', border: 'rgba(155, 107, 230, 0.3)' },
};

const STATUS_COLORS: Record<string, { color: string; bg: string; border: string }> = {
  success: { color: '#50fa7b', bg: 'rgba(80, 250, 123, 0.08)', border: 'rgba(80, 250, 123, 0.25)' },
  failure: { color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.08)', border: 'rgba(255, 107, 107, 0.25)' },
  failed:  { color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.08)', border: 'rgba(255, 107, 107, 0.25)' },
};

function TrackBadge({ track }: { track: string }) {
  const cfg = TRACK_COLORS[track.toUpperCase()] ?? { color: '#6B6B6B', bg: 'rgba(107,107,107,0.1)', border: 'rgba(107,107,107,0.3)' };
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 700,
        padding: '2px 7px',
        borderRadius: 6,
        background: cfg.bg,
        border: `1px solid ${cfg.border}`,
        color: cfg.color,
        letterSpacing: '0.04em',
      }}
    >
      {track.toUpperCase()}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cfg = STATUS_COLORS[status.toLowerCase()] ?? { color: 'var(--color-muted)', bg: 'rgba(255,255,255,0.04)', border: 'rgba(255,255,255,0.1)' };
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 600,
        padding: '2px 8px',
        borderRadius: 6,
        background: cfg.bg,
        border: `1px solid ${cfg.border}`,
        color: cfg.color,
        textTransform: 'capitalize',
      }}
    >
      {status}
    </span>
  );
}

function formatTimestamp(ts: string): string {
  if (!ts) return '—';
  try {
    const d = new Date(ts.replace(/^(\d{8})-(\d{6})$/, '$1T$2'));
    if (isNaN(d.getTime())) {
      // Try yyyyMMdd-HHmmss format (e.g. 20260410-082231)
      const m = ts.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/);
      if (m) {
        const d2 = new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`);
        return d2.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      }
      return ts;
    }
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch {
    return ts;
  }
}

function LoadingSkeleton() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {[0, 1, 2, 3].map(i => (
        <div
          key={i}
          aria-hidden="true"
          style={{
            height: 56,
            borderRadius: 10,
            background: 'linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.07) 50%, rgba(255,255,255,0.04) 75%)',
            backgroundSize: '200% 100%',
            animation: 'shimmer 1.6s infinite',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
        />
      ))}
    </div>
  );
}

function ReportContentPanel({ filename }: { filename: string }) {
  const { data: content, isLoading, error } = useReportContent(filename);

  if (isLoading) {
    return (
      <div style={{ padding: '16px 20px', color: 'var(--color-muted)', fontSize: 12 }}>
        Loading…
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: '16px 20px', color: 'var(--color-error)', fontSize: 12 }}>
        Failed to load report content.
      </div>
    );
  }

  return (
    <div
      style={{
        borderTop: '1px solid rgba(255,255,255,0.06)',
        padding: '16px 20px',
        overflowX: 'auto',
      }}
    >
      <pre
        style={{
          margin: 0,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 12,
          lineHeight: 1.6,
          color: 'var(--color-foreground)',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {content}
      </pre>
    </div>
  );
}

function ReportRow({ report }: { report: Report }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      style={{
        borderRadius: 10,
        background: expanded ? 'rgba(249, 115, 22, 0.04)' : 'rgba(255,255,255,0.02)',
        border: `1px solid ${expanded ? 'rgba(249, 115, 22, 0.2)' : 'rgba(255,255,255,0.07)'}`,
        overflow: 'hidden',
        transition: 'border-color 0.15s',
      }}
    >
      {/* Row header */}
      <button
        onClick={() => setExpanded(v => !v)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '12px 16px',
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        {/* Expand chevron */}
        <div style={{ color: 'var(--color-muted)', flexShrink: 0 }}>
          {expanded
            ? <ChevronDown size={14} />
            : <ChevronRight size={14} />
          }
        </div>

        {/* Timestamp */}
        <span
          style={{
            fontSize: 11,
            color: 'var(--color-muted)',
            whiteSpace: 'nowrap',
            width: 120,
            flexShrink: 0,
          }}
        >
          {formatTimestamp(report.timestamp)}
        </span>

        {/* Track badge */}
        <TrackBadge track={report.track} />

        {/* Terminal */}
        <span
          style={{
            fontSize: 11,
            color: 'rgba(244,244,249,0.6)',
            width: 40,
            flexShrink: 0,
          }}
        >
          {report.terminal}
        </span>

        {/* Dispatch ID */}
        <span
          style={{
            fontSize: 10,
            color: 'var(--color-muted)',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            maxWidth: 200,
            flexShrink: 1,
          }}
          title={report.dispatch_id}
        >
          {report.dispatch_id || '—'}
        </span>

        {/* Status badge */}
        <StatusBadge status={report.status} />

        {/* Title */}
        <span
          style={{
            fontSize: 12,
            color: 'var(--color-foreground)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            flex: 1,
            minWidth: 0,
          }}
          title={report.title}
        >
          {report.title || report.filename}
        </span>
      </button>

      {/* Expanded markdown content */}
      {expanded && <ReportContentPanel filename={report.filename} />}
    </div>
  );
}

const PAGE_SIZE = 50;

export default function ReportsPage() {
  const [terminalFilter, setTerminalFilter] = useState<string | undefined>(undefined);
  const [trackFilter, setTrackFilter] = useState<string>('');
  const [offset, setOffset] = useState(0);

  const { data: reportsEnv, isLoading, mutate } = useReports({
    limit: PAGE_SIZE,
    offset,
    terminal: terminalFilter,
    track: trackFilter || undefined,
  });

  // Get projects for the freshness display
  const reports: Report[] = reportsEnv?.reports ?? [];
  const total = reportsEnv?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  function handleTerminalSelect(terminal: string | undefined) {
    setTerminalFilter(terminal);
    setOffset(0);
  }

  function handleTrackFilter(track: string) {
    setTrackFilter(track);
    setOffset(0);
  }

  return (
    <div>
      {/* Page header */}
      <div className="flex items-center justify-between" style={{ marginBottom: 24 }}>
        <div className="flex items-center gap-3">
          <div
            style={{
              width: 4,
              height: 28,
              borderRadius: 2,
              background: 'var(--color-accent)',
            }}
          />
          <div>
            <h2
              style={{
                fontSize: '1.5rem',
                fontWeight: 700,
                letterSpacing: '-0.02em',
                color: 'var(--color-foreground)',
              }}
            >
              Session History
            </h2>
            <p style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
              Worker reports and dispatch receipts
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {total > 0 && (
            <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
              {total} report{total !== 1 ? 's' : ''}
            </span>
          )}
          <button
            onClick={() => mutate()}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '7px 14px',
              borderRadius: 8,
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)',
              cursor: 'pointer',
              fontSize: 12,
              color: 'var(--color-muted)',
            }}
          >
            <RefreshCw size={13} />
            Refresh
          </button>
        </div>
      </div>

      {/* Filter row */}
      <div
        className="glass-card"
        style={{ padding: '16px 20px', marginBottom: 20 }}
      >
        {/* Agent selector */}
        <div style={{ marginBottom: 16 }}>
          <AgentSelector
            selectedTerminal={terminalFilter}
            onSelect={handleTerminalSelect}
          />
        </div>

        {/* Track filter */}
        <div className="flex items-center gap-3">
          <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>Track:</span>
          {['', 'A', 'B', 'C'].map(t => (
            <button
              key={t || 'all'}
              onClick={() => handleTrackFilter(t)}
              style={{
                padding: '4px 12px',
                borderRadius: 8,
                fontSize: 11,
                fontWeight: trackFilter === t ? 700 : 400,
                background: trackFilter === t
                  ? (t ? (TRACK_COLORS[t]?.bg ?? 'rgba(249,115,22,0.1)') : 'rgba(249,115,22,0.1)')
                  : 'rgba(255,255,255,0.03)',
                border: `1px solid ${trackFilter === t
                  ? (t ? (TRACK_COLORS[t]?.border ?? 'rgba(249,115,22,0.4)') : 'rgba(249,115,22,0.4)')
                  : 'rgba(255,255,255,0.08)'}`,
                color: trackFilter === t
                  ? (t ? (TRACK_COLORS[t]?.color ?? 'var(--color-accent)') : 'var(--color-accent)')
                  : 'var(--color-muted)',
                cursor: 'pointer',
              }}
            >
              {t || 'All'}
            </button>
          ))}
        </div>
      </div>

      {/* Reports list */}
      <div className="glass-card" style={{ padding: '16px 20px' }}>
        {isLoading ? (
          <LoadingSkeleton />
        ) : reports.length === 0 ? (
          <div
            className="flex items-center gap-3"
            style={{
              padding: '32px 16px',
              justifyContent: 'center',
              color: 'var(--color-muted)',
            }}
          >
            <FileText size={20} strokeWidth={1.5} />
            <span style={{ fontSize: 14 }}>
              No reports found
              {(terminalFilter || trackFilter) && ' for the selected filters'}
            </span>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {reports.map(r => (
              <ReportRow key={r.filename} report={r} />
            ))}
          </div>
        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div
            className="flex items-center gap-3"
            style={{
              marginTop: 16,
              paddingTop: 16,
              borderTop: '1px solid rgba(255,255,255,0.06)',
              justifyContent: 'center',
            }}
          >
            <button
              disabled={offset === 0}
              onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
              style={{
                padding: '6px 14px',
                borderRadius: 8,
                fontSize: 12,
                background: 'rgba(255,255,255,0.04)',
                border: '1px solid rgba(255,255,255,0.1)',
                color: offset === 0 ? 'rgba(244,244,249,0.3)' : 'var(--color-muted)',
                cursor: offset === 0 ? 'not-allowed' : 'pointer',
              }}
            >
              Previous
            </button>
            <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
              Page {currentPage} of {totalPages}
            </span>
            <button
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(o => o + PAGE_SIZE)}
              style={{
                padding: '6px 14px',
                borderRadius: 8,
                fontSize: 12,
                background: 'rgba(255,255,255,0.04)',
                border: '1px solid rgba(255,255,255,0.1)',
                color: offset + PAGE_SIZE >= total ? 'rgba(244,244,249,0.3)' : 'var(--color-muted)',
                cursor: offset + PAGE_SIZE >= total ? 'not-allowed' : 'pointer',
              }}
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
