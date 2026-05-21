'use client';

import type { GroupBy } from '@/lib/types';

interface PeriodSelectorProps {
  from: string;
  to: string;
  group: GroupBy;
  onFromChange: (v: string) => void;
  onToChange: (v: string) => void;
  onGroupChange: (v: GroupBy) => void;
}

const GROUP_OPTIONS: { value: GroupBy; label: string }[] = [
  { value: 'day', label: 'Day' },
  { value: 'week', label: 'Week' },
  { value: 'month', label: 'Month' },
];

export default function PeriodSelector({
  from,
  to,
  group,
  onFromChange,
  onToChange,
  onGroupChange,
}: PeriodSelectorProps) {
  return (
    <div className="flex flex-wrap items-center gap-4 mb-6 animate-in-fast">
      <div className="flex items-center gap-2">
        <label
          className="text-xs font-medium"
          style={{ color: 'var(--color-muted)', letterSpacing: '0.03em', textTransform: 'uppercase' }}
        >
          From
        </label>
        <input
          type="date"
          value={from}
          onChange={(e) => onFromChange(e.target.value)}
          className="text-sm outline-none transition-all"
          style={{
            padding: '7px 12px',
            borderRadius: 8,
            backgroundColor: '#ffffff',
            border: '1px solid var(--color-border)',
            color: 'var(--color-foreground)',
            colorScheme: 'light',
          }}
        />
      </div>

      <div className="flex items-center gap-2">
        <label
          className="text-xs font-medium"
          style={{ color: 'var(--color-muted)', letterSpacing: '0.03em', textTransform: 'uppercase' }}
        >
          To
        </label>
        <input
          type="date"
          value={to}
          onChange={(e) => onToChange(e.target.value)}
          className="text-sm outline-none transition-all"
          style={{
            padding: '7px 12px',
            borderRadius: 8,
            backgroundColor: '#ffffff',
            border: '1px solid var(--color-border)',
            color: 'var(--color-foreground)',
            colorScheme: 'light',
          }}
        />
      </div>

      <div
        className="flex overflow-hidden"
        style={{
          borderRadius: 8,
          border: '1px solid var(--color-border)',
          backgroundColor: 'var(--color-card-hover)',
        }}
        role="radiogroup"
        aria-label="Group by period"
      >
        {GROUP_OPTIONS.map((opt) => {
          const isActive = group === opt.value;
          return (
            <button
              key={opt.value}
              onClick={() => onGroupChange(opt.value)}
              role="radio"
              aria-checked={isActive}
              className="text-sm font-medium transition-all"
              style={{
                padding: '7px 18px',
                backgroundColor: isActive ? 'var(--color-accent)' : 'transparent',
                color: isActive ? '#ffffff' : 'var(--color-muted)',
                border: 'none',
                cursor: 'pointer',
                position: 'relative',
              }}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
