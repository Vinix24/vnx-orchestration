'use client';

import { useState, useMemo } from 'react';
import { subDays, format } from 'date-fns';
import { useTokenStats } from '@/lib/hooks';
import PeriodSelector from '@/components/period-selector';
import TerminalFilter from '@/components/terminal-filter';
import dynamic from 'next/dynamic';

const TokenVolumeChart = dynamic(() => import('@/components/charts/token-volume-chart'), { ssr: false });
import { TERMINAL_COLORS } from '@/lib/types';
import type { GroupBy, TokenStats } from '@/lib/types';
import { DollarSign, ArrowUpDown, TrendingUp } from 'lucide-react';

const ALL_TERMINALS = new Set(['T-MANAGER', 'T0', 'T1', 'T2', 'T3', 'unknown']);

// Claude API pricing per million tokens
const PRICING = {
  'claude-opus': {
    input: 15,
    cacheWrite: 18.75,
    cacheRead: 1.5,
    output: 75,
  },
  'claude-sonnet': {
    input: 3,
    cacheWrite: 3.75,
    cacheRead: 0.30,
    output: 15,
  },
} as const;

type ModelKey = keyof typeof PRICING;

function getModelKey(model: string): ModelKey {
  if (model.includes('opus')) return 'claude-opus';
  if (model.includes('sonnet')) return 'claude-sonnet';
  return 'claude-opus';
}

interface CostBreakdown {
  inputCost: number;
  cacheWriteCost: number;
  cacheReadCost: number;
  outputCost: number;
  total: number;
}

function calculateCost(rows: TokenStats[]): CostBreakdown {
  let inputCost = 0;
  let cacheWriteCost = 0;
  let cacheReadCost = 0;
  let outputCost = 0;

  for (const row of rows) {
    const pricing = PRICING[getModelKey(row.model)];
    inputCost += (row.total_input_tokens / 1_000_000) * pricing.input;
    cacheWriteCost += (row.total_cache_creation_tokens / 1_000_000) * pricing.cacheWrite;
    cacheReadCost += (row.total_cache_read_tokens / 1_000_000) * pricing.cacheRead;
    outputCost += (row.total_output_tokens / 1_000_000) * pricing.output;
  }

  return {
    inputCost,
    cacheWriteCost,
    cacheReadCost,
    outputCost,
    total: inputCost + cacheWriteCost + cacheReadCost + outputCost,
  };
}

function calculateModelCosts(rows: TokenStats[]): Map<string, CostBreakdown> {
  const modelGroups = new Map<string, TokenStats[]>();
  for (const row of rows) {
    const key = getModelKey(row.model);
    const existing = modelGroups.get(key) ?? [];
    existing.push(row);
    modelGroups.set(key, existing);
  }

  const result = new Map<string, CostBreakdown>();
  for (const [model, modelRows] of modelGroups) {
    result.set(model, calculateCost(modelRows));
  }
  return result;
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function formatCurrency(value: number): string {
  return `$${value.toFixed(2)}`;
}

type SortKey = 'period' | 'terminal' | 'input' | 'cacheCreation' | 'cacheRead' | 'output' | 'totalContext' | 'cost';
type SortDir = 'asc' | 'desc';

interface TableRow {
  period: string;
  terminal: string;
  input: number;
  cacheCreation: number;
  cacheRead: number;
  output: number;
  totalContext: number;
  cost: number;
}

function CostCard({
  title,
  cost,
  color,
  subtitle,
}: {
  title: string;
  cost: CostBreakdown;
  color: string;
  subtitle: string;
}) {
  return (
    <div
      className="glass-card animate-in"
      style={{
        padding: '28px',
        boxShadow: `0 4px 24px ${color}12`,
      }}
    >
      <div className="flex items-center gap-3 mb-5">
        <div
          style={{
            width: 40,
            height: 40,
            borderRadius: 10,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            backgroundColor: `${color}15`,
          }}
        >
          <DollarSign size={20} style={{ color }} />
        </div>
        <div>
          <h3 className="text-sm font-semibold" style={{ color }}>
            {title}
          </h3>
          <p className="text-xs" style={{ color: 'var(--color-muted)' }}>{subtitle}</p>
        </div>
      </div>

      <div className="kpi-value" style={{ color, marginBottom: 20 }}>
        {formatCurrency(cost.total)}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {[
          { label: 'Input', value: cost.inputCost, barColor: '#ff6b6b' },
          { label: 'Cache Write', value: cost.cacheWriteCost, barColor: '#f97316' },
          { label: 'Cache Read', value: cost.cacheReadCost, barColor: '#6B8AE6' },
          { label: 'Output', value: cost.outputCost, barColor: '#50fa7b' },
        ].map((item) => {
          const pct = cost.total > 0 ? (item.value / cost.total) * 100 : 0;
          return (
            <div key={item.label}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs" style={{ color: 'var(--color-muted)' }}>{item.label}</span>
                <span className="text-xs font-medium" style={{ color: 'var(--color-foreground)' }}>
                  {formatCurrency(item.value)}
                </span>
              </div>
              <div
                style={{
                  height: 4,
                  borderRadius: 2,
                  backgroundColor: 'rgba(255,255,255,0.06)',
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    height: '100%',
                    width: `${pct}%`,
                    borderRadius: 2,
                    backgroundColor: item.barColor,
                    transition: 'width 0.6s ease',
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function UsagePage() {
  const [from, setFrom] = useState(() => format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [to, setTo] = useState(() => format(new Date(), 'yyyy-MM-dd'));
  const [group, setGroup] = useState<GroupBy>('day');
  const [terminals, setTerminals] = useState<Set<string>>(ALL_TERMINALS);
  const [sortKey, setSortKey] = useState<SortKey>('period');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const { data: raw, error, isLoading } = useTokenStats(from, to, group);
  const data = raw?.filter((r) => terminals.has(r.terminal));

  const tableRows: TableRow[] = useMemo(() => {
    if (!data) return [];
    const groupKey = (r: TokenStats) => `${r.period}|${r.terminal}`;
    const groups = new Map<string, TokenStats[]>();
    for (const row of data) {
      const key = groupKey(row);
      const existing = groups.get(key) ?? [];
      existing.push(row);
      groups.set(key, existing);
    }

    return Array.from(groups.entries()).map(([, rows]) => {
      const input = rows.reduce((s, r) => s + r.total_input_tokens, 0);
      const cacheCreation = rows.reduce((s, r) => s + r.total_cache_creation_tokens, 0);
      const cacheRead = rows.reduce((s, r) => s + r.total_cache_read_tokens, 0);
      const output = rows.reduce((s, r) => s + r.total_output_tokens, 0);
      const cost = calculateCost(rows);

      return {
        period: rows[0].period,
        terminal: rows[0].terminal,
        input,
        cacheCreation,
        cacheRead,
        output,
        totalContext: input + cacheCreation + cacheRead,
        cost: cost.total,
      };
    });
  }, [data]);

  const sortedRows = useMemo(() => {
    const sorted = [...tableRows].sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      if (typeof aVal === 'string' && typeof bVal === 'string') {
        return sortDir === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      }
      return sortDir === 'asc' ? (aVal as number) - (bVal as number) : (bVal as number) - (aVal as number);
    });
    return sorted;
  }, [tableRows, sortKey, sortDir]);

  const totals = useMemo(() => {
    return {
      input: tableRows.reduce((s, r) => s + r.input, 0),
      cacheCreation: tableRows.reduce((s, r) => s + r.cacheCreation, 0),
      cacheRead: tableRows.reduce((s, r) => s + r.cacheRead, 0),
      output: tableRows.reduce((s, r) => s + r.output, 0),
      totalContext: tableRows.reduce((s, r) => s + r.totalContext, 0),
      cost: tableRows.reduce((s, r) => s + r.cost, 0),
    };
  }, [tableRows]);

  const modelCosts = useMemo(() => {
    if (!data) return new Map<string, CostBreakdown>();
    return calculateModelCosts(data);
  }, [data]);

  const totalCost = useMemo(() => {
    if (!data) return { inputCost: 0, cacheWriteCost: 0, cacheReadCost: 0, outputCost: 0, total: 0 } as CostBreakdown;
    return calculateCost(data);
  }, [data]);

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  const columns: { key: SortKey; label: string; align?: string }[] = [
    { key: 'period', label: 'Period' },
    { key: 'terminal', label: 'Terminal' },
    { key: 'input', label: 'Input', align: 'right' },
    { key: 'cacheCreation', label: 'Cache Write', align: 'right' },
    { key: 'cacheRead', label: 'Cache Read', align: 'right' },
    { key: 'output', label: 'Output', align: 'right' },
    { key: 'totalContext', label: 'Total Context', align: 'right' },
    { key: 'cost', label: 'Hyp. Cost', align: 'right' },
  ];

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Usage & Costs</h2>
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
          {/* Hypothetical cost banner */}
          <div
            className="glass-card animate-in"
            style={{
              padding: '24px 28px',
              marginBottom: 24,
              display: 'flex',
              alignItems: 'center',
              gap: 20,
              boxShadow: '0 4px 24px rgba(249, 115, 22, 0.08)',
            }}
          >
            <div
              style={{
                width: 48,
                height: 48,
                borderRadius: 12,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: 'linear-gradient(135deg, rgba(249, 115, 22, 0.15), rgba(250, 204, 21, 0.1))',
                flexShrink: 0,
              }}
            >
              <TrendingUp size={24} style={{ color: 'var(--color-accent)' }} />
            </div>
            <div style={{ flex: 1 }}>
              <div className="text-xs font-medium" style={{ color: 'var(--color-muted)', marginBottom: 4, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
                Hypothetical per-token cost this period
              </div>
              <div className="kpi-value" style={{ color: 'var(--color-accent)' }}>
                {formatCurrency(totalCost.total)}
              </div>
              <div className="text-xs" style={{ color: 'var(--color-muted)', marginTop: 4 }}>
                You have a Max subscription -- this is for informational purposes only
              </div>
            </div>
          </div>

          {/* Model cost cards */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 mb-6">
            {Array.from(modelCosts.entries())
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([model, cost]) => {
                const color = model === 'claude-opus' ? '#f97316' : '#6B8AE6';
                const pricing = PRICING[model as ModelKey];
                return (
                  <CostCard
                    key={model}
                    title={model}
                    cost={cost}
                    color={color}
                    subtitle={`$${pricing.input}/M in | $${pricing.cacheWrite}/M cache | $${pricing.output}/M out`}
                  />
                );
              })}
          </div>

          {/* Token Volume Chart */}
          <div className="mb-6">
            <TokenVolumeChart data={data} />
          </div>

          {/* Token Breakdown Table */}
          <div
            className="glass-card animate-in"
            style={{ overflow: 'hidden' }}
          >
            <div style={{ padding: '20px 24px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
              <h3
                className="text-sm font-semibold"
                style={{ color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}
              >
                Token Breakdown
              </h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm" style={{ borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                    {columns.map((col) => (
                      <th
                        key={col.key}
                        onClick={() => handleSort(col.key)}
                        className="text-xs font-medium"
                        style={{
                          padding: '12px 16px',
                          color: sortKey === col.key ? 'var(--color-accent)' : 'var(--color-muted)',
                          textAlign: (col.align as 'left' | 'right') || 'left',
                          cursor: 'pointer',
                          userSelect: 'none',
                          letterSpacing: '0.03em',
                          textTransform: 'uppercase',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        <span className="inline-flex items-center gap-1">
                          {col.label}
                          <ArrowUpDown
                            size={10}
                            style={{
                              opacity: sortKey === col.key ? 1 : 0.3,
                            }}
                          />
                        </span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.map((row, idx) => {
                    const termColor = TERMINAL_COLORS[row.terminal] ?? TERMINAL_COLORS.unknown;
                    return (
                      <tr
                        key={`${row.period}-${row.terminal}-${idx}`}
                        className="table-row-hover"
                        style={{
                          borderBottom: '1px solid rgba(255,255,255,0.03)',
                          borderLeft: `3px solid ${termColor}30`,
                        }}
                      >
                        <td className="font-mono text-xs" style={{ padding: '10px 16px', color: 'var(--color-muted)' }}>
                          {row.period}
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
                            {row.terminal}
                          </span>
                        </td>
                        <td className="font-mono text-xs" style={{ padding: '10px 16px', color: '#ff6b6b', textAlign: 'right' }}>
                          {formatTokens(row.input)}
                        </td>
                        <td className="font-mono text-xs" style={{ padding: '10px 16px', color: '#f97316', textAlign: 'right' }}>
                          {formatTokens(row.cacheCreation)}
                        </td>
                        <td className="font-mono text-xs" style={{ padding: '10px 16px', color: '#6B8AE6', textAlign: 'right' }}>
                          {formatTokens(row.cacheRead)}
                        </td>
                        <td className="font-mono text-xs" style={{ padding: '10px 16px', color: '#50fa7b', textAlign: 'right' }}>
                          {formatTokens(row.output)}
                        </td>
                        <td className="font-mono text-xs" style={{ padding: '10px 16px', color: 'var(--color-foreground)', textAlign: 'right' }}>
                          {formatTokens(row.totalContext)}
                        </td>
                        <td className="font-mono text-xs font-medium" style={{ padding: '10px 16px', color: 'var(--color-accent-gold)', textAlign: 'right' }}>
                          {formatCurrency(row.cost)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
                <tfoot>
                  <tr
                    style={{
                      borderTop: '2px solid rgba(255,255,255,0.08)',
                      backgroundColor: 'rgba(255,255,255,0.02)',
                    }}
                  >
                    <td
                      colSpan={2}
                      className="text-xs font-semibold"
                      style={{ padding: '12px 16px', color: 'var(--color-foreground)', letterSpacing: '0.03em', textTransform: 'uppercase' }}
                    >
                      Total
                    </td>
                    <td className="font-mono text-xs font-bold" style={{ padding: '12px 16px', color: '#ff6b6b', textAlign: 'right' }}>
                      {formatTokens(totals.input)}
                    </td>
                    <td className="font-mono text-xs font-bold" style={{ padding: '12px 16px', color: '#f97316', textAlign: 'right' }}>
                      {formatTokens(totals.cacheCreation)}
                    </td>
                    <td className="font-mono text-xs font-bold" style={{ padding: '12px 16px', color: '#6B8AE6', textAlign: 'right' }}>
                      {formatTokens(totals.cacheRead)}
                    </td>
                    <td className="font-mono text-xs font-bold" style={{ padding: '12px 16px', color: '#50fa7b', textAlign: 'right' }}>
                      {formatTokens(totals.output)}
                    </td>
                    <td className="font-mono text-xs font-bold" style={{ padding: '12px 16px', color: 'var(--color-foreground)', textAlign: 'right' }}>
                      {formatTokens(totals.totalContext)}
                    </td>
                    <td className="font-mono text-xs font-bold" style={{ padding: '12px 16px', color: 'var(--color-accent-gold)', textAlign: 'right' }}>
                      {formatCurrency(totals.cost)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
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
