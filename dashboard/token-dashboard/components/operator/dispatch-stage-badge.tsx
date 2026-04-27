'use client';

import type { DispatchStage } from '@/lib/types';

const STAGE_STYLES: Record<DispatchStage, { bg: string; border: string; color: string; label: string }> = {
  staging: {
    bg: 'rgba(148, 163, 184, 0.12)',
    border: 'rgba(148, 163, 184, 0.30)',
    color: '#94a3b8',
    label: 'Staging',
  },
  pending: {
    bg: 'rgba(249, 115, 22, 0.12)',
    border: 'rgba(249, 115, 22, 0.35)',
    color: '#f97316',
    label: 'Pending',
  },
  active: {
    bg: 'rgba(34, 211, 238, 0.12)',
    border: 'rgba(34, 211, 238, 0.35)',
    color: '#22d3ee',
    label: 'Active',
  },
  review: {
    bg: 'rgba(168, 85, 247, 0.12)',
    border: 'rgba(168, 85, 247, 0.35)',
    color: '#a855f7',
    label: 'Review',
  },
  done: {
    bg: 'rgba(34, 197, 94, 0.12)',
    border: 'rgba(34, 197, 94, 0.35)',
    color: '#22c55e',
    label: 'Done',
  },
  rejected: {
    bg: 'rgba(239, 68, 68, 0.10)',
    border: 'rgba(239, 68, 68, 0.30)',
    color: '#ef4444',
    label: 'Rejected',
  },
};

export default function DispatchStageBadge({ stage }: { stage: DispatchStage }) {
  const s = STAGE_STYLES[stage] ?? STAGE_STYLES.staging;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '2px 8px',
        borderRadius: 12,
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        background: s.bg,
        border: `1px solid ${s.border}`,
        color: s.color,
      }}
    >
      {s.label}
    </span>
  );
}

export { STAGE_STYLES };
