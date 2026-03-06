'use client';

import { usePolling } from '@/lib/use-polling';
import type { DashboardStatus } from '@/lib/types';

export default function PRQueuePage() {
  const { data, loading } = usePolling<DashboardStatus>('/state/dashboard_status.json');

  if (loading || !data) {
    return (
      <div className="flex items-center justify-center py-20">
        <div
          className="animate-spin w-8 h-8 border-2 rounded-full"
          style={{ borderColor: 'var(--color-card-border)', borderTopColor: 'var(--color-accent)' }}
        />
      </div>
    );
  }

  const prQueue = data.pr_queue ?? {};
  const totalPrs = prQueue.total_prs ?? 0;
  const completedPrs = prQueue.completed_prs ?? 0;
  const progressPercent = prQueue.progress_percent ?? (totalPrs ? Math.round((completedPrs / totalPrs) * 100) : 0);
  const prs = prQueue.prs ?? [];

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>PR Queue</h2>
      </div>

      <div className="glass-card animate-in" style={{ padding: 24, marginBottom: 20 }}>
        {/* Feature Header */}
        <div className="flex items-baseline justify-between flex-wrap gap-3">
          <div className="text-lg font-bold" style={{ color: 'var(--color-foreground)' }}>
            {prQueue.active_feature || '—'}
          </div>
          <div className="text-sm" style={{ color: 'var(--color-muted)' }}>
            {completedPrs}/{totalPrs} completed · {progressPercent}%
          </div>
        </div>

        {/* Progress Bar */}
        <div
          style={{
            marginTop: 16,
            width: '100%',
            height: 12,
            borderRadius: 999,
            background: 'rgba(255, 255, 255, 0.08)',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              height: '100%',
              width: `${Math.max(0, Math.min(100, progressPercent))}%`,
              background: 'linear-gradient(90deg, rgba(80, 250, 123, 0.95), rgba(250, 204, 21, 0.92), rgba(249, 115, 22, 0.92))',
              transition: 'width 0.6s ease',
              borderRadius: 999,
            }}
          />
        </div>

        {/* PR Table */}
        <div className="overflow-x-auto" style={{ marginTop: 20 }}>
          <table className="w-full text-sm" style={{ borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.08)' }}>
                {['PR', 'Description', 'Status', 'Dependencies'].map((h) => (
                  <th
                    key={h}
                    className="text-xs font-bold"
                    style={{
                      textAlign: 'left',
                      padding: '12px 14px',
                      color: 'var(--color-muted)',
                      textTransform: 'uppercase',
                      letterSpacing: '0.1em',
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {prs.map((pr) => {
                const status = pr.status ?? 'pending';
                const isBlocked = Boolean(pr.blocked);
                const statusLabel = status === 'in_progress' ? 'In Progress' : status === 'done' ? 'Done' : 'Pending';
                const displayLabel = isBlocked ? 'Blocked' : statusLabel;
                const deps = pr.deps?.length ? pr.deps.join(', ') : '—';

                return (
                  <tr
                    key={pr.id}
                    className="table-row-hover"
                    style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.04)' }}
                  >
                    <td style={{ padding: '12px 14px' }}>
                      <span className="font-bold" style={{ color: 'var(--color-foreground)', letterSpacing: '0.01em' }}>
                        {pr.id}
                      </span>
                    </td>
                    <td style={{ padding: '12px 14px', color: 'var(--color-foreground)' }}>
                      {pr.description || '—'}
                    </td>
                    <td style={{ padding: '12px 14px' }}>
                      <StatusPill status={status} blocked={isBlocked} label={displayLabel} />
                    </td>
                    <td className="text-xs" style={{ padding: '12px 14px', color: 'var(--color-muted)' }}>
                      {deps}
                    </td>
                  </tr>
                );
              })}
              {prs.length === 0 && (
                <tr>
                  <td colSpan={4} className="text-center text-sm" style={{ padding: 40, color: 'var(--color-muted)' }}>
                    No PRs in queue.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function StatusPill({ status, blocked, label }: { status: string; blocked: boolean; label: string }) {
  let dotColor = '#facc15';
  let dotShadow = 'rgba(250, 204, 21, 0.45)';
  let pillBg = 'rgba(0, 0, 0, 0.18)';
  let pillBorder = 'rgba(255, 255, 255, 0.14)';

  if (status === 'done') {
    dotColor = '#50fa7b';
    dotShadow = 'rgba(80, 250, 123, 0.55)';
  } else if (status === 'in_progress') {
    dotColor = '#f97316';
    dotShadow = 'rgba(249, 115, 22, 0.55)';
  }

  if (blocked) {
    pillBg = 'rgba(255, 107, 107, 0.1)';
    pillBorder = 'rgba(255, 107, 107, 0.35)';
    dotColor = '#ff6b6b';
    dotShadow = 'rgba(255, 107, 107, 0.55)';
  }

  return (
    <span
      className="inline-flex items-center gap-2 text-xs"
      style={{
        padding: '4px 10px',
        borderRadius: 999,
        border: `1px solid ${pillBorder}`,
        background: pillBg,
        color: 'var(--color-foreground)',
        whiteSpace: 'nowrap',
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: dotColor,
          boxShadow: `0 0 10px ${dotShadow}`,
          display: 'inline-block',
        }}
      />
      {label}
    </span>
  );
}
