'use client';

import { useState } from 'react';
import { RefreshCw, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { useAggregateOpenItems, useProjects } from '@/lib/hooks';
import DegradedBanner from '@/components/operator/degraded-banner';
import FreshnessBadge from '@/components/operator/freshness-badge';
import OpenItemsList from '@/components/operator/open-items-list';

function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-16">
      <div
        className="animate-spin w-8 h-8 border-2 rounded-full"
        style={{
          borderColor: 'var(--color-card-border)',
          borderTopColor: 'var(--color-accent)',
        }}
      />
    </div>
  );
}

export default function OpenItemsPage() {
  const [projectFilter, setProjectFilter] = useState<string | undefined>(undefined);
  const { data: aggregateEnv, isLoading, mutate } = useAggregateOpenItems(projectFilter);
  const { data: projectsEnv } = useProjects();

  const projects = projectsEnv?.data ?? [];
  const data = aggregateEnv?.data;
  const items = data?.items ?? [];
  const summary = data?.total_summary ?? { blocker_count: 0, warn_count: 0, info_count: 0 };
  const perProject = data?.per_project_subtotals ?? {};
  const degradedReasons = aggregateEnv?.degraded
    ? (aggregateEnv.degraded_reasons ?? ['Aggregate open items view degraded'])
    : [];

  const totalOpen = summary.blocker_count + summary.warn_count + summary.info_count;

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

      {/* Project filter chips */}
      {projects.length > 0 && (
        <div className="flex items-center gap-2" style={{ marginBottom: 20, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>Filter:</span>
          <button
            onClick={() => setProjectFilter(undefined)}
            style={{
              padding: '4px 12px',
              borderRadius: 20,
              fontSize: 11,
              fontWeight: projectFilter === undefined ? 600 : 400,
              background: projectFilter === undefined ? 'rgba(249, 115, 22, 0.15)' : 'rgba(255,255,255,0.04)',
              border: `1px solid ${projectFilter === undefined ? 'rgba(249, 115, 22, 0.4)' : 'rgba(255,255,255,0.08)'}`,
              color: projectFilter === undefined ? 'var(--color-accent)' : 'var(--color-muted)',
              cursor: 'pointer',
            }}
          >
            All projects
          </button>
          {projects.map(p => {
            const sub = perProject[p.name];
            const hasBlockers = (sub?.blocker_count ?? 0) > 0;
            const hasWarns = (sub?.warn_count ?? 0) > 0;
            const dotColor = hasBlockers
              ? 'var(--color-error)'
              : hasWarns
              ? 'var(--color-warning)'
              : 'var(--color-success)';
            return (
              <button
                key={p.name}
                onClick={() => setProjectFilter(projectFilter === p.name ? undefined : p.name)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '4px 12px',
                  borderRadius: 20,
                  fontSize: 11,
                  fontWeight: projectFilter === p.name ? 600 : 400,
                  background: projectFilter === p.name ? 'rgba(107, 138, 230, 0.15)' : 'rgba(255,255,255,0.04)',
                  border: `1px solid ${projectFilter === p.name ? 'rgba(107, 138, 230, 0.4)' : 'rgba(255,255,255,0.08)'}`,
                  color: projectFilter === p.name ? '#6B8AE6' : 'var(--color-muted)',
                  cursor: 'pointer',
                }}
              >
                <div
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    backgroundColor: dotColor,
                  }}
                />
                {p.name}
              </button>
            );
          })}
        </div>
      )}

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
        <LoadingSpinner />
      ) : (
        <div className="glass-card" style={{ padding: '24px' }}>
          <OpenItemsList
            items={items}
            summary={summary}
            emptyLabel={
              projectFilter
                ? `No open items for ${projectFilter}`
                : 'No open items across any registered project.'
            }
          />
        </div>
      )}
    </div>
  );
}
