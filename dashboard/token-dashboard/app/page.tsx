'use client';

import { useState } from 'react';
import Link from 'next/link';
import dynamic from 'next/dynamic';
import { subDays, format } from 'date-fns';
import { useTokenStats } from '@/lib/hooks';
import { usePolling } from '@/lib/use-polling';
import PeriodSelector from '@/components/period-selector';
import TerminalFilter from '@/components/terminal-filter';
import KPICards from '@/components/kpi-cards';
import type { GroupBy, DashboardStatus } from '@/lib/types';
import { normalizeTerminalStatus } from '@/lib/utils';
import { AlertCircle, Monitor, GitBranch, Lightbulb } from 'lucide-react';

const ApiCallsChart = dynamic(() => import('@/components/charts/api-calls-chart'), { ssr: false });
const ModelDonut = dynamic(() => import('@/components/charts/model-donut'), { ssr: false });
const CacheEfficiency = dynamic(() => import('@/components/charts/cache-efficiency'), { ssr: false });

const ALL_TERMINALS = new Set(['T-MANAGER', 'T0', 'T1', 'T2', 'T3', 'unknown']);

function HealthBanner({ status }: { status: DashboardStatus }) {
  const processEntries = Object.entries(status.processes ?? {});
  const allRunning = processEntries.length > 0 && processEntries.every(([, p]) => p.running);
  const blockerCount = status.open_items?.summary?.blocker_count ?? 0;
  const activeTerminals = Object.entries(status.terminals ?? {}).filter(
    ([id, t]) => normalizeTerminalStatus(t.status, id) === 'working'
  ).length;

  const isGreen = allRunning && blockerCount === 0;
  const isRed = !allRunning || blockerCount > 0;
  const color = isGreen ? '#50fa7b' : isRed ? '#ff6b6b' : '#facc15';
  const label = isGreen ? 'All Systems Operational' : isRed ? 'Issues Detected' : 'Degraded';

  return (
    <div
      className="animate-in"
      style={{
        padding: '12px 20px',
        borderRadius: 12,
        border: `1px solid ${color}40`,
        background: `${color}08`,
        marginBottom: 20,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}
    >
      <span
        style={{
          width: 10,
          height: 10,
          borderRadius: '50%',
          background: color,
          boxShadow: `0 0 10px ${color}60`,
          display: 'inline-block',
        }}
      />
      <span className="text-sm font-semibold" style={{ color }}>{label}</span>
      <span className="text-xs" style={{ color: 'var(--color-muted)', marginLeft: 'auto' }}>
        {processEntries.filter(([, p]) => p.running).length}/{processEntries.length} processes ·
        {' '}{activeTerminals} active terminals ·
        {' '}{blockerCount} blockers
      </span>
    </div>
  );
}

export default function OverviewPage() {
  const [from, setFrom] = useState(() => format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [to, setTo] = useState(() => format(new Date(), 'yyyy-MM-dd'));
  const [group, setGroup] = useState<GroupBy>('day');
  const [terminals, setTerminals] = useState<Set<string>>(ALL_TERMINALS);

  const { data: raw, error, isLoading } = useTokenStats(from, to, group);
  const data = raw?.filter((r) => terminals.has(r.terminal));
  const { data: dashboardStatus } = usePolling<DashboardStatus>('/state/dashboard_status.json');

  const blockerCount = dashboardStatus?.open_items?.summary?.blocker_count ?? 0;
  const activeTerminals = dashboardStatus
    ? Object.values(dashboardStatus.terminals ?? {}).filter((t) => normalizeTerminalStatus(t.status) === 'working').length
    : 0;
  const processEntries = Object.entries(dashboardStatus?.processes ?? {});
  const runningProcesses = processEntries.filter(([, p]) => p.running).length;
  const recommendations = dashboardStatus?.recommendations ?? [];

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Overview</h2>
      </div>

      {/* System Health Banner */}
      {dashboardStatus && <HealthBanner status={dashboardStatus} />}

      {/* System KPI Cards */}
      {dashboardStatus && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6 stagger-children">
          <Link href="/open-items" className="glass-card" style={{ padding: 20, textDecoration: 'none' }}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-medium" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                Open Items
              </span>
              <AlertCircle size={16} style={{ color: blockerCount > 0 ? '#ff6b6b' : '#60a5fa' }} />
            </div>
            <div className="kpi-value" style={{ color: blockerCount > 0 ? '#ff6b6b' : 'var(--color-foreground)' }}>
              {dashboardStatus.open_items?.summary?.open_count ?? 0}
            </div>
            <div className="text-xs mt-1" style={{ color: 'var(--color-muted)' }}>
              {blockerCount} blockers · {dashboardStatus.open_items?.summary?.warn_count ?? 0} warnings
            </div>
          </Link>

          <Link href="/pr-queue" className="glass-card" style={{ padding: 20, textDecoration: 'none' }}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-medium" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                PR Queue
              </span>
              <GitBranch size={16} style={{ color: '#50fa7b' }} />
            </div>
            <div className="kpi-value" style={{ color: 'var(--color-foreground)' }}>
              {dashboardStatus.pr_queue?.completed_prs ?? 0}/{dashboardStatus.pr_queue?.total_prs ?? 0}
            </div>
            <div className="text-xs mt-1" style={{ color: 'var(--color-muted)' }}>
              {dashboardStatus.pr_queue?.progress_percent ?? 0}% complete
            </div>
          </Link>

          <Link href="/terminals" className="glass-card" style={{ padding: 20, textDecoration: 'none' }}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-medium" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                Active Terminals
              </span>
              <Monitor size={16} style={{ color: '#f97316' }} />
            </div>
            <div className="kpi-value" style={{ color: '#f97316' }}>
              {activeTerminals}
            </div>
            <div className="text-xs mt-1" style={{ color: 'var(--color-muted)' }}>
              of {Object.keys(dashboardStatus.terminals ?? {}).length} total
            </div>
          </Link>

          <Link href="/system" className="glass-card" style={{ padding: 20, textDecoration: 'none' }}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-medium" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                Process Health
              </span>
              <Monitor size={16} style={{ color: runningProcesses === processEntries.length ? '#50fa7b' : '#ff6b6b' }} />
            </div>
            <div className="kpi-value" style={{ color: runningProcesses === processEntries.length ? '#50fa7b' : '#ff6b6b' }}>
              {runningProcesses}/{processEntries.length}
            </div>
            <div className="text-xs mt-1" style={{ color: 'var(--color-muted)' }}>
              processes running
            </div>
          </Link>
        </div>
      )}

      {/* Recommendations */}
      {recommendations.length > 0 && (
        <div className="glass-card animate-in" style={{ padding: 20, marginBottom: 20 }}>
          <div className="flex items-center gap-2 mb-3">
            <Lightbulb size={16} style={{ color: '#facc15' }} />
            <h3 className="text-xs font-medium" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
              Recommendations
            </h3>
          </div>
          <div style={{ display: 'grid', gap: 6 }}>
            {recommendations.map((rec, i) => (
              <div key={i} className="text-sm" style={{ color: 'var(--color-foreground)', lineHeight: 1.5 }}>
                {rec}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Token Analytics Section */}
      <div className="section-header" style={{ marginTop: 12 }}>
        <div className="accent-bar" />
        <h2>Token Analytics</h2>
      </div>

      <PeriodSelector
        from={from}
        to={to}
        group={group}
        onFromChange={setFrom}
        onToChange={setTo}
        onGroupChange={setGroup}
      />

      <TerminalFilter selected={terminals} onChange={setTerminals} />

      {error && (
        <div
          className="glass-card"
          style={{
            padding: '16px 20px',
            marginBottom: 24,
            borderColor: 'var(--color-error)',
            color: 'var(--color-error)',
            fontSize: 14,
          }}
        >
          Failed to load data. Ensure the API server is running at localhost:4173.
        </div>
      )}

      {isLoading && (
        <div className="flex items-center justify-center py-20">
          <div
            className="animate-spin w-8 h-8 border-2 rounded-full"
            style={{
              borderColor: 'var(--color-card-border)',
              borderTopColor: 'var(--color-accent)',
            }}
          />
        </div>
      )}

      {data && data.length > 0 && (
        <>
          <KPICards data={data} />

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-5" style={{ marginTop: 24 }}>
            <div className="lg:col-span-2">
              <ApiCallsChart data={data} />
            </div>
            <div>
              <ModelDonut data={data} />
            </div>
          </div>

          <div style={{ marginTop: 24 }}>
            <CacheEfficiency data={data} />
          </div>
        </>
      )}

      {data && data.length === 0 && (
        <div
          className="glass-card"
          style={{
            padding: '40px',
            textAlign: 'center',
            color: 'var(--color-muted)',
            fontSize: 14,
          }}
        >
          No data available for the selected period and terminals.
        </div>
      )}
    </div>
  );
}
