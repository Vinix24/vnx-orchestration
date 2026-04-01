'use client';

import { AlertTriangle } from 'lucide-react';

interface Props {
  reasons: string[];
  view?: string;
}

export default function DegradedBanner({ reasons, view }: Props) {
  if (!reasons || reasons.length === 0) return null;

  return (
    <div
      role="alert"
      aria-live="polite"
      style={{
        background: 'linear-gradient(135deg, rgba(255, 107, 107, 0.12) 0%, rgba(255, 107, 107, 0.06) 100%)',
        border: '1px solid rgba(255, 107, 107, 0.4)',
        borderRadius: 12,
        padding: '14px 18px',
        marginBottom: 20,
        display: 'flex',
        gap: 12,
        alignItems: 'flex-start',
      }}
    >
      <AlertTriangle
        size={18}
        style={{ color: 'var(--color-error)', flexShrink: 0, marginTop: 1 }}
      />
      <div>
        <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-error)', marginBottom: 4 }}>
          {view ? `${view} — degraded state` : 'Degraded state'}
        </p>
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {reasons.map((r, i) => (
            <li key={i} style={{ fontSize: 12, color: 'rgba(255, 107, 107, 0.85)', lineHeight: 1.6 }}>
              {r}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
