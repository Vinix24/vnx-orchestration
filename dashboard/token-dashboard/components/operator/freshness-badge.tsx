'use client';

import { Clock } from 'lucide-react';

interface Props {
  staleness_seconds?: number;
  queried_at?: string;
}

function fmt(secs: number): string {
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

export default function FreshnessBadge({ staleness_seconds, queried_at }: Props) {
  const age = staleness_seconds ?? 0;
  const isAging = age > 60 && age <= 300;
  const isStale = age > 300;

  const color = isStale
    ? 'var(--color-error)'
    : isAging
    ? 'var(--color-warning)'
    : 'var(--color-muted)';

  const label = staleness_seconds != null ? fmt(age) : queried_at ? 'just now' : '—';

  return (
    <span
      className="flex items-center gap-1"
      style={{ fontSize: 11, color, fontVariantNumeric: 'tabular-nums' }}
      title={queried_at ? `Queried at ${queried_at}` : undefined}
    >
      <Clock size={11} />
      {label}
    </span>
  );
}
