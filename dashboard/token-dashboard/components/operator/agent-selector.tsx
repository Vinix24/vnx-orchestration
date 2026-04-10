'use client';

import { useAgents } from '@/lib/hooks';
import type { Agent } from '@/lib/types';

const ADAPTER_COLORS: Record<string, { color: string; bg: string; border: string }> = {
  subprocess: { color: '#50fa7b', bg: 'rgba(80, 250, 123, 0.1)', border: 'rgba(80, 250, 123, 0.3)' },
  tmux:       { color: '#6B8AE6', bg: 'rgba(107, 138, 230, 0.1)', border: 'rgba(107, 138, 230, 0.3)' },
};

const TERMINAL_COLORS: Record<string, string> = {
  T0: '#6B8AE6',
  T1: '#50fa7b',
  T2: '#facc15',
  T3: '#9B6BE6',
};

interface AgentSelectorProps {
  selectedTerminal?: string;
  onSelect: (terminal: string | undefined) => void;
  /** Compact inline variant, default: false */
  compact?: boolean;
}

function AgentPill({ agent, isSelected, onClick }: {
  agent: Agent;
  isSelected: boolean;
  onClick: () => void;
}) {
  const termColor = TERMINAL_COLORS[agent.terminal] ?? '#6B6B6B';
  const adapterCfg = ADAPTER_COLORS[agent.adapter] ?? ADAPTER_COLORS.tmux;

  return (
    <button
      onClick={onClick}
      title={`${agent.terminal} — ${agent.role} (${agent.adapter})`}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '7px 12px',
        borderRadius: 10,
        background: isSelected ? 'rgba(249, 115, 22, 0.1)' : 'rgba(255,255,255,0.03)',
        border: `1px solid ${isSelected ? 'rgba(249, 115, 22, 0.4)' : 'rgba(255,255,255,0.08)'}`,
        cursor: 'pointer',
        transition: 'all 0.15s',
        textAlign: 'left',
      }}
    >
      {/* Terminal color dot */}
      <div
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: termColor,
          flexShrink: 0,
          boxShadow: isSelected ? `0 0 6px ${termColor}80` : 'none',
        }}
      />

      {/* Name + terminal ID */}
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            fontSize: 12,
            fontWeight: isSelected ? 600 : 400,
            color: isSelected ? 'var(--color-accent)' : 'var(--color-foreground)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            maxWidth: 120,
          }}
        >
          {agent.name || agent.terminal}
        </div>
        <div style={{ fontSize: 10, color: 'var(--color-muted)', marginTop: 1 }}>
          {agent.terminal}
        </div>
      </div>

      {/* Adapter badge */}
      <div
        style={{
          fontSize: 9,
          fontWeight: 600,
          letterSpacing: '0.04em',
          textTransform: 'uppercase',
          padding: '2px 6px',
          borderRadius: 6,
          background: adapterCfg.bg,
          border: `1px solid ${adapterCfg.border}`,
          color: adapterCfg.color,
          marginLeft: 'auto',
          flexShrink: 0,
        }}
      >
        {agent.adapter}
      </div>
    </button>
  );
}

export default function AgentSelector({ selectedTerminal, onSelect, compact = false }: AgentSelectorProps) {
  const { data, isLoading } = useAgents();
  const agents: Agent[] = data?.agents ?? [];

  if (isLoading) {
    return (
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {[0, 1, 2].map(i => (
          <div
            key={i}
            style={{
              width: 120,
              height: 48,
              borderRadius: 10,
              background: 'rgba(255,255,255,0.04)',
              border: '1px solid rgba(255,255,255,0.06)',
              animation: 'shimmer 1.6s infinite',
            }}
          />
        ))}
      </div>
    );
  }

  if (agents.length === 0) return null;

  return (
    <div>
      {!compact && (
        <div style={{ fontSize: 11, color: 'var(--color-muted)', marginBottom: 8 }}>Agent</div>
      )}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {/* All agents option */}
        <button
          onClick={() => onSelect(undefined)}
          style={{
            padding: '7px 12px',
            borderRadius: 10,
            fontSize: 12,
            fontWeight: !selectedTerminal ? 600 : 400,
            background: !selectedTerminal ? 'rgba(249, 115, 22, 0.1)' : 'rgba(255,255,255,0.03)',
            border: `1px solid ${!selectedTerminal ? 'rgba(249, 115, 22, 0.4)' : 'rgba(255,255,255,0.08)'}`,
            color: !selectedTerminal ? 'var(--color-accent)' : 'var(--color-muted)',
            cursor: 'pointer',
          }}
        >
          All
        </button>

        {agents.map(agent => (
          <AgentPill
            key={agent.terminal}
            agent={agent}
            isSelected={selectedTerminal === agent.terminal}
            onClick={() => onSelect(selectedTerminal === agent.terminal ? undefined : agent.terminal)}
          />
        ))}
      </div>
    </div>
  );
}
