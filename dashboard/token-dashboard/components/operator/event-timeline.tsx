'use client';

import { useEffect, useMemo, useState } from 'react';
import {
  Search,
  FileEdit,
  FileText,
  Terminal,
  GitCommit,
  TestTube2,
  Compass,
  Hammer,
  Play,
  Pause,
  Rewind,
  FastForward,
} from 'lucide-react';
import type {
  DispatchEvent,
  DispatchEventPhase,
  DispatchToolUseEvent,
  DispatchPhaseMarker,
} from '@/lib/types';

const PHASE_META: Record<DispatchEventPhase, { label: string; color: string; icon: typeof Compass }> = {
  explore: { label: 'Explore', color: '#22d3ee', icon: Compass },
  implement: { label: 'Implement', color: '#f97316', icon: Hammer },
  test: { label: 'Test', color: '#a855f7', icon: TestTube2 },
  commit: { label: 'Commit', color: '#22c55e', icon: GitCommit },
  other: { label: 'Other', color: '#94a3b8', icon: Terminal },
};

function toolIcon(tool: string) {
  if (tool === 'Read' || tool === 'Grep' || tool === 'Glob') return Search;
  if (tool === 'Write') return FileText;
  if (tool === 'Edit' || tool === 'MultiEdit') return FileEdit;
  if (tool === 'Bash') return Terminal;
  return Terminal;
}

function formatOffset(secs: number | null): string {
  if (secs === null || secs === undefined) return '—';
  if (secs < 60) return `+${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `+${m}m${s.toString().padStart(2, '0')}s`;
}

function isPhase(ev: DispatchEvent): ev is DispatchPhaseMarker {
  return ev.type === 'phase_marker';
}

function isTool(ev: DispatchEvent): ev is DispatchToolUseEvent {
  return ev.type === 'tool_use';
}

interface Props {
  events: DispatchEvent[];
}

export default function EventTimeline({ events }: Props) {
  const [phaseFilter, setPhaseFilter] = useState<DispatchEventPhase | 'all'>('all');
  const [cursor, setCursor] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);

  const toolEvents = useMemo(() => events.filter(isTool), [events]);

  const phaseCounts = useMemo(() => {
    const counts: Record<string, number> = { all: toolEvents.length };
    let currentPhase: DispatchEventPhase = 'other';
    for (const ev of events) {
      if (isPhase(ev)) {
        currentPhase = ev.phase;
        continue;
      }
      if (isTool(ev)) {
        counts[currentPhase] = (counts[currentPhase] ?? 0) + 1;
      }
    }
    return counts;
  }, [events, toolEvents.length]);

  const toolWithPhase = useMemo(() => {
    const out: Array<{ event: DispatchToolUseEvent; phase: DispatchEventPhase; index: number }> = [];
    let currentPhase: DispatchEventPhase = 'other';
    let idx = 0;
    for (const ev of events) {
      if (isPhase(ev)) {
        currentPhase = ev.phase;
        continue;
      }
      if (isTool(ev)) {
        out.push({ event: ev, phase: currentPhase, index: idx });
        idx += 1;
      }
    }
    return out;
  }, [events]);

  const filtered = useMemo(() => {
    if (phaseFilter === 'all') return toolWithPhase;
    return toolWithPhase.filter(x => x.phase === phaseFilter);
  }, [toolWithPhase, phaseFilter]);

  const totalDuration = useMemo(() => {
    const last = toolEvents[toolEvents.length - 1];
    return last?.timestamp_offset ?? null;
  }, [toolEvents]);

  // Replay: advance cursor through filtered events
  function stepForward() {
    const max = filtered.length - 1;
    setCursor(c => (c === null ? 0 : Math.min(max, c + 1)));
  }
  function stepBack() {
    setCursor(c => (c === null ? null : Math.max(0, c - 1)));
  }
  function reset() {
    setCursor(null);
    setPlaying(false);
  }

  useEffect(() => {
    if (!playing) return;
    const max = filtered.length - 1;
    if (max < 0) {
      setPlaying(false);
      return;
    }
    const timer = setInterval(() => {
      setCursor(c => {
        const next = c === null ? 0 : c + 1;
        if (next >= max) {
          setPlaying(false);
          return max;
        }
        return next;
      });
    }, 400);
    return () => clearInterval(timer);
  }, [playing, filtered.length]);

  if (events.length === 0) {
    return (
      <div
        style={{
          padding: '32px 20px',
          textAlign: 'center',
          fontSize: 13,
          color: 'var(--color-muted)',
          background: 'rgba(255,255,255,0.02)',
          borderRadius: 12,
          border: '1px dashed rgba(255,255,255,0.08)',
        }}
      >
        No tool events recorded in the event archive for this dispatch.
      </div>
    );
  }

  const phases: (DispatchEventPhase | 'all')[] = ['all', 'explore', 'implement', 'test', 'commit', 'other'];

  return (
    <div>
      {/* Replay controls */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 12px',
          marginBottom: 12,
          borderRadius: 10,
          background: 'rgba(255,255,255,0.02)',
          border: '1px solid rgba(255,255,255,0.05)',
          flexWrap: 'wrap',
        }}
      >
        <button
          onClick={reset}
          data-testid="replay-reset"
          title="Reset"
          style={iconBtn}
        >
          <Rewind size={14} />
        </button>
        <button
          onClick={stepBack}
          data-testid="replay-back"
          disabled={cursor === null || cursor === 0}
          style={{ ...iconBtn, opacity: cursor === null || cursor === 0 ? 0.4 : 1 }}
        >
          <Rewind size={12} />
        </button>
        <button
          onClick={() => setPlaying(p => !p)}
          data-testid="replay-play"
          style={{ ...iconBtn, color: playing ? 'var(--color-accent)' : 'var(--color-foreground)' }}
        >
          {playing ? <Pause size={14} /> : <Play size={14} />}
        </button>
        <button
          onClick={stepForward}
          data-testid="replay-forward"
          disabled={cursor !== null && cursor >= filtered.length - 1}
          style={{
            ...iconBtn,
            opacity: cursor !== null && cursor >= filtered.length - 1 ? 0.4 : 1,
          }}
        >
          <FastForward size={12} />
        </button>
        <div style={{ flex: 1, minWidth: 120 }}>
          <input
            type="range"
            min={-1}
            max={filtered.length - 1}
            value={cursor ?? -1}
            onChange={e => {
              const v = Number(e.target.value);
              setCursor(v < 0 ? null : v);
            }}
            data-testid="replay-scrub"
            style={{ width: '100%', accentColor: 'var(--color-accent)' }}
          />
        </div>
        <span
          style={{
            fontSize: 11,
            color: 'var(--color-muted)',
            fontFamily: 'var(--font-mono, monospace)',
            minWidth: 80,
            textAlign: 'right',
          }}
        >
          {cursor === null ? `0 / ${filtered.length}` : `${cursor + 1} / ${filtered.length}`}
          {totalDuration !== null && (
            <span style={{ marginLeft: 6, opacity: 0.7 }}>
              · {formatOffset(totalDuration)}
            </span>
          )}
        </span>
      </div>

      {/* Phase filter chips */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 6,
          marginBottom: 12,
        }}
      >
        {phases.map(p => {
          const active = phaseFilter === p;
          const meta = p === 'all' ? null : PHASE_META[p];
          return (
            <button
              key={p}
              onClick={() => {
                setPhaseFilter(p);
                setCursor(null);
              }}
              data-testid={`phase-filter-${p}`}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                padding: '4px 10px',
                borderRadius: 16,
                fontSize: 10,
                fontWeight: active ? 600 : 400,
                background: active
                  ? meta
                    ? `${meta.color}22`
                    : 'rgba(249, 115, 22, 0.12)'
                  : 'rgba(255,255,255,0.04)',
                border: `1px solid ${
                  active
                    ? meta
                      ? `${meta.color}66`
                      : 'rgba(249, 115, 22, 0.35)'
                    : 'rgba(255,255,255,0.08)'
                }`,
                color: active ? (meta?.color ?? 'var(--color-accent)') : 'var(--color-muted)',
                cursor: 'pointer',
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
              }}
            >
              <span>{p === 'all' ? 'All' : meta!.label}</span>
              <span style={{ fontSize: 9, opacity: 0.8 }}>({phaseCounts[p] ?? 0})</span>
            </button>
          );
        })}
      </div>

      {/* Timeline */}
      <div
        role="list"
        aria-label="Event timeline"
        data-testid="event-timeline"
        style={{
          position: 'relative',
          paddingLeft: 22,
        }}
      >
        <div
          aria-hidden="true"
          style={{
            position: 'absolute',
            left: 7,
            top: 4,
            bottom: 4,
            width: 1,
            background: 'rgba(255,255,255,0.08)',
          }}
        />
        {filtered.map((item, i) => {
          const { event, phase } = item;
          const Icon = toolIcon(event.tool_name);
          const meta = PHASE_META[phase];
          const isActive = cursor !== null && i === cursor;
          const isFaded = cursor !== null && i > cursor;

          return (
            <div
              key={i}
              role="listitem"
              data-testid={`event-${i}`}
              data-active={isActive ? 'true' : undefined}
              style={{
                position: 'relative',
                display: 'flex',
                gap: 10,
                padding: '6px 10px',
                marginBottom: 4,
                borderRadius: 8,
                background: isActive ? 'rgba(249, 115, 22, 0.08)' : 'transparent',
                border: `1px solid ${isActive ? 'rgba(249, 115, 22, 0.35)' : 'transparent'}`,
                opacity: isFaded ? 0.35 : 1,
                transition: 'opacity 0.15s, background 0.15s',
              }}
            >
              <span
                aria-hidden="true"
                style={{
                  position: 'absolute',
                  left: -19,
                  top: 12,
                  width: 10,
                  height: 10,
                  borderRadius: '50%',
                  background: meta.color,
                  boxShadow: isActive
                    ? `0 0 0 3px ${meta.color}33`
                    : `0 0 0 2px rgba(0,0,0,0.4)`,
                }}
              />
              <Icon
                size={13}
                style={{
                  color: meta.color,
                  flexShrink: 0,
                  marginTop: 2,
                }}
              />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: 'var(--color-foreground)',
                    }}
                  >
                    {event.tool_name}
                  </span>
                  <span
                    style={{
                      fontSize: 9,
                      padding: '0 6px',
                      borderRadius: 10,
                      background: `${meta.color}1a`,
                      color: meta.color,
                      textTransform: 'uppercase',
                      letterSpacing: '0.04em',
                      fontWeight: 600,
                    }}
                  >
                    {meta.label}
                  </span>
                  <span
                    style={{
                      fontSize: 10,
                      color: 'var(--color-muted)',
                      fontFamily: 'var(--font-mono, monospace)',
                      marginLeft: 'auto',
                    }}
                  >
                    {formatOffset(event.timestamp_offset)}
                  </span>
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: 'var(--color-muted)',
                    fontFamily: 'var(--font-mono, monospace)',
                    marginTop: 2,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={event.summary}
                >
                  {event.summary || event.file_path || '—'}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

const iconBtn: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 28,
  height: 28,
  borderRadius: 7,
  background: 'rgba(255,255,255,0.05)',
  border: '1px solid rgba(255,255,255,0.08)',
  color: 'var(--color-foreground)',
  cursor: 'pointer',
};
