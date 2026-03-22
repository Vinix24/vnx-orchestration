'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import { subDays, format } from 'date-fns';
import { useTokenStats } from '@/lib/hooks';
import PeriodSelector from '@/components/period-selector';
import TerminalFilter from '@/components/terminal-filter';
import { weightedAverage } from '@/lib/metrics';

const TerminalComparison = dynamic(() => import('@/components/charts/terminal-comparison'), { ssr: false });
import { TERMINAL_COLORS } from '@/lib/types';
import type { GroupBy, TokenStats } from '@/lib/types';
import { Monitor, Zap, Database, TrendingUp } from 'lucide-react';

const ALL_TERMINALS = new Set(['T-MANAGER', 'T0', 'T1', 'T2', 'T3', 'unknown']);

function TerminalMiniCard({ terminal, rows }: { terminal: string; rows: TokenStats[] }) {
  const sessions = rows.reduce((s, r) => s + r.sessions, 0);
  const apiCalls = rows.reduce((s, r) => s + r.api_calls, 0);
  const avgContext = weightedAverage(rows, 'context_per_call_K', 'api_calls');
  const avgCache = weightedAverage(rows, 'cache_hit_pct', 'api_calls');
  const color = TERMINAL_COLORS[terminal] ?? TERMINAL_COLORS.unknown;

  const stats = [
    { icon: Monitor, label: 'Sessions', value: String(sessions) },
    { icon: Zap, label: 'API Calls', value: apiCalls.toLocaleString() },
    { icon: Database, label: 'Ctx/Call', value: `${avgContext.toFixed(1)}K` },
    {
      icon: TrendingUp,
      label: 'Cache %',
      value: `${avgCache.toFixed(1)}%`,
      valueColor:
        avgCache >= 95
          ? 'var(--color-success)'
          : avgCache >= 90
          ? 'var(--color-warning)'
          : 'var(--color-error)',
    },
  ];

  return (
    <div
      className="glass-card"
      style={{
        padding: '24px',
        boxShadow: `0 4px 20px ${color}08`,
        borderLeft: `3px solid ${color}60`,
      }}
    >
      <div className="flex items-center gap-3 mb-5">
        <div
          style={{
            width: 10,
            height: 10,
            borderRadius: '50%',
            backgroundColor: color,
            boxShadow: `0 0 8px ${color}50`,
          }}
        />
        <h4 className="text-sm font-semibold" style={{ color }}>{terminal}</h4>
      </div>
      <div className="grid grid-cols-2 gap-4">
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

export default function TerminalsPage() {
  const [from, setFrom] = useState(() => format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [to, setTo] = useState(() => format(new Date(), 'yyyy-MM-dd'));
  const [group, setGroup] = useState<GroupBy>('day');
  const [terminals, setTerminals] = useState<Set<string>>(ALL_TERMINALS);

  const { data: raw, error, isLoading } = useTokenStats(from, to, group);
  const data = raw?.filter((r) => terminals.has(r.terminal));

  const terminalGroups = new Map<string, TokenStats[]>();
  if (data) {
    for (const row of data) {
      const existing = terminalGroups.get(row.terminal) ?? [];
      existing.push(row);
      terminalGroups.set(row.terminal, existing);
    }
  }

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Terminal Comparison</h2>
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
          <div style={{ marginBottom: 24 }}>
            <TerminalComparison data={data} />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5 stagger-children">
            {Array.from(terminalGroups.entries())
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([terminal, rows]) => (
                <TerminalMiniCard key={terminal} terminal={terminal} rows={rows} />
              ))}
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
