'use client';

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { TokenStats } from '@/lib/types';
import { aggregateByTerminal } from '@/lib/metrics';

interface TerminalComparisonProps {
  data: TokenStats[];
}

export default function TerminalComparison({ data }: TerminalComparisonProps) {
  const terminalData = aggregateByTerminal(data).map((t) => ({
    terminal: t.terminal,
    sessions: t.sessions,
    api_calls: t.api_calls,
    output_tokens_K: Math.round(t.total_output_tokens / 1000),
  }));

  return (
    <div
      className="glass-card animate-in"
      style={{ padding: '24px' }}
    >
      <h3
        className="text-sm font-semibold mb-5"
        style={{ color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}
      >
        Terminal Comparison
      </h3>
      <div style={{ width: '100%', height: 350 }}>
        <ResponsiveContainer>
          <BarChart data={terminalData} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
            <defs>
              <linearGradient id="grad-sessions" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#f97316" stopOpacity={1} />
                <stop offset="100%" stopColor="#f97316" stopOpacity={0.6} />
              </linearGradient>
              <linearGradient id="grad-api-calls" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#6B8AE6" stopOpacity={1} />
                <stop offset="100%" stopColor="#6B8AE6" stopOpacity={0.6} />
              </linearGradient>
              <linearGradient id="grad-output" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#50fa7b" stopOpacity={1} />
                <stop offset="100%" stopColor="#50fa7b" stopOpacity={0.6} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.04)" vertical={false} />
            <XAxis
              dataKey="terminal"
              tick={{ fill: 'rgba(244,244,249,0.45)', fontSize: 11 }}
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
              cursor={{ fill: 'rgba(255,255,255,0.03)' }}
            />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8 }} />
            <Bar dataKey="sessions" fill="url(#grad-sessions)" radius={[6, 6, 0, 0]} />
            <Bar dataKey="api_calls" fill="url(#grad-api-calls)" radius={[6, 6, 0, 0]} />
            <Bar
              dataKey="output_tokens_K"
              name="output tokens (K)"
              fill="url(#grad-output)"
              radius={[6, 6, 0, 0]}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
