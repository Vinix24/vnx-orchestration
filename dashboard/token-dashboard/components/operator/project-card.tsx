'use client';

import { useState } from 'react';
import { Play, Square, RotateCcw, AlertTriangle, CheckCircle2, FolderOpen } from 'lucide-react';
import type { ProjectEntry, ActionOutcome } from '@/lib/types';
import { actionStartSession, actionStopSession, actionRefreshProjections } from '@/lib/operator-api';

interface Props {
  project: ProjectEntry;
  onActionComplete?: (outcome: ActionOutcome) => void;
}

const ATTENTION_CONFIG = {
  critical: { color: 'var(--color-error)',   border: 'rgba(255, 107, 107, 0.4)',  glow: 'rgba(255, 107, 107, 0.06)' },
  warning:  { color: 'var(--color-warning)', border: 'rgba(250, 204, 21, 0.3)',   glow: 'rgba(250, 204, 21, 0.04)' },
  clear:    { color: 'var(--color-success)', border: 'rgba(80, 250, 123, 0.2)',   glow: 'rgba(80, 250, 123, 0.03)' },
};

export default function ProjectCard({ project, onActionComplete }: Props) {
  const [pending, setPending] = useState<string | null>(null);
  const [lastOutcome, setLastOutcome] = useState<ActionOutcome | null>(null);

  const attn = ATTENTION_CONFIG[project.attention_level] ?? ATTENTION_CONFIG.clear;

  async function runAction(label: string, fn: () => Promise<ActionOutcome>) {
    setPending(label);
    setLastOutcome(null);
    try {
      const outcome = await fn();
      setLastOutcome(outcome);
      onActionComplete?.(outcome);
    } catch (err) {
      const fallback: ActionOutcome = {
        action: label,
        project: project.path,
        status: 'failed',
        message: err instanceof Error ? err.message : 'Unknown error',
        timestamp: new Date().toISOString(),
      };
      setLastOutcome(fallback);
      onActionComplete?.(fallback);
    } finally {
      setPending(null);
    }
  }

  const outcomeColor =
    lastOutcome?.status === 'success' || lastOutcome?.status === 'already_active'
      ? 'var(--color-success)'
      : lastOutcome?.status === 'degraded'
      ? 'var(--color-warning)'
      : lastOutcome?.status === 'failed'
      ? 'var(--color-error)'
      : undefined;

  return (
    <div
      className="glass-card"
      style={{
        padding: '22px 24px',
        borderTop: `3px solid ${attn.border}`,
        background: `linear-gradient(135deg, ${attn.glow} 0%, transparent 60%), linear-gradient(135deg, rgba(10, 20, 48, 0.85) 0%, rgba(10, 20, 48, 0.65) 100%)`,
      }}
    >
      {/* Header */}
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3
            className="text-sm font-bold"
            style={{ color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}
          >
            {project.name}
          </h3>
          <p
            style={{
              fontSize: 11,
              color: 'var(--color-muted)',
              marginTop: 2,
              fontFamily: 'monospace',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: 220,
            }}
            title={project.path}
          >
            {project.path}
          </p>
        </div>

        {/* Session badge */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 5,
            padding: '4px 10px',
            borderRadius: 20,
            background: project.session_active
              ? 'rgba(80, 250, 123, 0.12)'
              : 'rgba(255,255,255,0.05)',
            border: `1px solid ${project.session_active ? 'rgba(80, 250, 123, 0.3)' : 'rgba(255,255,255,0.08)'}`,
            flexShrink: 0,
          }}
        >
          <div
            style={{
              width: 6,
              height: 6,
              borderRadius: '50%',
              backgroundColor: project.session_active
                ? 'var(--color-success)'
                : 'var(--color-muted)',
              ...(project.session_active && { boxShadow: '0 0 6px var(--color-success)' }),
            }}
          />
          <span
            style={{
              fontSize: 11,
              fontWeight: 600,
              color: project.session_active ? 'var(--color-success)' : 'var(--color-muted)',
            }}
          >
            {project.session_active ? 'Active' : 'Inactive'}
          </span>
        </div>
      </div>

      {/* Feature */}
      {project.active_feature && (
        <div style={{ marginBottom: 12 }}>
          <span
            style={{
              display: 'inline-block',
              fontSize: 11,
              color: 'var(--color-accent)',
              background: 'rgba(249, 115, 22, 0.1)',
              border: '1px solid rgba(249, 115, 22, 0.25)',
              borderRadius: 6,
              padding: '2px 8px',
            }}
          >
            {project.active_feature}
          </span>
        </div>
      )}

      {/* Open item counts */}
      <div className="flex items-center gap-3" style={{ marginBottom: 16 }}>
        {project.open_blocker_count > 0 && (
          <div className="flex items-center gap-1">
            <AlertTriangle size={12} style={{ color: 'var(--color-error)' }} />
            <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-error)' }}>
              {project.open_blocker_count} blocker{project.open_blocker_count !== 1 ? 's' : ''}
            </span>
          </div>
        )}
        {project.open_warn_count > 0 && (
          <div className="flex items-center gap-1">
            <AlertTriangle size={12} style={{ color: 'var(--color-warning)' }} />
            <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-warning)' }}>
              {project.open_warn_count} warn{project.open_warn_count !== 1 ? 's' : ''}
            </span>
          </div>
        )}
        {project.open_blocker_count === 0 && project.open_warn_count === 0 && (
          <div className="flex items-center gap-1">
            <CheckCircle2 size={12} style={{ color: 'var(--color-success)' }} />
            <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>No open items</span>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2" style={{ flexWrap: 'wrap' }}>
        {!project.session_active ? (
          <button
            onClick={() => runAction('start', () => actionStartSession(project.path))}
            disabled={pending !== null}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '7px 14px',
              borderRadius: 8,
              background: 'linear-gradient(135deg, rgba(249, 115, 22, 0.9), rgba(251, 146, 60, 0.9))',
              border: 'none',
              cursor: pending ? 'not-allowed' : 'pointer',
              opacity: pending ? 0.6 : 1,
              fontSize: 12,
              fontWeight: 600,
              color: '#fff',
              transition: 'opacity 0.15s',
            }}
          >
            <Play size={13} />
            {pending === 'start' ? 'Starting…' : 'Start Session'}
          </button>
        ) : (
          <button
            onClick={() => runAction('stop', () => actionStopSession(project.path))}
            disabled={pending !== null}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '7px 14px',
              borderRadius: 8,
              background: 'rgba(255, 107, 107, 0.12)',
              border: '1px solid rgba(255, 107, 107, 0.3)',
              cursor: pending ? 'not-allowed' : 'pointer',
              opacity: pending ? 0.6 : 1,
              fontSize: 12,
              fontWeight: 600,
              color: 'var(--color-error)',
              transition: 'opacity 0.15s',
            }}
          >
            <Square size={13} />
            {pending === 'stop' ? 'Stopping…' : 'Stop Session'}
          </button>
        )}

        <button
          onClick={() => runAction('refresh', () => actionRefreshProjections(project.path))}
          disabled={pending !== null}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '7px 12px',
            borderRadius: 8,
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.1)',
            cursor: pending ? 'not-allowed' : 'pointer',
            opacity: pending ? 0.6 : 1,
            fontSize: 12,
            color: 'var(--color-muted)',
            transition: 'opacity 0.15s',
          }}
        >
          <RotateCcw size={12} />
          {pending === 'refresh' ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {/* Last action outcome */}
      {lastOutcome && (
        <div
          style={{
            marginTop: 12,
            padding: '8px 12px',
            borderRadius: 8,
            background: 'rgba(255,255,255,0.03)',
            border: `1px solid ${outcomeColor ? `${outcomeColor}30` : 'rgba(255,255,255,0.08)'}`,
            fontSize: 12,
            color: outcomeColor ?? 'var(--color-muted)',
          }}
        >
          {lastOutcome.message}
        </div>
      )}
    </div>
  );
}
