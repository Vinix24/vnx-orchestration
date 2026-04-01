'use client';

import { useState } from 'react';
import { RefreshCw, LayoutGrid, Activity } from 'lucide-react';
import { useProjects, useTerminals, useOperatorSession } from '@/lib/hooks';
import type { ActionOutcome } from '@/lib/types';
import ProjectCard from '@/components/operator/project-card';
import TerminalStatusCard from '@/components/operator/terminal-status-card';
import DegradedBanner from '@/components/operator/degraded-banner';
import FreshnessBadge from '@/components/operator/freshness-badge';
import ActionToast from '@/components/operator/action-toast';

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

function EmptyState({ label }: { label: string }) {
  return (
    <div
      style={{
        padding: '40px 20px',
        textAlign: 'center',
        color: 'var(--color-muted)',
        fontSize: 13,
        background: 'rgba(255,255,255,0.02)',
        borderRadius: 12,
        border: '1px dashed rgba(255,255,255,0.08)',
      }}
    >
      {label}
    </div>
  );
}

export default function OperatorPage() {
  const { data: projectsEnv, isLoading: projLoading, mutate: mutateProjects } = useProjects();
  const { data: terminalsEnv, isLoading: termLoading, mutate: mutateTerminals } = useTerminals();
  const { data: sessionEnv } = useOperatorSession();
  const [lastOutcome, setLastOutcome] = useState<ActionOutcome | null>(null);

  function handleRefresh() {
    mutateProjects();
    mutateTerminals();
  }

  const projects = projectsEnv?.data ?? [];
  const terminals = terminalsEnv?.data ?? [];
  const degradedProjects = projectsEnv?.degraded
    ? (projectsEnv.degraded_reasons ?? ['Projects view degraded'])
    : [];
  const degradedTerminals = terminalsEnv?.degraded
    ? (terminalsEnv.degraded_reasons ?? ['Terminals view degraded'])
    : [];

  // Session PR progress summary
  const prProgress = sessionEnv?.data?.pr_progress ?? [];
  const activePRs = prProgress.filter(p => p.status !== 'merged' && p.status !== 'done');

  return (
    <div>
      {/* Page header */}
      <div className="flex items-center justify-between" style={{ marginBottom: 24 }}>
        <div className="flex items-center gap-3">
          <div className="accent-bar" style={{ height: 28, width: 4, borderRadius: 2, background: 'var(--color-accent)' }} />
          <div>
            <h2
              style={{
                fontSize: '1.5rem',
                fontWeight: 700,
                letterSpacing: '-0.02em',
                color: 'var(--color-foreground)',
              }}
            >
              Operator Control
            </h2>
            {sessionEnv?.data?.feature_name && (
              <p style={{ fontSize: 12, color: 'var(--color-accent)', marginTop: 2 }}>
                {sessionEnv.data.feature_name}
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <FreshnessBadge
            staleness_seconds={Math.max(
              projectsEnv?.staleness_seconds ?? 0,
              terminalsEnv?.staleness_seconds ?? 0,
            )}
            queried_at={projectsEnv?.queried_at}
          />
          <button
            onClick={handleRefresh}
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

      {/* Active PR summary strip */}
      {activePRs.length > 0 && (
        <div
          style={{
            display: 'flex',
            gap: 8,
            marginBottom: 24,
            flexWrap: 'wrap',
          }}
        >
          {activePRs.map(pr => (
            <div
              key={pr.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '6px 12px',
                borderRadius: 20,
                background: 'rgba(107, 138, 230, 0.10)',
                border: '1px solid rgba(107, 138, 230, 0.25)',
              }}
            >
              <span style={{ fontSize: 11, fontWeight: 700, color: '#6B8AE6' }}>{pr.id}</span>
              {pr.title && (
                <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>
                  {pr.title.length > 40 ? pr.title.slice(0, 40) + '…' : pr.title}
                </span>
              )}
              {pr.status && (
                <span
                  style={{
                    fontSize: 10,
                    padding: '1px 6px',
                    borderRadius: 4,
                    background: 'rgba(107, 138, 230, 0.15)',
                    color: '#6B8AE6',
                  }}
                >
                  {pr.status}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Degraded banners */}
      <DegradedBanner reasons={degradedProjects} view="ProjectsView" />
      <DegradedBanner reasons={degradedTerminals} view="TerminalView" />

      {/* Projects section */}
      <div style={{ marginBottom: 40 }}>
        <div className="flex items-center gap-2" style={{ marginBottom: 16 }}>
          <LayoutGrid size={16} style={{ color: 'var(--color-accent)' }} />
          <h3
            style={{
              fontSize: 14,
              fontWeight: 700,
              color: 'var(--color-foreground)',
              letterSpacing: '-0.01em',
            }}
          >
            Projects
          </h3>
          {!projLoading && (
            <span
              style={{
                fontSize: 11,
                color: 'var(--color-muted)',
                background: 'rgba(255,255,255,0.05)',
                borderRadius: 20,
                padding: '1px 8px',
              }}
            >
              {projects.length}
            </span>
          )}
        </div>

        {projLoading ? (
          <LoadingSpinner />
        ) : projects.length === 0 ? (
          <EmptyState label="No projects registered. Register a project to get started." />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 stagger-children">
            {projects
              .sort((a, b) => {
                const order = { critical: 0, warning: 1, clear: 2 };
                return (order[a.attention_level] ?? 2) - (order[b.attention_level] ?? 2);
              })
              .map(proj => (
                <ProjectCard
                  key={proj.path}
                  project={proj}
                  onActionComplete={outcome => {
                    setLastOutcome(outcome);
                    setTimeout(() => mutateProjects(), 1500);
                  }}
                />
              ))}
          </div>
        )}
      </div>

      {/* Terminals section */}
      <div>
        <div className="flex items-center gap-2" style={{ marginBottom: 16 }}>
          <Activity size={16} style={{ color: 'var(--color-accent)' }} />
          <h3
            style={{
              fontSize: 14,
              fontWeight: 700,
              color: 'var(--color-foreground)',
              letterSpacing: '-0.01em',
            }}
          >
            Terminal State
          </h3>
          {!termLoading && (
            <span
              style={{
                fontSize: 11,
                color: 'var(--color-muted)',
                background: 'rgba(255,255,255,0.05)',
                borderRadius: 20,
                padding: '1px 8px',
              }}
            >
              {terminals.length}
            </span>
          )}
        </div>

        {termLoading ? (
          <LoadingSpinner />
        ) : terminals.length === 0 ? (
          <EmptyState label="No terminals registered. Start a VNX session to see terminal state." />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 stagger-children">
            {terminals
              .sort((a, b) => a.terminal_id.localeCompare(b.terminal_id))
              .map(t => (
                <TerminalStatusCard key={t.terminal_id} terminal={t} />
              ))}
          </div>
        )}
      </div>

      <ActionToast outcome={lastOutcome} onDismiss={() => setLastOutcome(null)} />
    </div>
  );
}
