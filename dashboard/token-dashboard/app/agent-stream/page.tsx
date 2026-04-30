'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, Circle, Pause, Play } from 'lucide-react';

const TERMINALS = ['T1', 'T2', 'T3'] as const;
type Terminal = (typeof TERMINALS)[number];

interface StreamEvent {
  type: string;
  timestamp: string;
  terminal: string;
  sequence: number;
  dispatch_id: string;
  data: Record<string, unknown>;
}

interface TerminalStatus {
  event_count: number;
  last_timestamp: string | null;
  agent_name?: string;
  domain?: string;
}

const EVENT_COLORS: Record<string, string> = {
  thinking: 'rgba(160, 170, 190, 0.85)',
  tool_use: '#6B8AE6',
  tool_result: '#50fa7b',
  text: 'var(--color-foreground)',
  result: 'var(--color-foreground)',
  error: '#ff6b6b',
  init: 'var(--color-accent)',
};

const EVENT_BG: Record<string, string> = {
  thinking: 'rgba(160, 170, 190, 0.05)',
  tool_use: 'rgba(107, 138, 230, 0.08)',
  tool_result: 'rgba(80, 250, 123, 0.06)',
  error: 'rgba(255, 107, 107, 0.08)',
  init: 'rgba(249, 115, 22, 0.08)',
};

function EventBadge({ type }: { type: string }) {
  const color = EVENT_COLORS[type] ?? 'var(--color-muted)';
  return (
    <span
      style={{
        display: 'inline-block',
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        color,
        background: EVENT_BG[type] ?? 'rgba(255,255,255,0.04)',
        padding: '2px 8px',
        borderRadius: 4,
        border: `1px solid ${color}33`,
        flexShrink: 0,
      }}
    >
      {type}
    </span>
  );
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return iso;
  }
}

function renderEventContent(event: StreamEvent): string {
  const d = event.data;
  if (event.type === 'text' || event.type === 'result') {
    return String(d?.text ?? d?.content ?? JSON.stringify(d));
  }
  if (event.type === 'thinking') {
    return String(d?.thinking ?? d?.text ?? JSON.stringify(d));
  }
  if (event.type === 'tool_use') {
    const name = String(d?.name ?? d?.tool ?? '');
    return name ? `${name}(...)` : JSON.stringify(d);
  }
  if (event.type === 'tool_result') {
    const content = String(d?.content ?? d?.output ?? d?.text ?? '');
    return content.length > 300 ? content.slice(0, 300) + '...' : content || JSON.stringify(d);
  }
  if (event.type === 'error') {
    return String(d?.error ?? d?.message ?? JSON.stringify(d));
  }
  if (event.type === 'init') {
    return `Session started: ${d?.session_id ?? d?.dispatch_id ?? ''}`;
  }
  return JSON.stringify(d);
}

function EventRow({ event }: { event: StreamEvent }) {
  const color = EVENT_COLORS[event.type] ?? 'var(--color-muted)';
  return (
    <div
      style={{
        display: 'flex',
        gap: 12,
        alignItems: 'flex-start',
        padding: '8px 12px',
        borderBottom: '1px solid rgba(255,255,255,0.04)',
        fontSize: 13,
        lineHeight: '1.5',
      }}
    >
      <span style={{ color: 'var(--color-muted)', fontSize: 11, flexShrink: 0, marginTop: 2, fontFamily: 'monospace' }}>
        {formatTime(event.timestamp)}
      </span>
      <EventBadge type={event.type} />
      <span
        style={{
          color,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          fontFamily: event.type === 'thinking' ? 'inherit' : 'monospace',
          fontStyle: event.type === 'thinking' ? 'italic' : 'normal',
          flex: 1,
        }}
      >
        {renderEventContent(event)}
      </span>
    </div>
  );
}

export default function AgentStreamPage() {
  const [terminal, setTerminal] = useState<Terminal>('T1');
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [status, setStatus] = useState<Record<string, TerminalStatus>>({});
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const lastTimestampRef = useRef<string | null>(null);
  const pausedRef = useRef(false);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Keep ref in sync with state for use in EventSource callbacks
  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  // Fetch status endpoint
  useEffect(() => {
    let cancelled = false;
    async function fetchStatus() {
      try {
        const res = await fetch('/api/agent-stream/status');
        if (res.ok) {
          const data = await res.json();
          if (!cancelled) setStatus(data.terminals ?? {});
        }
      } catch {
        // non-critical
      }
    }
    fetchStatus();
    const iv = setInterval(fetchStatus, 5000);
    return () => { cancelled = true; clearInterval(iv); };
  }, []);

  // Connect to SSE
  const connect = useCallback((term: Terminal, since: string | null) => {
    // Cancel any pending retry before opening a new connection
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    // Close existing connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    let url = `/api/agent-stream/${term}`;
    if (since) url += `?since=${encodeURIComponent(since)}`;

    const es = new EventSource(url);
    eventSourceRef.current = es;

    es.onopen = () => {
      setConnected(true);
      setError(null);
    };

    es.onmessage = (msg) => {
      try {
        const event: StreamEvent = JSON.parse(msg.data);
        if (event.timestamp) {
          lastTimestampRef.current = event.timestamp;
        }
        if (!pausedRef.current) {
          setEvents((prev) => [...prev, event]);
        }
      } catch {
        // skip malformed
      }
    };

    es.onerror = () => {
      setConnected(false);
      setError('SSE connection failed — retrying…');
      es.close();
      eventSourceRef.current = null;
      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null;
        connect(term, lastTimestampRef.current);
      }, 2000);
    };
  }, []);

  // Connect when terminal changes
  useEffect(() => {
    setEvents([]);
    setError(null);
    lastTimestampRef.current = null;
    connect(terminal, null);

    return () => {
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      setConnected(false);
    };
  }, [terminal, connect]);

  // Auto-scroll
  useEffect(() => {
    if (!paused && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events, paused]);

  const terminalStatus = status[terminal];

  return (
    <div>
      {/* Page header */}
      <div className="flex items-center justify-between" style={{ marginBottom: 24 }}>
        <div className="flex items-center gap-3">
          <div style={{ height: 28, width: 4, borderRadius: 2, background: 'var(--color-accent)' }} />
          <div>
            <h2
              style={{
                fontSize: '1.5rem',
                fontWeight: 700,
                letterSpacing: '-0.02em',
                color: 'var(--color-foreground)',
              }}
            >
              Agent Stream
            </h2>
            <p style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
              Real-time event stream from worker terminals
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* Connection status */}
          <div className="flex items-center gap-2" style={{ fontSize: 12, color: connected ? '#50fa7b' : 'var(--color-muted)' }}>
            <Circle size={8} fill={connected ? '#50fa7b' : 'var(--color-muted)'} />
            {connected ? 'Connected' : 'Disconnected'}
          </div>

          {/* Pause/Resume */}
          <button
            onClick={() => setPaused(!paused)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '7px 14px',
              borderRadius: 8,
              background: paused ? 'rgba(249, 115, 22, 0.15)' : 'rgba(255,255,255,0.05)',
              border: `1px solid ${paused ? 'rgba(249, 115, 22, 0.3)' : 'rgba(255,255,255,0.1)'}`,
              cursor: 'pointer',
              fontSize: 12,
              color: paused ? 'var(--color-accent)' : 'var(--color-muted)',
            }}
          >
            {paused ? <Play size={13} /> : <Pause size={13} />}
            {paused ? 'Resume' : 'Pause'}
          </button>

          {/* Agent / Terminal selector */}
          <div data-testid="agent-selector" style={{ display: 'flex', gap: 4 }}>
            {TERMINALS.map((t) => {
              const active = t === terminal;
              const hasEvents = !!status[t];
              const ts = status[t];
              const label = ts?.agent_name || t;
              return (
                <button
                  key={t}
                  onClick={() => setTerminal(t)}
                  title={ts?.domain ? `${label} (${ts.domain})` : label}
                  style={{
                    padding: '7px 16px',
                    borderRadius: 8,
                    background: active ? 'rgba(249, 115, 22, 0.15)' : 'rgba(255,255,255,0.05)',
                    border: `1px solid ${active ? 'rgba(249, 115, 22, 0.3)' : 'rgba(255,255,255,0.1)'}`,
                    cursor: 'pointer',
                    fontSize: 12,
                    fontWeight: active ? 600 : 400,
                    color: active ? 'var(--color-accent)' : 'var(--color-muted)',
                    position: 'relative',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    gap: 2,
                  }}
                >
                  <span>{label}</span>
                  {ts?.domain && (
                    <span style={{ fontSize: 9, opacity: 0.6, textTransform: 'capitalize' }}>
                      {ts.domain}
                    </span>
                  )}
                  {hasEvents && (
                    <span
                      style={{
                        position: 'absolute',
                        top: 4,
                        right: 4,
                        width: 6,
                        height: 6,
                        borderRadius: '50%',
                        background: '#50fa7b',
                      }}
                    />
                  )}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Status bar */}
      {terminalStatus && (
        <div
          style={{
            display: 'flex',
            gap: 24,
            padding: '10px 16px',
            marginBottom: 16,
            background: 'rgba(255,255,255,0.02)',
            borderRadius: 8,
            border: '1px solid rgba(255,255,255,0.06)',
            fontSize: 12,
            color: 'var(--color-muted)',
          }}
        >
          <span>Events: <strong style={{ color: 'var(--color-foreground)' }}>{terminalStatus.event_count}</strong></span>
          <span>Displayed: <strong style={{ color: 'var(--color-foreground)' }}>{events.length}</strong></span>
          {terminalStatus.last_timestamp && (
            <span>Last: <strong style={{ color: 'var(--color-foreground)' }}>{formatTime(terminalStatus.last_timestamp)}</strong></span>
          )}
        </div>
      )}

      {/* Error state */}
      {error && (
        <div
          data-testid="sse-error"
          role="alert"
          style={{
            padding: '12px 16px',
            marginBottom: 16,
            background: 'rgba(255, 107, 107, 0.08)',
            borderRadius: 8,
            border: '1px solid rgba(255, 107, 107, 0.2)',
            color: '#ff6b6b',
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}

      {/* Event stream */}
      <div
        ref={scrollRef}
        style={{
          background: 'var(--color-card)',
          borderRadius: 12,
          border: '1px solid var(--color-card-border)',
          height: 'calc(100vh - 240px)',
          overflowY: 'auto',
          overflowX: 'hidden',
        }}
      >
        {events.length === 0 ? (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              height: '100%',
              gap: 12,
              color: 'var(--color-muted)',
            }}
          >
            <Activity size={32} strokeWidth={1.5} />
            <span style={{ fontSize: 13 }}>
              {connected ? 'Waiting for events...' : `No events for ${terminal}`}
            </span>
          </div>
        ) : (
          events.map((ev, i) => <EventRow key={`${ev.terminal}-${ev.sequence}-${i}`} event={ev} />)
        )}
      </div>
    </div>
  );
}
