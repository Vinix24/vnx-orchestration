'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, Archive, Circle, Pause, Play, Radio } from 'lucide-react';
import { fetchAgentStreamArchiveList, fetchAgentStreamArchive, type ArchiveEntry } from '@/lib/api';

const TERMINALS = ['T1', 'T2', 'T3'] as const;
type Terminal = (typeof TERMINALS)[number];
type StreamMode = 'live' | 'archive';

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

function formatRelTime(epochSeconds: number): string {
  const diffMs = Date.now() - epochSeconds * 1000;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  return `${Math.floor(diffH / 24)}d ago`;
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
  const [mode, setMode] = useState<StreamMode>('live');
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [status, setStatus] = useState<Record<string, TerminalStatus>>({});
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);

  // Archive state
  const [archives, setArchives] = useState<ArchiveEntry[]>([]);
  const [selectedArchive, setSelectedArchive] = useState<string>('');
  const [archiveLoading, setArchiveLoading] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const statusIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastTimestampRef = useRef<string | null>(null);
  const pausedRef = useRef(false);
  const terminalRef = useRef<Terminal>('T1');
  const activeTerminalRef = useRef<Terminal | null>(null);
  const isNavigatingAwayRef = useRef(false);

  useEffect(() => { pausedRef.current = paused; }, [paused]);
  useEffect(() => { terminalRef.current = terminal; }, [terminal]);

  const clearStatusPolling = useCallback(() => {
    if (statusIntervalRef.current) {
      clearInterval(statusIntervalRef.current);
      statusIntervalRef.current = null;
    }
  }, []);

  const disconnectStream = useCallback(() => {
    activeTerminalRef.current = null;
    if (eventSourceRef.current) {
      eventSourceRef.current.onopen = null;
      eventSourceRef.current.onmessage = null;
      eventSourceRef.current.onerror = null;
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    setConnected(false);
  }, []);

  const pollStatus = useCallback(async () => {
    if (isNavigatingAwayRef.current) return;
    try {
      const res = await fetch('/api/agent-stream/status');
      if (res.ok && !isNavigatingAwayRef.current) {
        const data = await res.json();
        if (!isNavigatingAwayRef.current) {
          setStatus(data.terminals ?? {});
        }
      }
    } catch {
      // non-critical
    }
  }, []);

  const startStatusPolling = useCallback(() => {
    if (statusIntervalRef.current || isNavigatingAwayRef.current) return;
    statusIntervalRef.current = setInterval(() => { void pollStatus(); }, 5000);
  }, [pollStatus]);

  const connect = useCallback((term: Terminal, since: string | null) => {
    disconnectStream();
    if (isNavigatingAwayRef.current) return;

    let url = `/api/agent-stream/${term}`;
    if (since) url += `?since=${encodeURIComponent(since)}`;

    const es = new EventSource(url);
    eventSourceRef.current = es;
    activeTerminalRef.current = term;

    es.onopen = () => {
      if (eventSourceRef.current !== es || isNavigatingAwayRef.current) return;
      setConnected(true);
      setError(null);
    };

    es.onmessage = (msg) => {
      if (eventSourceRef.current !== es || isNavigatingAwayRef.current) return;
      try {
        const event: StreamEvent = JSON.parse(msg.data);
        if (event.timestamp) lastTimestampRef.current = event.timestamp;
        if (!pausedRef.current) setEvents((prev) => [...prev, event]);
      } catch {
        // skip malformed
      }
    };

    es.onerror = () => {
      if (eventSourceRef.current !== es || isNavigatingAwayRef.current) return;
      setConnected(false);
      setError('SSE connection failed — retrying…');
    };
  }, [disconnectStream]);

  // Load archive list when switching to archive mode or changing terminal
  useEffect(() => {
    if (mode !== 'archive') return;
    let cancelled = false;
    fetchAgentStreamArchiveList(terminal)
      .then((list) => {
        if (!cancelled) {
          const sorted = [...list].sort((a, b) => b.modified_at - a.modified_at).slice(0, 20);
          setArchives(sorted);
          setSelectedArchive(sorted[0]?.dispatch_id ?? '');
        }
      })
      .catch(() => {
        if (!cancelled) {
          setArchives([]);
          setSelectedArchive('');
        }
      });
    return () => { cancelled = true; };
  }, [mode, terminal]);

  // Load archive events when selection changes
  useEffect(() => {
    if (mode !== 'archive' || !selectedArchive) {
      if (mode === 'archive') setEvents([]);
      return;
    }
    setArchiveLoading(true);
    setError(null);
    fetchAgentStreamArchive(terminal, selectedArchive)
      .then((raw) => {
        setEvents(raw as unknown as StreamEvent[]);
        setArchiveLoading(false);
      })
      .catch((err: unknown) => {
        setError(String(err));
        setArchiveLoading(false);
      });
  }, [mode, terminal, selectedArchive]);

  useEffect(() => {
    function handleDocumentClick(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (!(event.target instanceof Element)) return;
      const anchor = event.target.closest('a[href]');
      const href = anchor?.getAttribute('href');
      if (!href || href.startsWith('#')) return;
      const nextUrl = new URL(href, window.location.href);
      if (nextUrl.origin !== window.location.origin) return;
      if (nextUrl.pathname !== '/agent-stream') {
        isNavigatingAwayRef.current = true;
        clearStatusPolling();
        disconnectStream();
        return;
      }
      if (!isNavigatingAwayRef.current) return;
      isNavigatingAwayRef.current = false;
      setEvents([]);
      setError(null);
      lastTimestampRef.current = null;
      startStatusPolling();
      connect(terminalRef.current, null);
    }
    document.addEventListener('click', handleDocumentClick, true);
    return () => { document.removeEventListener('click', handleDocumentClick, true); };
  }, [clearStatusPolling, connect, disconnectStream, startStatusPolling]);

  useEffect(() => {
    isNavigatingAwayRef.current = false;
    startStatusPolling();
    return clearStatusPolling;
  }, [clearStatusPolling, startStatusPolling]);

  // Connect live stream when terminal changes (live mode only)
  useEffect(() => {
    if (mode !== 'live') return;
    isNavigatingAwayRef.current = false;
    setEvents([]);
    setError(null);
    lastTimestampRef.current = null;
    if (eventSourceRef.current && activeTerminalRef.current === terminal) return;
    connect(terminal, null);
    return () => { disconnectStream(); };
  }, [terminal, mode, connect, disconnectStream]);

  // Disconnect SSE when switching to archive mode
  useEffect(() => {
    if (mode === 'archive') {
      disconnectStream();
      setEvents([]);
      setError(null);
    }
  }, [mode, disconnectStream]);

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
              {mode === 'live' ? 'Real-time event stream from worker terminals' : 'Archived dispatch replay'}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* Mode toggle */}
          <div style={{ display: 'flex', gap: 4, background: 'rgba(255,255,255,0.04)', borderRadius: 10, padding: 3 }}>
            {(['live', 'archive'] as StreamMode[]).map((m) => {
              const active = m === mode;
              return (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 5,
                    padding: '6px 14px',
                    borderRadius: 8,
                    background: active ? 'rgba(249, 115, 22, 0.15)' : 'transparent',
                    border: `1px solid ${active ? 'rgba(249, 115, 22, 0.3)' : 'transparent'}`,
                    cursor: 'pointer',
                    fontSize: 12,
                    fontWeight: active ? 600 : 400,
                    color: active ? 'var(--color-accent)' : 'var(--color-muted)',
                  }}
                >
                  {m === 'live' ? <Radio size={12} /> : <Archive size={12} />}
                  {m === 'live' ? 'Live' : 'Archive'}
                </button>
              );
            })}
          </div>

          {/* Connection status (live mode only) */}
          {mode === 'live' && (
            <div className="flex items-center gap-2" style={{ fontSize: 12, color: connected ? '#50fa7b' : 'var(--color-muted)' }}>
              <Circle size={8} fill={connected ? '#50fa7b' : 'var(--color-muted)'} />
              {connected ? 'Connected' : 'Disconnected'}
            </div>
          )}

          {/* Pause/Resume (live mode only) */}
          {mode === 'live' && (
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
          )}

          {/* Terminal selector */}
          <div data-testid="agent-selector" style={{ display: 'flex', gap: 4 }}>
            {TERMINALS.map((t) => {
              const active = t === terminal;
              const ts = status[t];
              const hasEvents = !!ts;
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

      {/* Archive selector (archive mode) */}
      {mode === 'archive' && (
        <div
          style={{
            marginBottom: 16,
            padding: '12px 16px',
            background: 'rgba(255,255,255,0.02)',
            borderRadius: 8,
            border: '1px solid rgba(255,255,255,0.07)',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <Archive size={14} style={{ color: 'var(--color-muted)', flexShrink: 0 }} />
          <span style={{ fontSize: 12, color: 'var(--color-muted)', flexShrink: 0 }}>Dispatch:</span>
          {archives.length === 0 ? (
            <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>No archives for {terminal}</span>
          ) : (
            <select
              value={selectedArchive}
              onChange={(e) => setSelectedArchive(e.target.value)}
              style={{
                background: 'rgba(255,255,255,0.06)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: 6,
                color: 'var(--color-foreground)',
                fontSize: 12,
                padding: '4px 8px',
                cursor: 'pointer',
                maxWidth: 480,
                flex: 1,
              }}
            >
              {archives.map((a) => (
                <option key={a.dispatch_id} value={a.dispatch_id}>
                  {a.dispatch_id} — {formatRelTime(a.modified_at)}, {a.file_size} bytes
                </option>
              ))}
            </select>
          )}
          {archiveLoading && (
            <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>Loading…</span>
          )}
        </div>
      )}

      {/* Status bar (live mode) */}
      {mode === 'live' && terminalStatus && (
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

      {/* Archive status bar */}
      {mode === 'archive' && events.length > 0 && (
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
          <span>Events: <strong style={{ color: 'var(--color-foreground)' }}>{events.length}</strong></span>
          {events[0]?.timestamp && (
            <span>From: <strong style={{ color: 'var(--color-foreground)' }}>{formatTime(events[0].timestamp)}</strong></span>
          )}
          {events[events.length - 1]?.timestamp && (
            <span>To: <strong style={{ color: 'var(--color-foreground)' }}>{formatTime(events[events.length - 1].timestamp)}</strong></span>
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
              {mode === 'live'
                ? (connected ? 'Waiting for events...' : `No events for ${terminal}`)
                : (archiveLoading ? 'Loading archive...' : (selectedArchive ? 'No events in this archive' : `No archives for ${terminal}`))}
            </span>
          </div>
        ) : (
          events.map((ev, i) => <EventRow key={`${ev.terminal}-${ev.sequence ?? i}-${i}`} event={ev} />)
        )}
      </div>
    </div>
  );
}
