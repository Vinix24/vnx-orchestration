'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import { subDays, format } from 'date-fns';
import { useTokenStats } from '@/lib/hooks';
import PeriodSelector from '@/components/period-selector';
import TerminalFilter from '@/components/terminal-filter';
import { weightedAverage } from '@/lib/metrics';

const ModelDonut = dynamic(() => import('@/components/charts/model-donut'), { ssr: false });
import { MODEL_COLORS } from '@/lib/types';
import type { GroupBy, TokenStats } from '@/lib/types';
import { Cpu, Zap, Database, TrendingUp, Hash } from 'lucide-react';

const ALL_TERMINALS = new Set(['T-MANAGER', 'T0', 'T1', 'T2', 'T3', 'unknown']);

function ModelKPICard({ model, rows }: { model: string; rows: TokenStats[] }) {
  const sessions = rows.reduce((s, r) => s + r.sessions, 0);
  const apiCalls = rows.reduce((s, r) => s + r.api_calls, 0);
  const totalOutput = rows.reduce((s, r) => s + r.total_output_tokens, 0);
  const avgContext = weightedAverage(rows, 'context_per_call_K', 'api_calls');
  const avgCache = weightedAverage(rows, 'cache_hit_pct', 'api_calls');
  const avgOutput = weightedAverage(rows, 'output_per_call_K', 'api_calls');
  const color = MODEL_COLORS[model] ?? MODEL_COLORS.unknown;

  const stats = [
    { icon: Cpu, label: 'Sessions', value: sessions.toLocaleString() },
    { icon: Zap, label: 'API Calls', value: apiCalls.toLocaleString() },
    { icon: Database, label: 'Avg Context/Call', value: `${avgContext.toFixed(1)}K` },
    {
      icon: TrendingUp,
      label: 'Cache Hit %',
      value: `${avgCache.toFixed(1)}%`,
      valueColor:
        avgCache >= 95
          ? 'var(--color-success)'
          : avgCache >= 90
          ? 'var(--color-warning)'
          : 'var(--color-error)',
    },
    { icon: Hash, label: 'Avg Output/Call', value: `${avgOutput.toFixed(2)}K` },
    { icon: Hash, label: 'Total Output', value: `${(totalOutput / 1000).toFixed(1)}K` },
  ];

  return (
    <div
      className="glass-card"
      style={{
        padding: '28px',
        boxShadow: `0 4px 24px ${color}10`,
      }}
    >
      <div className="flex items-center gap-3 mb-6">
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 12,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: `linear-gradient(135deg, ${color}20, ${color}08)`,
          }}
        >
          <Cpu size={22} style={{ color }} />
        </div>
        <div>
          <h3 className="text-base font-semibold" style={{ color, letterSpacing: '-0.01em' }}>
            {model}
          </h3>
          <p className="text-xs" style={{ color: 'var(--color-muted)', marginTop: 2 }}>
            {sessions} sessions total
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-5">
        {stats.map((s) => (
          <div key={s.label}>
            <div className="flex items-center gap-1 mb-1">
              <s.icon size={12} style={{ color: 'var(--color-muted)', opacity: 0.7 }} />
              <span className="text-xs" style={{ color: 'var(--color-muted)' }}>{s.label}</span>
            </div>
            <div
              className="kpi-value-sm"
              style={{ color: s.valueColor ?? 'var(--color-foreground)' }}
            >
              {s.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function ModelsPage() {
  const [from, setFrom] = useState(() => format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [to, setTo] = useState(() => format(new Date(), 'yyyy-MM-dd'));
  const [group, setGroup] = useState<GroupBy>('day');
  const [terminals, setTerminals] = useState<Set<string>>(ALL_TERMINALS);

  const { data: raw, error, isLoading } = useTokenStats(from, to, group);
  const data = raw?.filter((r) => terminals.has(r.terminal));

  const modelGroups = new Map<string, TokenStats[]>();
  if (data) {
    for (const row of data) {
      const existing = modelGroups.get(row.model) ?? [];
      existing.push(row);
      modelGroups.set(row.model, existing);
    }
  }

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Model Performance</h2>
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
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 stagger-children" style={{ marginBottom: 24 }}>
            {Array.from(modelGroups.entries())
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([model, rows]) => (
                <ModelKPICard key={model} model={model} rows={rows} />
              ))}
          </div>

          <ModelDonut data={data} />
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
