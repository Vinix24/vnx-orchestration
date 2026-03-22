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
import { TERMINAL_COLORS } from '@/lib/types';
import { buildStackedByTerminal, getTerminals } from '@/lib/metrics';
import { format, parseISO } from 'date-fns';

interface ApiCallsChartProps {
  data: TokenStats[];
}

export default function ApiCallsChart({ data }: ApiCallsChartProps) {
  const chartData = buildStackedByTerminal(data, 'api_calls');
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
        API Calls by Terminal
      </h3>
      <div style={{ width: '100%', height: 300 }}>
        <ResponsiveContainer>
          <AreaChart data={chartData} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
            <defs>
              {terminals.map((terminal) => {
                const color = TERMINAL_COLORS[terminal] ?? TERMINAL_COLORS.unknown;
                return (
                  <linearGradient key={terminal} id={`grad-${terminal}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={color} stopOpacity={0.4} />
                    <stop offset="95%" stopColor={color} stopOpacity={0.02} />
                  </linearGradient>
                );
              })}
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
            />
            <Legend
              wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
            />
            {terminals.map((terminal) => (
              <Area
                key={terminal}
                type="monotone"
                dataKey={terminal}
                stackId="1"
                stroke={TERMINAL_COLORS[terminal] ?? TERMINAL_COLORS.unknown}
                strokeWidth={2}
                fill={`url(#grad-${terminal})`}
                fillOpacity={1}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
