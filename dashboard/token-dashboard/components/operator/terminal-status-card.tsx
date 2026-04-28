'use client';

import { Clock, AlertCircle, CheckCircle2, MinusCircle, XCircle } from 'lucide-react';
import type { TerminalEntry, HeartbeatClassification, TerminalStatus } from '@/lib/types';
import { TERMINAL_COLORS } from '@/lib/types';

interface Props {
  terminal: TerminalEntry;
}

const STATUS_CONFIG: Record<string, { label: string; color: string; Icon: React.ElementType }> = {
  active:   { label: 'Active',   color: 'var(--color-success)', Icon: CheckCircle2 },
  working:  { label: 'Working',  color: 'var(--color-success)', Icon: CheckCircle2 },
  blocked:  { label: 'Blocked',  color: 'var(--color-error)',   Icon: AlertCircle },
  stale:    { label: 'Stale',    color: 'var(--color-warning)', Icon: Clock },
  exited:   { label: 'Exited',   color: 'var(--color-muted)',   Icon: XCircle },
  idle:     { label: 'Idle',     color: 'var(--color-muted)',   Icon: MinusCircle },
  unknown:  { label: 'Unknown',  color: 'rgba(255,255,255,0.3)',Icon: MinusCircle },
};

const HB_CONFIG: Record<HeartbeatClassification, { label: string; color: string }> = {
  fresh:   { label: 'Heartbeat fresh',  color: 'var(--color-success)' },
  stale:   { label: 'Heartbeat stale',  color: 'var(--color-warning)' },
  dead:    { label: 'Heartbeat dead',   color: 'var(--color-error)' },
  missing: { label: 'No heartbeat',     color: 'rgba(255,255,255,0.3)' },
};

function fmtAge(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const secs = (Date.now() - new Date(iso).getTime()) / 1000;
    if (secs < 60) return `${Math.round(secs)}s ago`;
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    return `${Math.round(secs / 3600)}h ago`;
  } catch {
    return '—';
  }
}

export default function TerminalStatusCard({ terminal }: Props) {
  const status = (terminal.status || 'unknown') as TerminalStatus;
  const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.unknown;
  const { Icon } = cfg;
  const termColor = TERMINAL_COLORS[terminal.terminal_id] ?? TERMINAL_COLORS.unknown;
  const hbCfg = HB_CONFIG[terminal.heartbeat_classification] ?? HB_CONFIG.missing;

  const hasContextWarning = terminal.context_pressure?.warning;

  return (
    <div
      className="glass-card"
      style={{
        padding: '20px 22px',
        borderLeft: `3px solid ${termColor}80`,
        boxShadow: `0 4px 20px ${termColor}08`,
        position: 'relative',
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <div
            style={{
              width: 10,
              height: 10,
              borderRadius: '50%',
              backgroundColor: termColor,
              boxShadow: `0 0 8px ${termColor}60`,
            }}
          />
          <span className="text-sm font-bold" style={{ color: termColor, letterSpacing: '-0.01em' }}>
            {terminal.terminal_id}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <Icon size={14} style={{ color: cfg.color }} />
          <span style={{ fontSize: 12, fontWeight: 600, color: cfg.color }}>{cfg.label}</span>
        </div>
      </div>

      {/* Status rows */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {/* Heartbeat */}
        <div className="flex items-center justify-between">
          <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Heartbeat</span>
          <span style={{ fontSize: 11, color: hbCfg.color, fontWeight: 500 }}>{hbCfg.label}</span>
        </div>

        {/* Last output */}
        <div className="flex items-center justify-between">
          <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Last output</span>
          <span style={{ fontSize: 11, color: 'var(--color-foreground)', fontVariantNumeric: 'tabular-nums' }}>
            {fmtAge(terminal.last_output_at)}
          </span>
        </div>

        {/* Dispatch */}
        {terminal.dispatch_id && (
          <div className="flex items-center justify-between">
            <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Dispatch</span>
            <span
              style={{
                fontSize: 10,
                color: 'var(--color-muted)',
                fontFamily: 'monospace',
                maxWidth: 140,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={terminal.dispatch_id}
            >
              {terminal.dispatch_id}
            </span>
          </div>
        )}

        {/* Stall count */}
        {(terminal.stall_count ?? 0) > 0 && (
          <div className="flex items-center justify-between">
            <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Stalls</span>
            <span style={{ fontSize: 11, color: 'var(--color-warning)', fontWeight: 600 }}>
              {terminal.stall_count}
            </span>
          </div>
        )}

        {/* Blocked reason */}
        {terminal.blocked_reason && (
          <div
            style={{
              marginTop: 4,
              padding: '6px 10px',
              borderRadius: 8,
              background: 'rgba(255, 107, 107, 0.08)',
              border: '1px solid rgba(255, 107, 107, 0.2)',
            }}
          >
            <span style={{ fontSize: 11, color: 'var(--color-error)' }}>
              Blocked: {terminal.blocked_reason}
            </span>
          </div>
        )}

        {/* Context pressure */}
        {terminal.context_pressure && (
          <div className="flex items-center justify-between" style={{ marginTop: 2 }}>
            <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Context left</span>
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: hasContextWarning ? 'var(--color-error)' : 'var(--color-success)',
              }}
            >
              {terminal.context_pressure.remaining_pct}%
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
