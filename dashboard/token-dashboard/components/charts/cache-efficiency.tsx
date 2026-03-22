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
              domain={[80, 100]}
              tick={{ fill: 'rgba(244,244,249,0.45)', fontSize: 11 }}
              stroke="rgba(255,255,255,0.06)"
              axisLine={false}
              tickLine={false}
              tickFormatter={(v: number) => `${v}%`}
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
              formatter={(value: number) => [`${value}%`]}
            />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8 }} />
            <ReferenceLine
              y={95}
              stroke="var(--color-success)"
              strokeDasharray="6 3"
              strokeOpacity={0.4}
              label={{
                value: '95% target',
                position: 'right',
                fill: 'var(--color-success)',
                fontSize: 10,
                opacity: 0.7,
              }}
            />
            {terminals.map((terminal) => (
              <Line
                key={terminal}
                type="monotone"
                dataKey={terminal}
                stroke={TERMINAL_COLORS[terminal] ?? TERMINAL_COLORS.unknown}
                strokeWidth={2.5}
                dot={{ r: 3, fill: '#070b16', strokeWidth: 2 }}
                activeDot={{
                  r: 6,
                  fill: TERMINAL_COLORS[terminal] ?? TERMINAL_COLORS.unknown,
                  stroke: '#070b16',
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
