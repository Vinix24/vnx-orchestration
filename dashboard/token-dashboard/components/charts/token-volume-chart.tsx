'use client';

import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { TokenStats } from '@/lib/types';
import { format, parseISO } from 'date-fns';

interface TokenVolumeChartProps {
  data: TokenStats[];
}

interface ChartRow {
  period: string;
  input: number;
  cacheCreation: number;
  cacheRead: number;
  output: number;
}

function formatMillions(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(0)}K`;
  return String(value);
}

export default function TokenVolumeChart({ data }: TokenVolumeChartProps) {
  const periodMap = new Map<string, ChartRow>();

  for (const row of data) {
    if (!periodMap.has(row.period)) {
      periodMap.set(row.period, {
        period: row.period,
        input: 0,
        cacheCreation: 0,
        cacheRead: 0,
        output: 0,
      });
    }
    const entry = periodMap.get(row.period)!;
    entry.input += row.total_input_tokens;
    entry.cacheCreation += row.total_cache_creation_tokens;
    entry.cacheRead += row.total_cache_read_tokens;
    entry.output += row.total_output_tokens;
  }

  const chartData = Array.from(periodMap.values()).sort((a, b) =>
    a.period.localeCompare(b.period)
  );

  const categories = [
    { key: 'input', label: 'Input Tokens', color: '#ff6b6b' },
    { key: 'cacheCreation', label: 'Cache Creation', color: '#f97316' },
    { key: 'cacheRead', label: 'Cache Read', color: '#6B8AE6' },
    { key: 'output', label: 'Output Tokens', color: '#50fa7b' },
  ];

  return (
    <div
      className="glass-card animate-in"
      style={{ padding: '24px' }}
    >
      <h3
        className="text-sm font-semibold mb-5"
        style={{ color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}
      >
        Token Volume Over Time
      </h3>
      <div style={{ width: '100%', height: 350 }}>
        <ResponsiveContainer>
          <AreaChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
            <defs>
              {categories.map((cat) => (
                <linearGradient key={cat.key} id={`vol-grad-${cat.key}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={cat.color} stopOpacity={0.35} />
                  <stop offset="95%" stopColor={cat.color} stopOpacity={0.02} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.04)" vertical={false} />
            <XAxis
              dataKey="period"
              tick={{ fill: 'rgba(244,244,249,0.45)', fontSize: 11 }}
              tickFormatter={(v: string) => {
                try { return format(parseISO(v), 'MMM d'); } catch { return v; }
              }}
              stroke="rgba(255,255,255,0.06)"
              axisLine={{ stroke: 'rgba(255,255,255,0.06)' }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: 'rgba(244,244,249,0.45)', fontSize: 11 }}
              stroke="rgba(255,255,255,0.06)"
              axisLine={false}
              tickLine={false}
              tickFormatter={formatMillions}
            />
            <Tooltip
              contentStyle={{
                background: 'linear-gradient(135deg, rgba(10, 20, 48, 0.95), rgba(10, 20, 48, 0.85))',
                backdropFilter: 'blur(16px)',
                border: '1px solid rgba(255,255,255,0.1)',
                borderRadius: 12,
                fontSize: 12,
                boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
              }}
              labelFormatter={(v: string) => {
                try { return format(parseISO(v), 'MMM d, yyyy'); } catch { return v; }
              }}
              formatter={(value: number, name: string) => [formatMillions(value), name]}
            />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8 }} />
            {categories.map((cat) => (
              <Area
                key={cat.key}
                type="monotone"
                dataKey={cat.key}
                name={cat.label}
                stackId="1"
                stroke={cat.color}
                strokeWidth={2}
                fill={`url(#vol-grad-${cat.key})`}
                fillOpacity={1}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
