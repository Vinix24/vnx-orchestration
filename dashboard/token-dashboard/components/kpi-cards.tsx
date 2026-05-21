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
      color: '#f39c12',
      glowColor: 'rgba(243, 156, 18, 0.10)',
    },
    {
      label: 'Total API Calls',
      value: totalApiCalls.toLocaleString(),
      icon: Zap,
      color: '#2e86c1',
      glowColor: 'rgba(46, 134, 193, 0.10)',
    },
    {
      label: 'Avg Context/Call',
      value: `${avgContext.toFixed(1)}K`,
      icon: Database,
      color: '#0a2463',
      glowColor: 'rgba(10, 36, 99, 0.08)',
    },
    {
      label: 'Cache Hit %',
      value: `${avgCache.toFixed(1)}%`,
      icon: TrendingUp,
      color: avgCache >= 95 ? '#27ae60' : avgCache >= 90 ? '#f39c12' : '#c0392b',
      glowColor: avgCache >= 95
        ? 'rgba(39, 174, 96, 0.10)'
        : avgCache >= 90
        ? 'rgba(243, 156, 18, 0.10)'
        : 'rgba(192, 57, 43, 0.10)',
    },
    {
      label: 'Context Rotations',
      value: totalRotations.toLocaleString(),
      icon: RefreshCw,
      color: '#6B8AE6',
      glowColor: 'rgba(107, 138, 230, 0.10)',
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
            boxShadow: `0 2px 12px ${card.glowColor}, var(--shadow-sm)`,
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
                backgroundColor: `${card.color}12`,
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
