'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import { subDays, format } from 'date-fns';
import { useTokenStats } from '@/lib/hooks';
import PeriodSelector from '@/components/period-selector';
import TerminalFilter from '@/components/terminal-filter';
import KPICards from '@/components/kpi-cards';
import type { GroupBy } from '@/lib/types';

const ApiCallsChart = dynamic(() => import('@/components/charts/api-calls-chart'), { ssr: false });
const ModelDonut = dynamic(() => import('@/components/charts/model-donut'), { ssr: false });
const CacheEfficiency = dynamic(() => import('@/components/charts/cache-efficiency'), { ssr: false });

const ALL_TERMINALS = new Set(['T-MANAGER', 'T0', 'T1', 'T2', 'T3', 'unknown']);

export default function OverviewPage() {
  const [from, setFrom] = useState(() => format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [to, setTo] = useState(() => format(new Date(), 'yyyy-MM-dd'));
  const [group, setGroup] = useState<GroupBy>('day');
  const [terminals, setTerminals] = useState<Set<string>>(ALL_TERMINALS);

  const { data: raw, error, isLoading } = useTokenStats(from, to, group);
  const data = raw?.filter((r) => terminals.has(r.terminal));

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Overview</h2>
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
