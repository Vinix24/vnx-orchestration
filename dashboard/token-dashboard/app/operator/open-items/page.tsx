'use client';

import { useState, useMemo } from 'react';
import { RefreshCw, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { useAggregateOpenItems, useProjects } from '@/lib/hooks';
import DegradedBanner from '@/components/operator/degraded-banner';
import FreshnessBadge from '@/components/operator/freshness-badge';
import OpenItemsList from '@/components/operator/open-items-list';
import type { OpenItem } from '@/lib/types';

// Normalized severity groups: blocker|blocking → 'blocker', warn|warning → 'warn', info → 'info'
type SeverityFilter = 'blocker' | 'warn' | 'info';

function normalizeSeverity(sev: string): SeverityFilter {
  if (sev === 'blocker' || sev === 'blocking') return 'blocker';
  if (sev === 'warn' || sev === 'warning') return 'warn';
  return 'info';
}

function LoadingSkeleton() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {[0, 1, 2].map(i => (
        <div
          key={i}
          aria-hidden="true"
          style={{
            height: 64,
            borderRadius: 10,
            background:
              'linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.07) 50%, rgba(255,255,255,0.04) 75%)',
            backgroundSize: '200% 100%',
            animation: 'shimmer 1.6s infinite',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
        />
      ))}
    </div>
  );
}

const SEVERITY_CHIP_CONFIG: Record<SeverityFilter, { label: string; activeColor: string; activeBg: string; activeBorder: string }> = {
  blocker: {
    label: 'Blockers',
    activeColor: 'var(--color-error)',
    activeBg: 'rgba(255, 107, 107, 0.12)',
    activeBorder: 'rgba(255, 107, 107, 0.4)',
  },
  warn: {
    label: 'Warnings',
    activeColor: 'var(--color-warning)',
    activeBg: 'rgba(250, 204, 21, 0.12)',
    activeBorder: 'rgba(250, 204, 21, 0.4)',
  },
  info: {
    label: 'Info',
    activeColor: 'var(--color-muted)',
    activeBg: 'rgba(255,255,255,0.08)',
    activeBorder: 'rgba(255,255,255,0.2)',
  },
};

export default function OpenItemsPage() {
  const [projectFilter, setProjectFilter] = useState<string>('');
  const [severityFilter, setSeverityFilter] = useState<SeverityFilter | null>(null);

  const { data: aggregateEnv, isLoading, mutate } = useAggregateOpenItems(
    projectFilter || undefined
  );
  const { data: projectsEnv } = useProjects();

  const projects = projectsEnv?.data ?? [];
  const data = aggregateEnv?.data;
  const allItems: OpenItem[] = data?.items ?? [];
  const summary = data?.total_summary ?? { blocker_count: 0, warn_count: 0, info_count: 0 };
  const perProject = data?.per_project_subtotals ?? {};
  const degradedReasons = aggregateEnv?.degraded
    ? (aggregateEnv.degraded_reasons ?? ['Aggregate open items view degraded'])
    : [];

  // Per-severity counts from the currently fetched (already project-filtered) items
  const severityCounts = useMemo(() => {
    const counts: Record<SeverityFilter, number> = { blocker: 0, warn: 0, info: 0 };
    for (const item of allItems) {
      counts[normalizeSeverity(item.severity)]++;
    }
    return counts;
  }, [allItems]);

  // Apply client-side severity filter
  const items: OpenItem[] = useMemo(() => {
    if (!severityFilter) return allItems;
    return allItems.filter(item => normalizeSeverity(item.severity) === severityFilter);
  }, [allItems, severityFilter]);

  const totalOpen = summary.blocker_count + summary.warn_count + summary.info_count;

  const emptyLabel = (() => {
    if (severityFilter && allItems.length > 0 && items.length === 0) {
      return `No ${SEVERITY_CHIP_CONFIG[severityFilter].label.toLowerCase()} for${projectFilter ? ` ${projectFilter}` : ' any project'}`;
    }
    if (projectFilter) return `No open items for ${projectFilter}`;
    return 'No open items across any registered project.';
  })();

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
              Open Items
            </h2>
            <p style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
              Aggregate view across all projects
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <FreshnessBadge
            staleness_seconds={aggregateEnv?.staleness_seconds}
            queried_at={aggregateEnv?.queried_at}
          />
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

      {/* Aggregate summary KPIs */}
      {!isLoading && (
        <div className="grid grid-cols-3 gap-4 stagger-children" style={{ marginBottom: 28 }}>
          <div
            className="glass-card"
            style={{
              padding: '18px 20px',
              borderTop: summary.blocker_count > 0
                ? '2px solid rgba(255, 107, 107, 0.5)'
                : '2px solid rgba(255,255,255,0.06)',
            }}
          >
            <div className="flex items-center gap-2" style={{ marginBottom: 8 }}>
              <AlertTriangle size={13} style={{ color: 'var(--color-error)' }} />
              <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Blockers</span>
            </div>
            <div
              className="kpi-value"
              style={{ color: summary.blocker_count > 0 ? 'var(--color-error)' : 'var(--color-muted)' }}
            >
              {summary.blocker_count}
            </div>
          </div>

          <div
            className="glass-card"
            style={{
              padding: '18px 20px',
              borderTop: summary.warn_count > 0
                ? '2px solid rgba(250, 204, 21, 0.4)'
                : '2px solid rgba(255,255,255,0.06)',
            }}
          >
            <div className="flex items-center gap-2" style={{ marginBottom: 8 }}>
              <AlertTriangle size={13} style={{ color: 'var(--color-warning)' }} />
              <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Warnings</span>
            </div>
            <div
              className="kpi-value"
              style={{ color: summary.warn_count > 0 ? 'var(--color-warning)' : 'var(--color-muted)' }}
            >
              {summary.warn_count}
            </div>
          </div>

          <div className="glass-card" style={{ padding: '18px 20px', borderTop: '2px solid rgba(255,255,255,0.06)' }}>
            <div className="flex items-center gap-2" style={{ marginBottom: 8 }}>
              <CheckCircle2 size={13} style={{ color: 'var(--color-success)' }} />
              <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>Total Open</span>
            </div>
            <div className="kpi-value" style={{ color: 'var(--color-foreground)' }}>
              {totalOpen}
            </div>
          </div>
        </div>
      )}

      {/* Filter row: project dropdown + severity chips */}
      <div
        data-testid="filter-row"
        className="flex items-center gap-4"
        style={{ marginBottom: 20, flexWrap: 'wrap' }}
      >
        {/* Project dropdown */}
        {projects.length > 0 && (
          <div className="flex items-center gap-2">
            <label
              htmlFor="project-select"
              style={{ fontSize: 12, color: 'var(--color-muted)', whiteSpace: 'nowrap' }}
            >
              Project:
            </label>
            <select
              id="project-select"
              data-testid="project-dropdown"
              value={projectFilter}
              onChange={e => {
                setProjectFilter(e.target.value);
                setSeverityFilter(null);
              }}
              style={{
                padding: '5px 10px',
                borderRadius: 8,
                fontSize: 12,
                background: 'rgba(10, 20, 48, 0.9)',
                border: projectFilter
                  ? '1px solid rgba(107, 138, 230, 0.4)'
                  : '1px solid rgba(255,255,255,0.12)',
                color: projectFilter ? '#6B8AE6' : 'var(--color-foreground)',
                cursor: 'pointer',
                outline: 'none',
              }}
            >
              <option value="">All projects</option>
              {projects.map(p => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* Severity filter chips */}
        {!isLoading && (
          <div
            data-testid="severity-filter"
            className="flex items-center gap-2"
            style={{ flexWrap: 'wrap' }}
          >
            <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>Severity:</span>
            {(Object.entries(SEVERITY_CHIP_CONFIG) as [SeverityFilter, typeof SEVERITY_CHIP_CONFIG[SeverityFilter]][]).map(
              ([sev, cfg]) => {
                const count = severityCounts[sev];
                const isActive = severityFilter === sev;
                return (
                  <button
                    key={sev}
                    data-testid={`severity-chip-${sev}`}
                    onClick={() => setSeverityFilter(isActive ? null : sev)}
                    aria-pressed={isActive}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 5,
                      padding: '4px 10px',
                      borderRadius: 20,
                      fontSize: 11,
                      fontWeight: isActive ? 700 : 400,
                      background: isActive ? cfg.activeBg : 'rgba(255,255,255,0.04)',
                      border: `1px solid ${isActive ? cfg.activeBorder : 'rgba(255,255,255,0.08)'}`,
                      color: isActive ? cfg.activeColor : 'var(--color-muted)',
                      cursor: 'pointer',
                    }}
                  >
                    {cfg.label}
                    <span
                      data-testid={`severity-count-${sev}`}
                      style={{
                        fontSize: 10,
                        fontWeight: 700,
                        minWidth: 14,
                        textAlign: 'center',
                        padding: '0 3px',
                        borderRadius: 6,
                        background: isActive ? 'rgba(255,255,255,0.15)' : 'rgba(255,255,255,0.06)',
                        color: isActive ? cfg.activeColor : 'rgba(244,244,249,0.5)',
                      }}
                    >
                      {count}
                    </span>
                  </button>
                );
              }
            )}
            {severityFilter && (
              <button
                data-testid="severity-clear"
                onClick={() => setSeverityFilter(null)}
                style={{
                  fontSize: 11,
                  color: 'var(--color-muted)',
                  background: 'none',
                  border: 'none',
                  cursor: 'pointer',
                  padding: '4px 6px',
                  borderRadius: 6,
                  textDecoration: 'underline',
                  textDecorationColor: 'rgba(244,244,249,0.3)',
                }}
              >
                Clear
              </button>
            )}
          </div>
        )}
      </div>

      {/* Degraded banner */}
      <DegradedBanner reasons={degradedReasons} view="AggregateOpenItemsView" />

      {/* Per-project subtotal chips when viewing all */}
      {!projectFilter && Object.keys(perProject).length > 0 && (
        <div
          style={{
            display: 'flex',
            gap: 8,
            marginBottom: 20,
            flexWrap: 'wrap',
          }}
        >
          {Object.entries(perProject).map(([name, sub]) => (
            <div
              key={name}
              style={{
                padding: '5px 12px',
                borderRadius: 8,
                fontSize: 11,
                background: sub.status === 'unavailable' ? 'rgba(255, 107, 107, 0.06)' : 'rgba(255,255,255,0.04)',
                border: `1px solid ${sub.blocker_count > 0 ? 'rgba(255, 107, 107, 0.3)' : 'rgba(255,255,255,0.08)'}`,
                color: 'var(--color-muted)',
              }}
            >
              <span style={{ fontWeight: 600, color: 'var(--color-foreground)' }}>{name}</span>
              {sub.status === 'unavailable' ? (
                <span style={{ marginLeft: 6, color: 'var(--color-error)' }}>unavailable</span>
              ) : (
                <>
                  {sub.blocker_count > 0 && (
                    <span style={{ marginLeft: 6, color: 'var(--color-error)', fontWeight: 600 }}>
                      {sub.blocker_count}B
                    </span>
                  )}
                  {sub.warn_count > 0 && (
                    <span style={{ marginLeft: 4, color: 'var(--color-warning)' }}>
                      {sub.warn_count}W
                    </span>
                  )}
                  {sub.blocker_count === 0 && sub.warn_count === 0 && (
                    <span style={{ marginLeft: 6, color: 'var(--color-success)' }}>clear</span>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Items list */}
      {isLoading ? (
        <div className="glass-card" style={{ padding: '24px' }}>
          <LoadingSkeleton />
        </div>
      ) : (
        <div className="glass-card" style={{ padding: '24px' }}>
          <OpenItemsList
            items={items}
            summary={summary}
            emptyLabel={emptyLabel}
          />
        </div>
      )}
    </div>
  );
}
