'use client';

import { RefreshCw, AlertTriangle, Info, ShieldAlert, CheckCircle2, BookOpen, TrendingUp } from 'lucide-react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';
import { useGovernanceDigest } from '@/lib/hooks';
import DegradedBanner from '@/components/operator/degraded-banner';
import FreshnessBadge from '@/components/operator/freshness-badge';
import type { DigestRecurrenceRecord, DigestRecommendation } from '@/lib/types';

// ---- Severity helpers ----

const SEVERITY_COLORS: Record<string, string> = {
  blocker: 'var(--color-error)',
  warn: 'var(--color-warning)',
  info: 'var(--color-muted)',
};

const SEVERITY_BG: Record<string, string> = {
  blocker: 'rgba(255, 107, 107, 0.12)',
  warn: 'rgba(250, 204, 21, 0.12)',
  info: 'rgba(255,255,255,0.06)',
};

const SEVERITY_BORDER: Record<string, string> = {
  blocker: 'rgba(255, 107, 107, 0.4)',
  warn: 'rgba(250, 204, 21, 0.35)',
  info: 'rgba(255,255,255,0.12)',
};

const SEVERITY_BAR_FILL: Record<string, string> = {
  blocker: '#ff6b6b',
  warn: '#facc15',
  info: 'rgba(107, 138, 230, 0.8)',
};

function severityColor(sev: string): string {
  return SEVERITY_COLORS[sev] ?? SEVERITY_COLORS.info;
}

function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span
      data-testid="severity-badge"
      style={{
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        padding: '2px 8px',
        borderRadius: 5,
        background: SEVERITY_BG[severity] ?? SEVERITY_BG.info,
        border: `1px solid ${SEVERITY_BORDER[severity] ?? SEVERITY_BORDER.info}`,
        color: severityColor(severity),
        flexShrink: 0,
      }}
    >
      {severity}
    </span>
  );
}

// ---- Loading skeleton ----

function SkeletonRow() {
  return (
    <div
      aria-hidden="true"
      style={{
        height: 48,
        borderRadius: 8,
        background:
          'linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.07) 50%, rgba(255,255,255,0.04) 75%)',
        backgroundSize: '200% 100%',
        animation: 'shimmer 1.6s infinite',
        marginBottom: 8,
        border: '1px solid rgba(255,255,255,0.05)',
      }}
    />
  );
}

// ---- Empty state ----

function EmptySection({ label }: { label: string }) {
  return (
    <div
      style={{
        padding: '32px 20px',
        textAlign: 'center',
        color: 'var(--color-muted)',
        fontSize: 12,
        background: 'rgba(255,255,255,0.02)',
        borderRadius: 10,
        border: '1px dashed rgba(255,255,255,0.07)',
      }}
    >
      {label}
    </div>
  );
}

// ---- Recurrence table ----

function RecurrenceTable({
  patterns,
  isLoading,
}: {
  patterns: DigestRecurrenceRecord[];
  isLoading: boolean;
}) {
  return (
    <div data-testid="recurrence-table" className="glass-card" style={{ padding: '24px' }}>
      <div className="flex items-center gap-2" style={{ marginBottom: 18 }}>
        <TrendingUp size={15} style={{ color: 'var(--color-accent)' }} />
        <h3
          style={{
            fontSize: 14,
            fontWeight: 700,
            color: 'var(--color-foreground)',
            letterSpacing: '-0.01em',
          }}
        >
          Recurring Failure Patterns
        </h3>
        {!isLoading && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--color-muted)',
              background: 'rgba(255,255,255,0.05)',
              borderRadius: 20,
              padding: '1px 8px',
            }}
          >
            {patterns.length}
          </span>
        )}
      </div>

      {isLoading ? (
        <>
          <SkeletonRow />
          <SkeletonRow />
          <SkeletonRow />
        </>
      ) : patterns.length === 0 ? (
        <EmptySection label="No recurring patterns detected." />
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table
            style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontSize: 12,
            }}
          >
            <thead>
              <tr>
                {['Failure Family', 'Count', 'Severity', 'Features', 'PRs'].map(h => (
                  <th
                    key={h}
                    style={{
                      textAlign: 'left',
                      padding: '6px 12px 10px',
                      fontSize: 10,
                      fontWeight: 700,
                      letterSpacing: '0.07em',
                      textTransform: 'uppercase',
                      color: 'rgba(244,244,249,0.4)',
                      borderBottom: '1px solid rgba(255,255,255,0.06)',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {patterns.map((p, i) => (
                <tr
                  key={p.defect_family || i}
                  data-testid="recurrence-row"
                  style={{
                    borderBottom: '1px solid rgba(255,255,255,0.04)',
                    transition: 'background 0.15s ease',
                  }}
                  onMouseEnter={e => {
                    (e.currentTarget as HTMLTableRowElement).style.background =
                      'rgba(255,255,255,0.03)';
                  }}
                  onMouseLeave={e => {
                    (e.currentTarget as HTMLTableRowElement).style.background = 'transparent';
                  }}
                >
                  <td
                    data-testid="recurrence-family"
                    style={{
                      padding: '10px 12px',
                      color: 'var(--color-foreground)',
                      fontWeight: 500,
                      maxWidth: 280,
                    }}
                  >
                    <span
                      title={p.representative_content}
                      style={{
                        display: 'block',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {p.defect_family || '—'}
                    </span>
                    {p.representative_content && (
                      <span
                        style={{
                          display: 'block',
                          fontSize: 11,
                          color: 'var(--color-muted)',
                          marginTop: 2,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {p.representative_content.length > 80
                          ? p.representative_content.slice(0, 80) + '…'
                          : p.representative_content}
                      </span>
                    )}
                  </td>
                  <td
                    data-testid="recurrence-count"
                    style={{
                      padding: '10px 12px',
                      fontWeight: 700,
                      color:
                        p.count >= 5
                          ? 'var(--color-error)'
                          : p.count >= 3
                          ? 'var(--color-warning)'
                          : 'var(--color-foreground)',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  >
                    {p.count}
                  </td>
                  <td style={{ padding: '10px 12px' }}>
                    <SeverityBadge severity={p.severity} />
                  </td>
                  <td
                    data-testid="recurrence-features"
                    style={{ padding: '10px 12px', color: 'var(--color-muted)' }}
                  >
                    {p.impacted_features.length > 0
                      ? p.impacted_features.slice(0, 3).join(', ') +
                        (p.impacted_features.length > 3 ? ` +${p.impacted_features.length - 3}` : '')
                      : '—'}
                  </td>
                  <td
                    data-testid="recurrence-prs"
                    style={{ padding: '10px 12px', color: 'var(--color-muted)' }}
                  >
                    {p.impacted_prs.length > 0
                      ? p.impacted_prs.slice(0, 3).join(', ') +
                        (p.impacted_prs.length > 3 ? ` +${p.impacted_prs.length - 3}` : '')
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---- Recommendation card ----

const CATEGORY_COLORS: Record<string, string> = {
  operational_defect: '#ff6b6b',
  prompt_config_tuning: '#6B8AE6',
  governance_health: '#50fa7b',
  process: '#facc15',
  default: 'var(--color-muted)',
};

function categoryColor(cat: string): string {
  return CATEGORY_COLORS[cat] ?? CATEGORY_COLORS.default;
}

function RecommendationCard({ rec }: { rec: DigestRecommendation }) {
  const catColor = categoryColor(rec.category);
  return (
    <div
      data-testid="recommendation-card"
      className="glass-card"
      style={{
        padding: '18px 20px',
        borderLeft: `3px solid ${severityColor(rec.severity)}`,
      }}
    >
      {/* Header row */}
      <div
        className="flex items-center gap-2"
        style={{ marginBottom: 10, flexWrap: 'wrap' }}
      >
        <span
          data-testid="category-badge"
          style={{
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
            padding: '2px 8px',
            borderRadius: 5,
            background: `${catColor}18`,
            border: `1px solid ${catColor}44`,
            color: catColor,
            flexShrink: 0,
          }}
        >
          {rec.category.replace(/_/g, ' ')}
        </span>
        <SeverityBadge severity={rec.severity} />
        {rec.advisory_only && (
          <span
            data-testid="advisory-only-badge"
            style={{
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: '0.06em',
              textTransform: 'uppercase',
              padding: '2px 8px',
              borderRadius: 5,
              background: 'rgba(107, 138, 230, 0.12)',
              border: '1px solid rgba(107, 138, 230, 0.3)',
              color: '#6B8AE6',
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              flexShrink: 0,
            }}
          >
            <Info size={9} />
            advisory only
          </span>
        )}
        {rec.recurrence_count > 1 && (
          <span
            style={{
              fontSize: 10,
              color: 'var(--color-muted)',
              marginLeft: 'auto',
            }}
          >
            ×{rec.recurrence_count}
          </span>
        )}
      </div>

      {/* Content */}
      <p
        data-testid="recommendation-content"
        style={{
          fontSize: 13,
          color: 'var(--color-foreground)',
          lineHeight: 1.6,
          marginBottom: rec.evidence_basis.length > 0 ? 12 : 0,
        }}
      >
        {rec.content}
      </p>

      {/* Evidence pointers */}
      {rec.evidence_basis.length > 0 && (
        <div>
          <p
            style={{
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: '0.07em',
              textTransform: 'uppercase',
              color: 'rgba(244,244,249,0.35)',
              marginBottom: 6,
            }}
          >
            Evidence
          </p>
          <div
            data-testid="evidence-pointers"
            style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}
          >
            {rec.evidence_basis.map((e, i) => (
              <span
                key={i}
                style={{
                  fontSize: 10,
                  padding: '2px 8px',
                  borderRadius: 5,
                  background: 'rgba(255,255,255,0.05)',
                  border: '1px solid rgba(255,255,255,0.08)',
                  color: 'var(--color-muted)',
                  fontFamily: 'monospace',
                }}
              >
                {e}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---- Signal volume chart ----

interface ChartDatum {
  name: string;
  count: number;
  severity: string;
}

function SignalTimelineChart({
  patterns,
  totalSignals,
  isLoading,
}: {
  patterns: DigestRecurrenceRecord[];
  totalSignals: number;
  isLoading: boolean;
}) {
  const chartData: ChartDatum[] = patterns
    .slice(0, 10)
    .map(p => ({
      name:
        p.defect_family.length > 24
          ? p.defect_family.slice(0, 24) + '…'
          : p.defect_family,
      count: p.count,
      severity: p.severity,
    }))
    .sort((a, b) => b.count - a.count);

  return (
    <div
      data-testid="signal-timeline-chart"
      className="glass-card"
      style={{ padding: '24px' }}
    >
      <div className="flex items-center gap-2" style={{ marginBottom: 18 }}>
        <BookOpen size={15} style={{ color: 'var(--color-accent)' }} />
        <h3
          style={{
            fontSize: 14,
            fontWeight: 700,
            color: 'var(--color-foreground)',
            letterSpacing: '-0.01em',
          }}
        >
          Signal Volume by Pattern
        </h3>
        {!isLoading && totalSignals > 0 && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--color-muted)',
              marginLeft: 'auto',
            }}
          >
            {totalSignals} total signals
          </span>
        )}
      </div>

      {isLoading ? (
        <div style={{ height: 200 }}>
          <SkeletonRow />
          <SkeletonRow />
          <SkeletonRow />
        </div>
      ) : chartData.length === 0 ? (
        <EmptySection label="No signal data available." />
      ) : (
        <div style={{ width: '100%', height: 220 }}>
          <ResponsiveContainer>
            <BarChart
              data={chartData}
              layout="vertical"
              margin={{ top: 0, right: 20, left: 0, bottom: 0 }}
            >
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="rgba(255,255,255,0.04)"
                horizontal={false}
              />
              <XAxis
                type="number"
                tick={{ fill: 'rgba(244,244,249,0.45)', fontSize: 11 }}
                stroke="rgba(255,255,255,0.06)"
                axisLine={false}
                tickLine={false}
                allowDecimals={false}
              />
              <YAxis
                type="category"
                dataKey="name"
                width={140}
                tick={{ fill: 'rgba(244,244,249,0.55)', fontSize: 11 }}
                stroke="rgba(255,255,255,0.06)"
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                contentStyle={{
                  background:
                    'linear-gradient(135deg, rgba(10, 20, 48, 0.95), rgba(10, 20, 48, 0.85))',
                  backdropFilter: 'blur(16px)',
                  border: '1px solid rgba(255,255,255,0.1)',
                  borderRadius: 12,
                  fontSize: 12,
                  boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
                }}
                cursor={{ fill: 'rgba(255,255,255,0.04)' }}
                formatter={(value: number) => [value, 'Occurrences']}
              />
              <Bar dataKey="count" radius={[0, 4, 4, 0]} maxBarSize={22}>
                {chartData.map((entry, index) => (
                  <Cell
                    key={`cell-${index}`}
                    fill={SEVERITY_BAR_FILL[entry.severity] ?? SEVERITY_BAR_FILL.info}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ---- Page ----

export default function GovernancePage() {
  const { data: envelope, isLoading, error, mutate } = useGovernanceDigest();

  const digest = envelope?.data;
  const patterns = digest?.recurring_patterns ?? [];
  const recommendations = digest?.recommendations ?? [];
  const totalSignals = digest?.total_signals_processed ?? 0;
  const singleOccurrences = digest?.single_occurrence_count ?? 0;

  const degradedReasons = envelope?.degraded
    ? (envelope.degraded_reasons ?? ['Governance digest degraded'])
    : error
    ? ['Failed to load governance digest — check the dashboard server']
    : [];

  const blockerRecs = recommendations.filter(r => r.severity === 'blocker');
  const warnRecs = recommendations.filter(r => r.severity === 'warn');

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
              Governance Digest
            </h2>
            <p style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
              Recurring failure patterns and advisory recommendations
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <FreshnessBadge
            staleness_seconds={envelope?.staleness_seconds ?? undefined}
            queried_at={envelope?.queried_at}
          />
          <button
            onClick={() => mutate()}
            aria-label="Refresh governance digest"
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

      {/* Degraded / error banner */}
      <DegradedBanner reasons={degradedReasons} view="GovernanceDigestView" />

      {/* KPI strip */}
      {!isLoading && digest && (
        <div
          data-testid="kpi-strip"
          className="grid grid-cols-2 md:grid-cols-4 gap-4 stagger-children"
          style={{ marginBottom: 28 }}
        >
          <div className="glass-card" style={{ padding: '16px 18px' }}>
            <div className="flex items-center gap-2" style={{ marginBottom: 6 }}>
              <ShieldAlert size={12} style={{ color: 'var(--color-accent)' }} />
              <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Total Signals</span>
            </div>
            <div
              data-testid="kpi-total-signals"
              className="kpi-value"
              style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--color-foreground)' }}
            >
              {totalSignals}
            </div>
          </div>

          <div
            className="glass-card"
            style={{
              padding: '16px 18px',
              borderTop:
                patterns.length > 0
                  ? '2px solid rgba(250, 204, 21, 0.4)'
                  : '2px solid rgba(255,255,255,0.06)',
            }}
          >
            <div className="flex items-center gap-2" style={{ marginBottom: 6 }}>
              <TrendingUp size={12} style={{ color: 'var(--color-warning)' }} />
              <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Recurring Patterns</span>
            </div>
            <div
              data-testid="kpi-recurring-patterns"
              className="kpi-value"
              style={{
                fontSize: '1.5rem',
                fontWeight: 700,
                color: patterns.length > 0 ? 'var(--color-warning)' : 'var(--color-muted)',
              }}
            >
              {patterns.length}
            </div>
          </div>

          <div
            className="glass-card"
            style={{
              padding: '16px 18px',
              borderTop:
                blockerRecs.length > 0
                  ? '2px solid rgba(255, 107, 107, 0.5)'
                  : '2px solid rgba(255,255,255,0.06)',
            }}
          >
            <div className="flex items-center gap-2" style={{ marginBottom: 6 }}>
              <AlertTriangle size={12} style={{ color: 'var(--color-error)' }} />
              <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Blocker Recs</span>
            </div>
            <div
              data-testid="kpi-blocker-recs"
              className="kpi-value"
              style={{
                fontSize: '1.5rem',
                fontWeight: 700,
                color: blockerRecs.length > 0 ? 'var(--color-error)' : 'var(--color-muted)',
              }}
            >
              {blockerRecs.length}
            </div>
          </div>

          <div className="glass-card" style={{ padding: '16px 18px' }}>
            <div className="flex items-center gap-2" style={{ marginBottom: 6 }}>
              <CheckCircle2 size={12} style={{ color: 'var(--color-success)' }} />
              <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Single Occurrences</span>
            </div>
            <div
              data-testid="kpi-single-occurrences"
              className="kpi-value"
              style={{ fontSize: '1.5rem', fontWeight: 700, color: 'var(--color-foreground)' }}
            >
              {singleOccurrences}
            </div>
          </div>
        </div>
      )}

      {/* Signal volume chart + Recurrence table side by side on large screens */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr',
          gap: 20,
          marginBottom: 24,
        }}
      >
        <SignalTimelineChart
          patterns={patterns}
          totalSignals={totalSignals}
          isLoading={isLoading}
        />
      </div>

      {/* Recurrence table */}
      <div style={{ marginBottom: 24 }}>
        <RecurrenceTable patterns={patterns} isLoading={isLoading} />
      </div>

      {/* Recommendations */}
      <div>
        <div className="flex items-center gap-2" style={{ marginBottom: 16 }}>
          <Info size={15} style={{ color: 'var(--color-accent)' }} />
          <h3
            style={{
              fontSize: 14,
              fontWeight: 700,
              color: 'var(--color-foreground)',
              letterSpacing: '-0.01em',
            }}
          >
            Recommendations
          </h3>
          {!isLoading && (
            <span
              style={{
                fontSize: 11,
                color: 'var(--color-muted)',
                background: 'rgba(255,255,255,0.05)',
                borderRadius: 20,
                padding: '1px 8px',
              }}
            >
              {recommendations.length}
            </span>
          )}
          <span
            style={{
              fontSize: 10,
              color: 'rgba(107, 138, 230, 0.8)',
              background: 'rgba(107, 138, 230, 0.1)',
              border: '1px solid rgba(107, 138, 230, 0.25)',
              borderRadius: 5,
              padding: '2px 8px',
              fontWeight: 600,
              letterSpacing: '0.04em',
            }}
          >
            Advisory Only — T0 decides whether to act
          </span>
        </div>

        {isLoading ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <SkeletonRow />
            <SkeletonRow />
          </div>
        ) : recommendations.length === 0 ? (
          <EmptySection label="No recommendations at this time." />
        ) : (
          <div
            data-testid="recommendations-list"
            style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
          >
            {/* Blockers first */}
            {blockerRecs.map((rec, i) => (
              <RecommendationCard key={`blocker-${i}`} rec={rec} />
            ))}
            {/* Warnings */}
            {warnRecs.map((rec, i) => (
              <RecommendationCard key={`warn-${i}`} rec={rec} />
            ))}
            {/* Info */}
            {recommendations
              .filter(r => r.severity !== 'blocker' && r.severity !== 'warn')
              .map((rec, i) => (
                <RecommendationCard key={`info-${i}`} rec={rec} />
              ))}
          </div>
        )}
      </div>
    </div>
  );
}
