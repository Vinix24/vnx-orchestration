'use client';

import { useSystemHealth } from '@/lib/hooks';
import type { SystemHealthComponent } from '@/lib/types';

// Status → color. Unknown statuses fall back to muted (never throws on a new status string).
function statusColor(status: string): string {
  switch (status) {
    case 'healthy': return 'var(--color-success, #50fa7b)';
    case 'degraded': return 'var(--color-warning, #facc15)';
    case 'dead': return 'var(--color-danger, #ff5555)';
    default: return 'var(--color-muted, rgba(244,244,249,0.5))';
  }
}

function ComponentCard({ name, comp }: { name: string; comp: SystemHealthComponent }) {
  const detailKeys = Object.keys(comp.details ?? {});
  return (
    <div
      data-testid={`health-component-${name}`}
      style={{
        borderRadius: 10,
        padding: '12px 14px',
        background: 'linear-gradient(135deg, rgba(10,20,48,0.9) 0%, rgba(10,20,48,0.7) 100%)',
        border: '1px solid rgba(255,255,255,0.08)',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-foreground)' }}>{name}</span>
        <span
          data-testid={`health-status-${name}`}
          style={{
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: '0.04em',
            padding: '2px 8px',
            borderRadius: 6,
            color: statusColor(comp.status),
            border: `1px solid ${statusColor(comp.status)}`,
          }}
        >
          {comp.status}
        </span>
      </div>
      {detailKeys.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {detailKeys.slice(0, 8).map((k) => (
            <span key={k} style={{ fontSize: 10, color: 'var(--color-muted)' }}>
              {k}: {String((comp.details as Record<string, unknown>)[k])}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default function SystemHealthPage() {
  const { data, isLoading, error } = useSystemHealth();

  if (isLoading) {
    return <div data-testid="health-loading" style={{ padding: 24, color: 'var(--color-muted)' }}>Loading system health…</div>;
  }
  if (error || !data) {
    return <div data-testid="health-error" style={{ padding: 24, color: 'var(--color-danger, #ff5555)' }}>Failed to load system health.</div>;
  }

  const components = data.components ?? {};
  const names = Object.keys(components).sort();
  const scorePct = Math.round((data.health_score ?? 0) * 100);

  return (
    <div data-testid="health-page" style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>System Health</h1>
        <span
          data-testid="health-overall"
          style={{ fontSize: 12, fontWeight: 700, color: statusColor(data.status) }}
        >
          {data.status}
        </span>
        <span data-testid="health-score" style={{ fontSize: 12, color: 'var(--color-muted)' }}>
          score {scorePct}%
        </span>
        {data.queried_at && (
          <span style={{ fontSize: 11, color: 'rgba(244,244,249,0.4)' }}>· {data.queried_at}</span>
        )}
      </div>

      {names.length === 0 ? (
        <div data-testid="health-empty" style={{ color: 'var(--color-muted)' }}>No components reported.</div>
      ) : (
        <div
          data-testid="health-grid"
          style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 12 }}
        >
          {names.map((name) => (
            <ComponentCard key={name} name={name} comp={components[name]} />
          ))}
        </div>
      )}
    </div>
  );
}
