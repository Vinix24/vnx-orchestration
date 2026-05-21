'use client';

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
} from 'recharts';
import type { TokenStats } from '@/lib/types';
import { TERMINAL_COLORS } from '@/lib/types';
import { buildLineByTerminal, getTerminals } from '@/lib/metrics';
import { format, parseISO } from 'date-fns';

interface CacheEfficiencyProps {
  data: TokenStats[];
}

export default function CacheEfficiency({ data }: CacheEfficiencyProps) {
  const chartData = buildLineByTerminal(data, 'cache_hit_pct');
  const terminals = getTerminals(data);

  return (
    <div
      className="glass-card animate-in"
      style={{ padding: '24px' }}
    >
      <h3
        className="text-sm font-semibold mb-5"
        style={{ color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}
      >
        Cache Efficiency Over Time
      </h3>
      <div style={{ width: '100%', height: 300 }}>
        <ResponsiveContainer>
          <LineChart data={chartData} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e1e8f0" vertical={false} />
            <XAxis
              dataKey="period"
              tick={{ fill: '#4a5a7a', fontSize: 11 }}
              tickFormatter={(v: string) => {
                try { return format(parseISO(v), 'MMM d'); } catch { return v; }
              }}
              stroke="#e1e8f0"
              axisLine={{ stroke: '#e1e8f0' }}
              tickLine={false}
            />
            <YAxis
              domain={[80, 100]}
              tick={{ fill: '#4a5a7a', fontSize: 11 }}
              stroke="#e1e8f0"
              axisLine={false}
              tickLine={false}
              tickFormatter={(v: number) => `${v}%`}
            />
            <Tooltip
              contentStyle={{
                background: '#ffffff',
                border: '1px solid #e1e8f0',
                borderRadius: 8,
                fontSize: 12,
                boxShadow: '0 2px 8px rgba(10, 36, 99, 0.08)',
                color: '#0a2463',
              }}
              labelFormatter={(v: string) => {
                try { return format(parseISO(v), 'MMM d, yyyy'); } catch { return v; }
              }}
              formatter={(value: number) => [`${value}%`]}
            />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8, color: '#4a5a7a' }} />
            <ReferenceLine
              y={95}
              stroke="var(--color-success)"
              strokeDasharray="6 3"
              strokeOpacity={0.6}
              label={{
                value: '95% target',
                position: 'right',
                fill: 'var(--color-success)',
                fontSize: 10,
                opacity: 0.8,
              }}
            />
            {terminals.map((terminal) => (
              <Line
                key={terminal}
                type="monotone"
                dataKey={terminal}
                stroke={TERMINAL_COLORS[terminal] ?? TERMINAL_COLORS.unknown}
                strokeWidth={2.5}
                dot={{ r: 3, fill: '#ffffff', strokeWidth: 2 }}
                activeDot={{
                  r: 6,
                  fill: TERMINAL_COLORS[terminal] ?? TERMINAL_COLORS.unknown,
                  stroke: '#ffffff',
                  strokeWidth: 2,
                }}
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
