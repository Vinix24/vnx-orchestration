'use client';

import { RefreshCw, Brain, CheckCircle2, AlertTriangle, Zap, Activity } from 'lucide-react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  LineChart,
  Line,
  Legend,
} from 'recharts';
import {
  useIntelligencePatterns,
  useIntelligenceInjections,
  useIntelligenceClassifications,
  useIntelligenceDispatchOutcomes,
} from '@/lib/hooks';
import type { SuccessPattern, Antipattern, DispatchOutcome, ClassificationRecord } from '@/lib/types';

// ---- Helpers ----

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ff6b6b',
  high: '#f97316',
  medium: '#facc15',
  low: '#6B8AE6',
};

const PIE_PALETTE = ['#6B8AE6', '#50fa7b', '#facc15', '#f97316', '#9B6BE6', '#ff6b6b', '#8be9fd'];

const TRACK_COLORS: Record<string, string> = {
  A: '#50fa7b',
  B: '#facc15',
  C: '#9B6BE6',
};

function SectionHeader({ icon: Icon, title, count }: { icon: React.ElementType; title: string; count?: number }) {
  return (
    <div className="flex items-center gap-2" style={{ marginBottom: 16 }}>
      <Icon size={16} style={{ color: 'var(--color-accent)' }} />
      <h3 style={{ fontSize: 14, fontWeight: 700, color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}>
        {title}
      </h3>
      {count !== undefined && (
        <span style={{ fontSize: 11, color: 'var(--color-muted)', background: 'rgba(255,255,255,0.05)', borderRadius: 20, padding: '1px 8px' }}>
          {count}
        </span>
      )}
    </div>
  );
}

function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-12">
      <div className="animate-spin w-6 h-6 border-2 rounded-full" style={{ borderColor: 'var(--color-card-border)', borderTopColor: 'var(--color-accent)' }} />
    </div>
  );
}

function EmptyState({ label }: { label: string }) {
  return (
    <div style={{ padding: '32px 20px', textAlign: 'center', color: 'var(--color-muted)', fontSize: 13, background: 'rgba(255,255,255,0.02)', borderRadius: 12, border: '1px dashed rgba(255,255,255,0.08)' }}>
      {label}
    </div>
  );
}

// ---- Pattern Cards ----

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.min(1, Math.max(0, value)) * 100;
  const color = pct >= 80 ? '#50fa7b' : pct >= 50 ? '#facc15' : '#ff6b6b';
  return (
    <div style={{ height: 4, background: 'rgba(255,255,255,0.1)', borderRadius: 2, overflow: 'hidden', marginTop: 6 }}>
      <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 2, transition: 'width 0.4s ease' }} />
    </div>
  );
}

function SuccessPatternCard({ pattern }: { pattern: SuccessPattern }) {
  return (
    <div style={{ padding: '14px 16px', borderRadius: 10, background: 'rgba(80, 250, 123, 0.06)', border: '1px solid rgba(80, 250, 123, 0.18)' }}>
      <div className="flex items-start justify-between gap-2">
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-foreground)', lineHeight: 1.4 }}>{pattern.title}</span>
        <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 5, background: 'rgba(80, 250, 123, 0.12)', border: '1px solid rgba(80, 250, 123, 0.25)', color: '#50fa7b', flexShrink: 0 }}>
          {pattern.category || 'general'}
        </span>
      </div>
      <ConfidenceBar value={pattern.confidence} />
      <div className="flex items-center gap-3" style={{ marginTop: 8 }}>
        <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>
          conf <strong style={{ color: '#50fa7b' }}>{(pattern.confidence * 100).toFixed(0)}%</strong>
        </span>
        <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>
          used <strong style={{ color: 'var(--color-foreground)' }}>{pattern.used_count}</strong>×
        </span>
        {pattern.last_seen && (
          <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)' }}>{pattern.last_seen.slice(0, 10)}</span>
        )}
      </div>
    </div>
  );
}

function AntipatternCard({ pattern }: { pattern: Antipattern }) {
  const color = SEVERITY_COLORS[pattern.severity] ?? SEVERITY_COLORS.medium;
  return (
    <div style={{ padding: '14px 16px', borderRadius: 10, background: 'rgba(255,107,107,0.06)', border: '1px solid rgba(255,107,107,0.18)' }}>
      <div className="flex items-start justify-between gap-2">
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-foreground)', lineHeight: 1.4 }}>{pattern.title}</span>
        <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 5, background: 'rgba(255,107,107,0.12)', border: `1px solid ${color}40`, color, flexShrink: 0 }}>
          {pattern.severity}
        </span>
      </div>
      <div className="flex items-center gap-3" style={{ marginTop: 8 }}>
        <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>
          seen <strong style={{ color }}>{pattern.occurrence_count}</strong>×
        </span>
        {pattern.last_seen && (
          <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)' }}>{pattern.last_seen.slice(0, 10)}</span>
        )}
      </div>
    </div>
  );
}

// ---- Classification Analytics ----

function parseQualityScore(s: string): number | null {
  const n = parseFloat(s);
  return isNaN(n) ? null : n;
}

function buildQualityBuckets(records: ClassificationRecord[]) {
  const buckets: Record<string, number> = { '0–2': 0, '2–4': 0, '4–6': 0, '6–8': 0, '8–10': 0 };
  for (const r of records) {
    const score = parseQualityScore(r.quality_score);
    if (score === null) continue;
    if (score <= 2) buckets['0–2']++;
    else if (score <= 4) buckets['2–4']++;
    else if (score <= 6) buckets['4–6']++;
    else if (score <= 8) buckets['6–8']++;
    else buckets['8–10']++;
  }
  return Object.entries(buckets).map(([range, count]) => ({ range, count }));
}

function buildContentTypeCounts(records: ClassificationRecord[]) {
  const counts: Record<string, number> = {};
  for (const r of records) {
    const ct = r.content_type?.trim() || 'unknown';
    counts[ct] = (counts[ct] ?? 0) + 1;
  }
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 7)
    .map(([name, value]) => ({ name, value }));
}

function buildComplexityCounts(records: ClassificationRecord[]) {
  const counts: Record<string, number> = { high: 0, medium: 0, low: 0, unknown: 0 };
  for (const r of records) {
    const key = r.complexity?.trim().toLowerCase() || 'unknown';
    if (key in counts) counts[key]++;
    else counts['unknown']++;
  }
  return Object.entries(counts).filter(([, v]) => v > 0).map(([name, value]) => ({ name, value }));
}

const COMPLEXITY_COLORS: Record<string, string> = {
  high: '#ff6b6b',
  medium: '#facc15',
  low: '#50fa7b',
  unknown: '#6B6B6B',
};

// ---- Dispatch Outcomes ----

function buildTrackSuccessRates(outcomes: DispatchOutcome[]) {
  const byTrack: Record<string, { total: number; success: number }> = {};
  for (const o of outcomes) {
    const track = o.track || 'unknown';
    if (!byTrack[track]) byTrack[track] = { total: 0, success: 0 };
    byTrack[track].total++;
    if (/success|complete|done/i.test(o.status)) byTrack[track].success++;
  }
  return Object.entries(byTrack)
    .filter(([t]) => t !== 'unknown')
    .map(([track, { total, success }]) => ({
      track,
      rate: total > 0 ? Math.round((success / total) * 100) : 0,
      total,
    }))
    .sort((a, b) => a.track.localeCompare(b.track));
}

function buildFailureTrend(outcomes: DispatchOutcome[]) {
  const dailyCounts: Record<string, number> = {};
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - 30);

  for (const o of outcomes) {
    if (!o.timestamp || /success|complete|done/i.test(o.status)) continue;
    const date = o.timestamp.slice(0, 10);
    if (new Date(date) < cutoff) continue;
    dailyCounts[date] = (dailyCounts[date] ?? 0) + 1;
  }

  return Object.entries(dailyCounts)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, failures]) => ({ date: date.slice(5), failures }));
}

// ---- Main Page ----

export default function IntelligencePage() {
  const { data: patternsData, isLoading: patternsLoading, mutate: mutatePatterns } = useIntelligencePatterns();
  const { data: injectionsData, isLoading: injectionsLoading, mutate: mutateInjections } = useIntelligenceInjections();
  const { data: classificationsData, isLoading: classLoading, mutate: mutateClass } = useIntelligenceClassifications();
  const { data: outcomesData, isLoading: outcomesLoading, mutate: mutateOutcomes } = useIntelligenceDispatchOutcomes();

  function handleRefresh() {
    mutatePatterns();
    mutateInjections();
    mutateClass();
    mutateOutcomes();
  }

  const successPatterns = patternsData?.success_patterns ?? [];
  const antipatterns = patternsData?.antipatterns ?? [];
  const injections = injectionsData?.injections ?? [];
  const classifications = classificationsData?.classifications ?? [];
  const outcomes = outcomesData?.outcomes ?? [];

  const qualityBuckets = buildQualityBuckets(classifications);
  const contentTypeCounts = buildContentTypeCounts(classifications);
  const complexityCounts = buildComplexityCounts(classifications);
  const trackSuccessRates = buildTrackSuccessRates(outcomes);
  const failureTrend = buildFailureTrend(outcomes);

  return (
    <div>
      {/* Page header */}
      <div className="flex items-center justify-between" style={{ marginBottom: 28 }}>
        <div className="flex items-center gap-3">
          <div style={{ height: 28, width: 4, borderRadius: 2, background: 'var(--color-accent)' }} />
          <div>
            <h2 style={{ fontSize: '1.5rem', fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--color-foreground)' }}>
              Intelligence
            </h2>
            <p style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
              Pattern analytics · Classification insights · Dispatch outcomes
            </p>
          </div>
        </div>
        <button
          onClick={handleRefresh}
          style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 14px', borderRadius: 8, background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', cursor: 'pointer', fontSize: 12, color: 'var(--color-muted)' }}
        >
          <RefreshCw size={13} />
          Refresh
        </button>
      </div>

      {/* === Section 1: Pattern Overview === */}
      <div style={{ marginBottom: 40 }}>
        <SectionHeader icon={Brain} title="Pattern Overview" />
        {patternsLoading ? (
          <LoadingSpinner />
        ) : successPatterns.length === 0 && antipatterns.length === 0 ? (
          <EmptyState label="No pattern data available yet. Patterns are populated by the intelligence pipeline." />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
            {/* Success Patterns */}
            <div>
              <div className="flex items-center gap-2" style={{ marginBottom: 12 }}>
                <CheckCircle2 size={13} style={{ color: '#50fa7b' }} />
                <span style={{ fontSize: 12, fontWeight: 600, color: '#50fa7b' }}>
                  Success Patterns ({successPatterns.length})
                </span>
              </div>
              {successPatterns.length === 0 ? (
                <EmptyState label="No success patterns recorded." />
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {successPatterns.map((p, i) => <SuccessPatternCard key={i} pattern={p} />)}
                </div>
              )}
            </div>
            {/* Antipatterns */}
            <div>
              <div className="flex items-center gap-2" style={{ marginBottom: 12 }}>
                <AlertTriangle size={13} style={{ color: '#ff6b6b' }} />
                <span style={{ fontSize: 12, fontWeight: 600, color: '#ff6b6b' }}>
                  Antipatterns ({antipatterns.length})
                </span>
              </div>
              {antipatterns.length === 0 ? (
                <EmptyState label="No antipatterns recorded." />
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {antipatterns.map((p, i) => <AntipatternCard key={i} pattern={p} />)}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* === Section 2: Classification Analytics === */}
      <div style={{ marginBottom: 40 }}>
        <SectionHeader icon={Activity} title="Classification Analytics" count={classifications.length} />
        {classLoading ? (
          <LoadingSpinner />
        ) : classifications.length === 0 ? (
          <EmptyState label="No classification data available. Classifications are extracted from unified reports." />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 24 }}>
            {/* Quality score distribution */}
            <div style={{ padding: 16, borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}>
              <p style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 12 }}>Quality Score Distribution</p>
              <ResponsiveContainer width="100%" height={160}>
                <BarChart data={qualityBuckets} layout="vertical" margin={{ left: 0, right: 10, top: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" horizontal={false} />
                  <XAxis type="number" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.4)' }} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="range" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.5)' }} axisLine={false} tickLine={false} width={32} />
                  <Tooltip
                    contentStyle={{ background: '#0c1638', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }}
                    cursor={{ fill: 'rgba(107,138,230,0.08)' }}
                  />
                  <Bar dataKey="count" fill="#6B8AE6" radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Content type pie */}
            <div style={{ padding: 16, borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}>
              <p style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 12 }}>Content Types</p>
              {contentTypeCounts.length === 0 ? (
                <EmptyState label="No content type data." />
              ) : (
                <ResponsiveContainer width="100%" height={160}>
                  <PieChart>
                    <Pie data={contentTypeCounts} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={60} strokeWidth={0}>
                      {contentTypeCounts.map((_, i) => (
                        <Cell key={i} fill={PIE_PALETTE[i % PIE_PALETTE.length]} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{ background: '#0c1638', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }}
                    />
                    <Legend wrapperStyle={{ fontSize: 10, color: 'rgba(255,255,255,0.5)' }} />
                  </PieChart>
                </ResponsiveContainer>
              )}
            </div>

            {/* Complexity distribution */}
            <div style={{ padding: 16, borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}>
              <p style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 12 }}>Complexity Distribution</p>
              <ResponsiveContainer width="100%" height={160}>
                <BarChart data={complexityCounts} margin={{ left: 0, right: 10, top: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
                  <XAxis dataKey="name" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.5)' }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.4)' }} axisLine={false} tickLine={false} width={24} />
                  <Tooltip
                    contentStyle={{ background: '#0c1638', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }}
                    cursor={{ fill: 'rgba(107,138,230,0.08)' }}
                  />
                  <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                    {complexityCounts.map((entry, i) => (
                      <Cell key={i} fill={COMPLEXITY_COLORS[entry.name] ?? '#6B6B6B'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </div>

      {/* === Section 3: Injection History === */}
      <div style={{ marginBottom: 40 }}>
        <SectionHeader icon={Zap} title="Injection History" count={injections.length} />
        {injectionsLoading ? (
          <LoadingSpinner />
        ) : injections.length === 0 ? (
          <EmptyState label="No injection events recorded yet." />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {injections.slice(0, 20).map((ev, i) => (
              <div
                key={i}
                style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '10px 14px', borderRadius: 8, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)' }}
              >
                <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)', minWidth: 80, flexShrink: 0 }}>
                  {ev.timestamp ? ev.timestamp.slice(0, 16).replace('T', ' ') : '—'}
                </span>
                <span style={{ fontSize: 11, color: 'var(--color-muted)', fontFamily: 'monospace', flexShrink: 0, minWidth: 160 }}>
                  {ev.dispatch_id ? ev.dispatch_id.slice(0, 20) + (ev.dispatch_id.length > 20 ? '…' : '') : '—'}
                </span>
                <div className="flex items-center gap-8" style={{ marginLeft: 'auto' }}>
                  <span style={{ fontSize: 11 }}>
                    <span style={{ color: 'var(--color-muted)' }}>injected </span>
                    <strong style={{ color: '#50fa7b' }}>{ev.items_injected}</strong>
                  </span>
                  <span style={{ fontSize: 11 }}>
                    <span style={{ color: 'var(--color-muted)' }}>suppressed </span>
                    <strong style={{ color: '#facc15' }}>{ev.items_suppressed}</strong>
                  </span>
                </div>
              </div>
            ))}
            {injections.length > 20 && (
              <p style={{ fontSize: 11, color: 'var(--color-muted)', textAlign: 'center', padding: '4px 0' }}>
                Showing 20 of {injections.length} events
              </p>
            )}
          </div>
        )}
      </div>

      {/* === Section 4: Dispatch Outcomes === */}
      <div style={{ marginBottom: 40 }}>
        <SectionHeader icon={Activity} title="Dispatch Outcomes" count={outcomes.length} />
        {outcomesLoading ? (
          <LoadingSpinner />
        ) : outcomes.length === 0 ? (
          <EmptyState label="No dispatch outcome data available. Outcomes are parsed from t0_receipts.ndjson." />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 24 }}>
            {/* Success rate per track */}
            <div style={{ padding: 16, borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}>
              <p style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 16 }}>Success Rate by Track</p>
              {trackSuccessRates.length === 0 ? (
                <EmptyState label="No track data." />
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {trackSuccessRates.map(({ track, rate, total }) => {
                    const color = TRACK_COLORS[track] ?? '#6B8AE6';
                    return (
                      <div key={track}>
                        <div className="flex items-center justify-between" style={{ marginBottom: 4 }}>
                          <span style={{ fontSize: 12, fontWeight: 600, color }}>Track {track}</span>
                          <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                            <strong style={{ color }}>{rate}%</strong> · {total} dispatches
                          </span>
                        </div>
                        <div style={{ height: 6, background: 'rgba(255,255,255,0.08)', borderRadius: 3, overflow: 'hidden' }}>
                          <div style={{ width: `${rate}%`, height: '100%', background: color, borderRadius: 3, transition: 'width 0.4s ease' }} />
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Failure trend line */}
            <div style={{ padding: 16, borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}>
              <p style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-muted)', marginBottom: 12 }}>Failure Trend (last 30 days)</p>
              {failureTrend.length === 0 ? (
                <EmptyState label="No failure events in the last 30 days." />
              ) : (
                <ResponsiveContainer width="100%" height={160}>
                  <LineChart data={failureTrend} margin={{ left: 0, right: 10, top: 4, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
                    <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.4)' }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
                    <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: 'rgba(255,255,255,0.4)' }} axisLine={false} tickLine={false} width={24} />
                    <Tooltip
                      contentStyle={{ background: '#0c1638', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }}
                      cursor={{ stroke: 'rgba(255,255,255,0.1)' }}
                    />
                    <Line type="monotone" dataKey="failures" stroke="#ff6b6b" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: '#ff6b6b' }} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
