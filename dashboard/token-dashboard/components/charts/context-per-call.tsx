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
} from 'recharts';
import type { TokenStats } from '@/lib/types';
import { TERMINAL_COLORS } from '@/lib/types';
import { buildLineByTerminal, getTerminals } from '@/lib/metrics';
import { format, parseISO } from 'date-fns';

interface ContextPerCallProps {
  data: TokenStats[];
}

export default function ContextPerCall({ data }: ContextPerCallProps) {
  const chartData = buildLineByTerminal(data, 'context_per_call_K');
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
        Context Per Call Over Time
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
              tick={{ fill: '#4a5a7a', fontSize: 11 }}
              stroke="#e1e8f0"
              axisLine={false}
              tickLine={false}
              label={{
                value: 'K tokens',
                angle: -90,
                position: 'insideLeft',
                fill: '#8a96ad',
                fontSize: 11,
              }}
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
              formatter={(value: number) => [`${value}K`]}
            />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8, color: '#4a5a7a' }} />
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
