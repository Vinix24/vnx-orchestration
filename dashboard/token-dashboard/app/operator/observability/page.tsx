'use client';

import React from 'react';
import { useObservability } from '@/lib/hooks';
import type { ObservabilityEnvelope } from '@/lib/types';

const PANEL = 'linear-gradient(135deg, rgba(10,20,48,0.9) 0%, rgba(10,20,48,0.7) 100%)';

function Section({ title, count, degraded, children }: {
  title: string; count?: number; degraded?: boolean; children: React.ReactNode;
}) {
  return (
    <section
      data-testid={`obs-section-${title.toLowerCase().replace(/\s+/g, '-')}`}
      style={{ borderRadius: 10, padding: 14, background: PANEL, border: '1px solid rgba(255,255,255,0.08)', display: 'flex', flexDirection: 'column', gap: 8 }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <h2 style={{ fontSize: 13, fontWeight: 700, margin: 0, color: 'var(--color-foreground)' }}>{title}</h2>
        {count != null && <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>({count})</span>}
        {degraded && (
          <span data-testid={`obs-degraded-${title.toLowerCase().replace(/\s+/g, '-')}`} style={{ fontSize: 10, color: 'var(--color-warning, #facc15)', border: '1px solid var(--color-warning, #facc15)', borderRadius: 5, padding: '1px 6px' }}>
            degraded
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

const _muted: React.CSSProperties = { fontSize: 11, color: 'rgba(244,244,249,0.4)', padding: '6px 0' };
const _row: React.CSSProperties = { fontSize: 11, borderBottom: '1px solid rgba(255,255,255,0.06)', paddingBottom: 4, display: 'flex', gap: 8, flexWrap: 'wrap' };

function chainColor(status: string): string {
  switch (status) {
    case 'complete': return 'var(--color-success, #50fa7b)';
    case 'broken': return 'var(--color-danger, #ff5555)';
    default: return 'var(--color-warning, #facc15)';  // incomplete
  }
}

function Body({ data }: { data: ObservabilityEnvelope }) {
  const sl = data.self_learning;
  const tg = data.tagging;
  const pv = data.provenance;
  const rt = data.runtime;
  return (
    <div data-testid="observability-page" style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <h1 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>Observability</h1>
        <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>{data.project_id} · governance & audit trail</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 16, alignItems: 'start' }}>
        {/* Self-learning loop */}
        <Section title="Self-learning" count={sl.events.length} degraded={sl.degraded}>
          <div style={{ fontSize: 11, color: 'var(--color-muted)' }}>{sl.proposals} pending rule proposal(s)</div>
          {sl.events.length === 0 ? (
            <div style={_muted}>No confidence adjustments recorded.</div>
          ) : sl.events.map((e, i) => (
            <div key={i} data-testid="obs-learning-row" style={_row}>
              <code style={{ fontWeight: 700 }}>{e.dispatch_id}</code>
              <span style={{ color: e.confidence_change >= 0 ? 'var(--color-success, #50fa7b)' : 'var(--color-danger, #ff5555)' }}>
                {e.confidence_change >= 0 ? '+' : ''}{e.confidence_change.toFixed(3)}
              </span>
              <span style={{ color: 'var(--color-muted)' }}>{e.outcome} · ↑{e.patterns_boosted} ↓{e.patterns_decayed} · {e.occurred_at}</span>
            </div>
          ))}
        </Section>

        {/* Tagging agent */}
        <Section title="Tagging agent" count={tg.events.length} degraded={tg.degraded}>
          {tg.events.length === 0 ? (
            <div style={_muted}>{tg.degraded ? 'No tagging_events table yet (tagger has not run since the audit trail was added).' : 'No taggings recorded.'}</div>
          ) : tg.events.map((e, i) => (
            <div key={i} data-testid="obs-tagging-row" style={_row}>
              <span style={{ fontWeight: 700 }} title={e.pattern_title ?? ''}>{(e.pattern_title ?? `#${e.pattern_id}`).slice(0, 40)}</span>
              <span style={{ color: 'var(--color-accent, #f97316)' }}>{e.tags.join(', ')}</span>
              <span style={{ color: 'var(--color-muted)' }}>{e.provider} · {e.tagged_at}</span>
            </div>
          ))}
        </Section>

        {/* Provenance / traceability */}
        <Section title="Provenance" degraded={pv.degraded}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {Object.keys(pv.by_status).length === 0 ? (
              <span style={_muted}>Registry empty — dispatch→receipt→commit chain not yet populated.</span>
            ) : Object.entries(pv.by_status).map(([status, n]) => (
              <span key={status} data-testid={`obs-chain-${status}`} style={{ fontSize: 10, fontWeight: 700, color: chainColor(status), border: `1px solid ${chainColor(status)}`, borderRadius: 5, padding: '1px 7px' }}>
                {status}: {n}
              </span>
            ))}
          </div>
          {pv.recent.map((r, i) => (
            <div key={i} data-testid="obs-provenance-row" style={_row}>
              <code style={{ fontWeight: 700 }}>{r.dispatch_id}</code>
              <span style={{ color: chainColor(r.chain_status) }}>{r.chain_status}</span>
              <span style={{ color: 'var(--color-muted)' }}>
                {r.commit_sha ? `commit ${r.commit_sha.slice(0, 8)}` : 'no commit'}{r.pr_number ? ` · PR#${r.pr_number}` : ''}
                {r.gaps.length ? ` · gaps: ${r.gaps.length}` : ''}
              </span>
            </div>
          ))}
        </Section>

        {/* Runtime health */}
        <Section title="Runtime health">
          <div data-testid="obs-daemons" style={{ fontSize: 11 }}>
            <span style={{ color: rt.daemons_running > 0 ? 'var(--color-success, #50fa7b)' : 'var(--color-muted)' }}>
              {rt.daemons_running} daemon(s) running for this project
            </span>
            {rt.daemons.map((d, i) => (
              <span key={i} style={{ color: 'var(--color-muted)' }}> · {d.name} ({d.pid})</span>
            ))}
          </div>
          <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--color-muted)', marginTop: 4 }}>Cron ({rt.cron.length})</div>
          {rt.cron.length === 0 ? (
            <div style={_muted}>No VNX cron jobs found.</div>
          ) : rt.cron.map((c, i) => (
            <div key={i} data-testid="obs-cron-row" style={_row}>
              <code style={{ fontWeight: 700 }}>{c.schedule}</code>
              <span style={{ color: 'var(--color-muted)', overflow: 'hidden', textOverflow: 'ellipsis' }} title={c.command}>{c.command.slice(0, 64)}</span>
              <span style={{ color: c.last_run ? 'var(--color-success, #50fa7b)' : 'rgba(244,244,249,0.4)' }}>{c.last_run ? `last ${c.last_run.slice(0, 16)}` : 'never run'}</span>
            </div>
          ))}
        </Section>
      </div>
    </div>
  );
}

export default function ObservabilityPage() {
  const { data, isLoading, error } = useObservability();
  if (isLoading) {
    return <div data-testid="observability-loading" style={{ padding: 24, color: 'var(--color-muted)' }}>Loading observability…</div>;
  }
  if (error || !data) {
    return <div data-testid="observability-error" style={{ padding: 24, color: 'var(--color-danger, #ff5555)' }}>Failed to load observability.</div>;
  }
  return <Body data={data} />;
}
