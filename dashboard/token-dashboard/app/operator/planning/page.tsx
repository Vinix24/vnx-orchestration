'use client';

import { usePlanning } from '@/lib/hooks';
import type { PlanningCard, PlanningHorizon } from '@/lib/types';

const HORIZONS: { key: PlanningHorizon; label: string; accent: string }[] = [
  { key: 'now', label: 'Now', accent: 'rgba(249, 115, 22, 0.6)' },
  { key: 'next', label: 'Next', accent: 'rgba(107, 138, 230, 0.6)' },
  { key: 'later', label: 'Later', accent: 'rgba(155, 107, 230, 0.6)' },
];

// Declared phase → color. Unknown phases fall back to muted (never throws).
function phaseColor(phase: string): string {
  switch (phase) {
    case 'done': return 'var(--color-success, #50fa7b)';
    case 'active': case 'in_progress': return 'var(--color-accent, #f97316)';
    case 'blocked': case 'failed': return 'var(--color-danger, #ff5555)';
    case 'queued': case 'proposed': return 'var(--color-info)';
    default: return 'var(--color-muted, rgba(244,244,249,0.5))';
  }
}

function severityColor(sev: string | null): string {
  switch (sev) {
    case 'blocker': case 'critical': return 'var(--color-danger, #ff5555)';
    case 'warn': case 'high': return 'var(--color-warning, #facc15)';
    default: return 'var(--color-muted, rgba(244,244,249,0.5))';
  }
}

function TrackCard({ card }: { card: PlanningCard }) {
  const deliverables = card.deliverables ?? [];
  const ois = card.open_items ?? [];
  const deps = card.depends_on ?? [];
  return (
    <div
      data-testid={`track-${card.track_id}`}
      style={{
        borderRadius: 10,
        padding: '12px 14px',
        background: 'linear-gradient(135deg, #ffffff 0%, #f4f7fb 100%)',
        border: '1px solid var(--color-card-border)',
        boxShadow: 'var(--shadow-md)',
        marginBottom: 8,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-foreground)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={card.title}>
          {card.title || card.track_id}
        </span>
        <span
          data-testid={`track-phase-${card.track_id}`}
          style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 6, color: phaseColor(card.phase), border: `1px solid ${phaseColor(card.phase)}`, flexShrink: 0 }}
        >
          {card.phase}
        </span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 10, color: 'var(--color-muted)' }}>
        {card.next_up && <span data-testid={`track-nextup-${card.track_id}`} style={{ color: 'var(--color-accent, #f97316)', fontWeight: 700 }}>next-up</span>}
        {card.priority && <span>· {card.priority}</span>}
        <span>· {card.dispatch_count} dispatch{card.dispatch_count === 1 ? '' : 'es'}</span>
        {card.pr_ref && <span>· {card.pr_ref}</span>}
      </div>

      {deps.length > 0 && (
        <div style={{ fontSize: 10, color: 'var(--color-text-faint)' }}>
          depends on: {deps.map((d) => d.to_track_id).join(', ')}
        </div>
      )}

      {deliverables.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {deliverables.slice(0, 6).map((d) => (
            <span key={d.deliverable_ref} style={{ fontSize: 10, color: 'var(--color-muted)' }}>
              {d.output_kind} · {d.deliverable_ref} — <span style={{ color: phaseColor(d.derived_status) }}>{d.derived_status}</span>
            </span>
          ))}
        </div>
      )}

      {ois.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {ois.slice(0, 6).map((oi) => (
            <span key={oi.oi_id} style={{ fontSize: 10, color: severityColor(oi.severity) }} title={oi.title}>
              OI: {oi.title}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default function PlanningPage() {
  const { data, isLoading, error } = usePlanning();

  if (isLoading) {
    return <div data-testid="planning-loading" style={{ padding: 24, color: 'var(--color-muted)' }}>Loading planning…</div>;
  }
  if (error || !data) {
    return <div data-testid="planning-error" style={{ padding: 24, color: 'var(--color-danger, #ff5555)' }}>Failed to load planning.</div>;
  }

  const horizons = data.horizons ?? { now: [], next: [], later: [] };
  const driftCount = data.drift?.divergent_count ?? 0;

  return (
    <div data-testid="planning-page" style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>Planning</h1>
        <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>{data.total_tracks ?? 0} tracks</span>
        {driftCount > 0 && (
          <span data-testid="planning-drift" style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-warning, #facc15)' }}>
            · {driftCount} drift
          </span>
        )}
      </div>

      {data.degraded && (
        <div data-testid="planning-degraded" style={{ fontSize: 12, color: 'var(--color-warning, #facc15)' }}>
          Degraded: {(data.degraded_reasons ?? []).join('; ')}
        </div>
      )}

      <div data-testid="planning-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16, alignItems: 'start' }}>
        {HORIZONS.map(({ key, label, accent }) => {
          const cards = horizons[key] ?? [];
          return (
            <div key={key} data-testid={`horizon-${key}`} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-foreground)', borderBottom: `2px solid ${accent}`, paddingBottom: 6 }}>
                {label} <span style={{ color: 'var(--color-muted)', fontWeight: 400 }}>({cards.length})</span>
              </div>
              {cards.length === 0 ? (
                <div data-testid={`horizon-empty-${key}`} style={{ padding: '24px 12px', textAlign: 'center', color: 'var(--color-text-faint)', fontSize: 12, border: '1px dashed rgba(255,255,255,0.06)', borderRadius: 10 }}>
                  No tracks
                </div>
              ) : (
                cards.map((c) => <TrackCard key={c.track_id} card={c} />)
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
