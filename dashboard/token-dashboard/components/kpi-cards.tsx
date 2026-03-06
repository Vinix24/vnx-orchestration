'use client';

import { Activity, Zap, Database, TrendingUp, RefreshCw } from 'lucide-react';
import type { TokenStats } from '@/lib/types';
import { weightedAverage } from '@/lib/metrics';

interface KPICardsProps {
  data: TokenStats[];
}

export default function KPICards({ data }: KPICardsProps) {
  const totalSessions = data.reduce((s, r) => s + r.sessions, 0);
  const totalApiCalls = data.reduce((s, r) => s + r.api_calls, 0);
  const avgContext = weightedAverage(data, 'context_per_call_K', 'api_calls');
  const avgCache = weightedAverage(data, 'cache_hit_pct', 'api_calls');
  const totalRotations = data.reduce((s, r) => s + (r.context_rotations ?? 0), 0);

  const cards = [
    {
      label: 'Total Sessions',
      value: totalSessions.toLocaleString(),
      icon: Activity,
      color: '#f97316',
      glowColor: 'rgba(249, 115, 22, 0.15)',
    },
    {
      label: 'Total API Calls',
      value: totalApiCalls.toLocaleString(),
      icon: Zap,
      color: '#facc15',
      glowColor: 'rgba(250, 204, 21, 0.12)',
    },
    {
      label: 'Avg Context/Call',
      value: `${avgContext.toFixed(1)}K`,
      icon: Database,
      color: '#6B8AE6',
      glowColor: 'rgba(107, 138, 230, 0.12)',
    },
    {
      label: 'Cache Hit %',
      value: `${avgCache.toFixed(1)}%`,
      icon: TrendingUp,
      color: avgCache >= 95 ? '#50fa7b' : avgCache >= 90 ? '#facc15' : '#ff6b6b',
      glowColor: avgCache >= 95
        ? 'rgba(80, 250, 123, 0.12)'
        : avgCache >= 90
        ? 'rgba(250, 204, 21, 0.12)'
        : 'rgba(255, 107, 107, 0.12)',
    },
    {
      label: 'Context Rotations',
      value: totalRotations.toLocaleString(),
      icon: RefreshCw,
      color: '#9B6BE6',
      glowColor: 'rgba(155, 107, 230, 0.12)',
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-5 stagger-children">
      {cards.map((card) => (
        <div
          key={card.label}
          className="glass-card"
          style={{
            padding: '24px',
            boxShadow: `0 4px 24px ${card.glowColor}, 0 1px 2px rgba(0,0,0,0.2)`,
          }}
        >
          <div className="flex items-center justify-between mb-4">
            <span
              className="text-xs font-medium"
              style={{ color: 'var(--color-muted)', letterSpacing: '0.04em', textTransform: 'uppercase' }}
            >
              {card.label}
            </span>
            <div
              style={{
                width: 32,
                height: 32,
                borderRadius: 8,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                backgroundColor: `${card.color}15`,
              }}
            >
              <card.icon size={16} style={{ color: card.color }} />
            </div>
          </div>
          <div className="kpi-value" style={{ color: card.color }}>
            {card.value}
          </div>
        </div>
      ))}
    </div>
  );
}
