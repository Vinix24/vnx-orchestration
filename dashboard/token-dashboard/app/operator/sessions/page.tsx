'use client';

import { useLiveSessions } from '@/lib/hooks';
import type { LiveSession } from '@/lib/types';
import { Activity } from 'lucide-react';

function formatAge(seconds: number | null): string {
  if (seconds === null || seconds < 0) return '—';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function StatusBadge({ status }: { status: LiveSession['status'] }) {
  const color = status === 'busy' ? '#facc15' : 'var(--color-muted)';
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 700,
        textTransform: 'uppercase',
        color,
        border: `1px solid ${color}`,
        borderRadius: 5,
        padding: '2px 7px',
      }}
    >
      {status}
    </span>
  );
}

function SessionRow({ session }: { session: LiveSession }) {
  return (
    <div
      data-testid={`session-row-${session.name}`}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        padding: '12px 14px',
        borderBottom: '1px solid rgba(255,255,255,0.06)',
        fontSize: 12,
      }}
    >
      <div style={{ minWidth: 180, flex: 1 }}>
        <div style={{ fontWeight: 600, color: 'var(--color-foreground)' }}>{session.name}</div>
        {session.project && (
          <div style={{ color: 'var(--color-muted)', fontSize: 11 }}>{session.project}</div>
        )}
      </div>
      <div style={{ minWidth: 70 }}>
        <StatusBadge status={session.status} />
      </div>
      <div style={{ minWidth: 160, color: 'var(--color-muted)' }}>
        {session.last_activity ? new Date(session.last_activity).toLocaleString() : '—'}
      </div>
      <div style={{ minWidth: 70, color: 'var(--color-muted)' }}>{formatAge(session.age_seconds)}</div>
      {session.remote_control_url && (
        <a
          href={session.remote_control_url}
          target="_blank"
          rel="noreferrer"
          style={{ color: 'var(--color-accent)', textDecoration: 'none' }}
        >
          remote
        </a>
      )}
    </div>
  );
}

export default function LiveSessionsPage() {
  const { data, error, isLoading } = useLiveSessions();
  const sessions = data?.data ?? [];

  return (
    <div data-testid="live-sessions-page" style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <Activity size={18} style={{ color: 'var(--color-accent)' }} />
        <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>Live Sessions</h1>
        <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>read-only observability</span>
      </div>

      {data?.degraded && (
        <div
          data-testid="live-sessions-degraded"
          style={{
            padding: '10px 14px',
            borderRadius: 8,
            background: 'rgba(250, 204, 21, 0.08)',
            border: '1px solid rgba(250, 204, 21, 0.3)',
            color: '#facc15',
            fontSize: 12,
          }}
        >
          Degraded: {data.degraded_reasons?.join('; ')}
        </div>
      )}

      {isLoading && (
        <div data-testid="live-sessions-loading" style={{ color: 'var(--color-muted)', padding: '20px 0' }}>
          Loading live sessions…
        </div>
      )}

      {!isLoading && error && (
        <div data-testid="live-sessions-error" style={{ color: 'var(--color-danger, #ff5555)', padding: '20px 0' }}>
          Failed to load live sessions.
        </div>
      )}

      {!isLoading && !error && sessions.length === 0 && (
        <div
          data-testid="live-sessions-empty"
          style={{
            padding: '32px 20px',
            textAlign: 'center',
            color: 'var(--color-muted)',
            fontSize: 13,
            background: 'rgba(255,255,255,0.02)',
            borderRadius: 12,
            border: '1px dashed rgba(255,255,255,0.08)',
          }}
        >
          No live tmux sessions found.
        </div>
      )}

      {!isLoading && !error && sessions.length > 0 && (
        <div
          style={{
            borderRadius: 12,
            background: 'rgba(255,255,255,0.03)',
            border: '1px solid rgba(255,255,255,0.08)',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 16,
              padding: '10px 14px',
              borderBottom: '1px solid rgba(255,255,255,0.08)',
              fontSize: 11,
              fontWeight: 700,
              color: 'var(--color-muted)',
              textTransform: 'uppercase',
            }}
          >
            <div style={{ minWidth: 180, flex: 1 }}>Session / Project</div>
            <div style={{ minWidth: 70 }}>Status</div>
            <div style={{ minWidth: 160 }}>Last Activity</div>
            <div style={{ minWidth: 70 }}>Age</div>
          </div>
          {sessions.map(session => (
            <SessionRow key={session.name} session={session} />
          ))}
        </div>
      )}
    </div>
  );
}
