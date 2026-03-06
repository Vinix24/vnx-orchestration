'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import { subDays, format } from 'date-fns';
import { useTokenStats, useSessions } from '@/lib/hooks';
import PeriodSelector from '@/components/period-selector';
import TerminalFilter from '@/components/terminal-filter';
import type { GroupBy, SessionDetail } from '@/lib/types';
import { TERMINAL_COLORS } from '@/lib/types';

const ContextPerCall = dynamic(() => import('@/components/charts/context-per-call'), { ssr: false });
const CacheEfficiency = dynamic(() => import('@/components/charts/cache-efficiency'), { ssr: false });

const ALL_TERMINALS = new Set(['T-MANAGER', 'T0', 'T1', 'T2', 'T3', 'unknown']);

function SessionTable({ sessions }: { sessions: SessionDetail[] }) {
  const sorted = [...sessions].sort((a, b) => b.api_calls - a.api_calls).slice(0, 10);

  return (
    <div className="glass-card animate-in" style={{ overflow: 'hidden' }}>
      <div style={{ padding: '20px 24px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
        <h3
          className="text-sm font-semibold"
          style={{ color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}
        >
          Top Sessions by API Calls
        </h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" style={{ borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
              {['Session', 'Terminal', 'API Calls', 'Context/Call', 'Cache %', 'Duration', 'Activity', 'Tools'].map(
                (h) => (
                  <th
                    key={h}
                    className="text-xs font-medium"
                    style={{
                      textAlign: 'left',
                      padding: '12px 16px',
                      color: 'var(--color-muted)',
                      letterSpacing: '0.03em',
                      textTransform: 'uppercase',
                    }}
                  >
                    {h}
                  </th>
                )
              )}
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => {
              const termColor = TERMINAL_COLORS[s.terminal] ?? TERMINAL_COLORS.unknown;
              const ctxK = s.context_per_call_K ?? 0;
              const cachePct = s.cache_hit_pct ?? 0;
              return (
                <tr
                  key={s.session_id}
                  className="table-row-hover"
                  style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}
                >
                  <td className="font-mono text-xs" style={{ padding: '10px 16px', color: 'var(--color-muted)' }}>
                    {(s.session_id ?? '').slice(0, 8)}
                  </td>
                  <td style={{ padding: '10px 16px' }}>
                    <span
                      className="inline-flex items-center gap-1.5 text-xs font-medium"
                      style={{ color: termColor }}
                    >
                      <span
                        style={{
                          width: 7,
                          height: 7,
                          borderRadius: '50%',
                          backgroundColor: termColor,
                          boxShadow: `0 0 4px ${termColor}40`,
                        }}
                      />
                      {s.terminal}
                    </span>
                  </td>
                  <td className="font-mono" style={{ padding: '10px 16px', color: 'var(--color-foreground)' }}>
                    {(s.api_calls ?? 0).toLocaleString()}
                  </td>
                  <td className="font-mono" style={{ padding: '10px 16px', color: 'var(--color-foreground)' }}>
                    {ctxK.toFixed(1)}K
                  </td>
                  <td style={{ padding: '10px 16px' }}>
                    <span
                      className="font-mono"
                      style={{
                        color:
                          cachePct >= 95
                            ? 'var(--color-success)'
                            : cachePct >= 90
                            ? 'var(--color-warning)'
                            : 'var(--color-error)',
                      }}
                    >
                      {cachePct.toFixed(1)}%
                    </span>
                  </td>
                  <td className="text-xs" style={{ padding: '10px 16px', color: 'var(--color-muted)' }}>
                    {Math.round(s.duration_minutes ?? 0)}m
                  </td>
                  <td className="text-xs" style={{ padding: '10px 16px', color: 'var(--color-muted)' }}>
                    {s.primary_activity ?? '—'}
                  </td>
                  <td className="font-mono text-xs" style={{ padding: '10px 16px', color: 'var(--color-muted)' }}>
                    {(s.tool_calls_total ?? 0).toLocaleString()}
                  </td>
                </tr>
              );
            })}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={8} className="text-center text-xs" style={{ padding: '40px 16px', color: 'var(--color-muted)' }}>
                  No session data available.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function TokensPage() {
  const [from, setFrom] = useState(() => format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [to, setTo] = useState(() => format(new Date(), 'yyyy-MM-dd'));
  const [group, setGroup] = useState<GroupBy>('day');
  const [terminals, setTerminals] = useState<Set<string>>(ALL_TERMINALS);

  const { data: raw, error, isLoading } = useTokenStats(from, to, group);
  const data = raw?.filter((r) => terminals.has(r.terminal));
  const { data: rawSessions } = useSessions(to);
  const sessions = rawSessions?.filter((s) => terminals.has(s.terminal));

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Token Analysis</h2>
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
            <ContextPerCall data={data} />
          </div>

          <div style={{ marginBottom: 24 }}>
            <CacheEfficiency data={data} />
          </div>

          {sessions && <SessionTable sessions={sessions} />}
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
