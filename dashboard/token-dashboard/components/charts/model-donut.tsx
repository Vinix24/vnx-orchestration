'use client';

import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts';
import type { TokenStats } from '@/lib/types';
import { MODEL_COLORS } from '@/lib/types';
import { aggregateByModel } from '@/lib/metrics';

interface ModelDonutProps {
  data: TokenStats[];
}

export default function ModelDonut({ data }: ModelDonutProps) {
  const modelData = aggregateByModel(data);
  const totalSessions = modelData.reduce((s, r) => s + r.sessions, 0);

  const pieData = modelData.map((m) => ({
    name: m.model,
    value: m.sessions,
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
        Sessions by Model
      </h3>
      <div style={{ width: '100%', height: 300, position: 'relative' }}>
        <div
          style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            textAlign: 'center',
            pointerEvents: 'none',
            zIndex: 1,
          }}
        >
          <div
            className="kpi-value"
            style={{ color: 'var(--color-foreground)' }}
          >
            {totalSessions}
          </div>
          <div
            className="text-xs"
            style={{ color: 'var(--color-muted)', marginTop: 4 }}
          >
            sessions
          </div>
        </div>
        <ResponsiveContainer>
          <PieChart>
            <defs>
              {pieData.map((entry) => {
                const color = MODEL_COLORS[entry.name] ?? MODEL_COLORS.unknown;
                return (
                  <linearGradient key={entry.name} id={`model-grad-${entry.name}`} x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%" stopColor={color} stopOpacity={1} />
                    <stop offset="100%" stopColor={color} stopOpacity={0.7} />
                  </linearGradient>
                );
              })}
            </defs>
            <Pie
              data={pieData}
              cx="50%"
              cy="50%"
              innerRadius={72}
              outerRadius={105}
              paddingAngle={4}
              dataKey="value"
              stroke="none"
              cornerRadius={4}
            >
              {pieData.map((entry) => (
                <Cell
                  key={entry.name}
                  fill={`url(#model-grad-${entry.name})`}
                />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{
                background: 'linear-gradient(135deg, rgba(10, 20, 48, 0.95), rgba(10, 20, 48, 0.85))',
                backdropFilter: 'blur(16px)',
                border: '1px solid rgba(255,255,255,0.1)',
                borderRadius: 12,
                fontSize: 12,
                boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
              }}
              formatter={(value: number, name: string) => [`${value} sessions`, name]}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <div className="flex justify-center gap-6 mt-3">
        {pieData.map((entry) => {
          const color = MODEL_COLORS[entry.name] ?? MODEL_COLORS.unknown;
          const pct = totalSessions > 0 ? ((entry.value / totalSessions) * 100).toFixed(0) : '0';
          return (
            <div key={entry.name} className="flex items-center gap-2 text-xs">
              <div
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: 3,
                  backgroundColor: color,
                  boxShadow: `0 0 6px ${color}40`,
                }}
              />
              <span style={{ color: 'var(--color-muted)' }}>
                {entry.name} ({pct}%)
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
