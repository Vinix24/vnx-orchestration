'use client';

import { useSystemHealth, useHealthBeacons } from '@/lib/hooks';
import type { SystemHealthComponent, SubsystemEffectivenessRow } from '@/lib/types';

// Status → color. Unknown statuses fall back to muted (never throws on a new status string).
function statusColor(status: string): string {
  switch (status) {
    case 'healthy': return 'var(--color-success, #50fa7b)';
    case 'degraded': return 'var(--color-warning, #facc15)';
    case 'dead': return 'var(--color-danger, #ff5555)';
    default: return 'var(--color-muted, rgba(244,244,249,0.5))';
  }
}

// Beacon health vocabulary (ok | stale | fail | corrupt | unknown) differs from the
// system-health vocabulary above — kept as a separate mapping rather than overloading it.
function beaconHealthColor(health: string): string {
  switch (health) {
    case 'ok': return 'var(--color-success, #50fa7b)';
    case 'stale': return 'var(--color-warning, #facc15)';
    case 'fail':
    case 'corrupt': return 'var(--color-danger, #ff5555)';
    default: return 'var(--color-muted, rgba(244,244,249,0.5))'; // unknown
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
        background: 'linear-gradient(135deg, #ffffff 0%, #f4f7fb 100%)',
        border: '1px solid var(--color-card-border)',
        boxShadow: 'var(--shadow-md)',
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

function SubsystemCard({ row }: { row: SubsystemEffectivenessRow }) {
  const isUnknown = row.health === 'unknown';
  const detailKeys = Object.keys(row.detail ?? {});
  return (
    <div
      data-testid={`subsystem-card-${row.subsystem}`}
      style={{
        borderRadius: 10,
        padding: '12px 14px',
        background: 'linear-gradient(135deg, #ffffff 0%, #f4f7fb 100%)',
        border: `1px solid ${isUnknown ? 'rgba(255,255,255,0.08)' : `${beaconHealthColor(row.health)}33`}`,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-foreground)' }}>{row.subsystem}</span>
        <span
          data-testid={`subsystem-health-${row.subsystem}`}
          style={{
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: '0.04em',
            padding: '2px 8px',
            borderRadius: 6,
            color: beaconHealthColor(row.health),
            border: `1px solid ${beaconHealthColor(row.health)}`,
          }}
        >
          {row.health}
        </span>
      </div>
      {isUnknown ? (
        <span style={{ fontSize: 10, color: 'var(--color-muted)' }}>
          No probe registered — add or improve an effectiveness probe to measure this subsystem.
        </span>
      ) : (
        <>
          {row.last_signal && (
            <span style={{ fontSize: 10, color: 'var(--color-text-faint)' }}>last signal {row.last_signal}</span>
          )}
          {detailKeys.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              {detailKeys.slice(0, 6).map((k) => (
                <span key={k} style={{ fontSize: 10, color: 'var(--color-muted)' }}>
                  {k}: {String(row.detail[k])}
                </span>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function SystemHealthPage() {
  const { data, isLoading, error } = useSystemHealth();
  const { data: beaconData, isLoading: subsystemsLoading, error: subsystemsError } = useHealthBeacons();

  if (isLoading) {
    return <div data-testid="health-loading" style={{ padding: 24, color: 'var(--color-muted)' }}>Loading system health…</div>;
  }
  if (error || !data) {
    return <div data-testid="health-error" style={{ padding: 24, color: 'var(--color-danger, #ff5555)' }}>Failed to load system health.</div>;
  }

  const components = data.components ?? {};
  const names = Object.keys(components).sort();
  const scorePct = Math.round((data.health_score ?? 0) * 100);
  const subsystems = beaconData?.subsystems ?? [];

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
          <span style={{ fontSize: 11, color: 'var(--color-text-faint)' }}>· {data.queried_at}</span>
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

      <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <h2 style={{ fontSize: 15, fontWeight: 700, margin: 0 }}>Subsystem Effectiveness</h2>

        {subsystemsLoading ? (
          <div data-testid="subsystems-loading" style={{ color: 'var(--color-muted)' }}>Loading subsystem effectiveness…</div>
        ) : subsystemsError ? (
          <div data-testid="subsystems-error" style={{ color: 'var(--color-danger, #ff5555)' }}>Failed to load subsystem effectiveness.</div>
        ) : subsystems.length === 0 ? (
          <div data-testid="subsystems-empty" style={{ color: 'var(--color-muted)' }}>No subsystems reported.</div>
        ) : (
          <div
            data-testid="subsystems-grid"
            style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 12 }}
          >
            {subsystems.map((row) => (
              <SubsystemCard key={row.subsystem} row={row} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
