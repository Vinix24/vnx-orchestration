'use client';

import { TERMINAL_COLORS } from '@/lib/types';

const ALL_TERMINALS = ['T-MANAGER', 'T0', 'T1', 'T2', 'T3', 'unknown'];

interface TerminalFilterProps {
  selected: Set<string>;
  onChange: (selected: Set<string>) => void;
}

export default function TerminalFilter({ selected, onChange }: TerminalFilterProps) {
  function toggle(terminal: string) {
    const next = new Set(selected);
    if (next.has(terminal)) {
      if (next.size > 1) next.delete(terminal);
    } else {
      next.add(terminal);
    }
    onChange(next);
  }

  function selectAll() {
    onChange(new Set(ALL_TERMINALS));
  }

  const allSelected = selected.size === ALL_TERMINALS.length;

  return (
    <div className="flex flex-wrap items-center gap-2 mb-5 animate-in-fast">
      <span
        className="text-xs font-medium mr-1"
        style={{ color: 'var(--color-muted)', letterSpacing: '0.03em', textTransform: 'uppercase' }}
      >
        Terminals
      </span>
      {ALL_TERMINALS.map((t) => {
        const active = selected.has(t);
        const color = TERMINAL_COLORS[t] ?? TERMINAL_COLORS.unknown;
        return (
          <button
            key={t}
            onClick={() => toggle(t)}
            className="flex items-center gap-1.5 text-xs font-medium transition-all"
            style={{
              padding: '5px 14px',
              borderRadius: 20,
              backgroundColor: active ? `${color}14` : 'transparent',
              border: `1.5px solid ${active ? `${color}60` : 'var(--color-border)'}`,
              color: active ? color : 'var(--color-muted)',
              opacity: active ? 1 : 0.7,
              cursor: 'pointer',
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                backgroundColor: active ? color : 'var(--color-border-strong)',
                transition: 'all 0.2s ease',
              }}
            />
            {t}
          </button>
        );
      })}
      {!allSelected && (
        <button
          onClick={selectAll}
          className="text-xs font-medium transition-all"
          style={{
            padding: '5px 10px',
            borderRadius: 20,
            color: 'var(--color-accent)',
            cursor: 'pointer',
            border: 'none',
            backgroundColor: 'transparent',
          }}
        >
          All
        </button>
      )}
    </div>
  );
}
