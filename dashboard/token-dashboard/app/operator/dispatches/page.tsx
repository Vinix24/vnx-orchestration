'use client';

import { useMemo, useState } from 'react';
import { RefreshCw, ClipboardList, Search } from 'lucide-react';
import { useDispatches } from '@/lib/hooks';
import type { DispatchStage, DispatchSummary } from '@/lib/types';
import DispatchList from '@/components/operator/dispatch-list';

const ALL_STAGES: (DispatchStage | 'all')[] = ['all', 'pending', 'active', 'review', 'done', 'staging'];

const STAGE_LABELS: Record<string, string> = {
  all: 'All',
  staging: 'Staging',
  pending: 'Pending',
  active: 'Active',
  review: 'Review',
  done: 'Done',
};

function LoadingSkeleton() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {[0, 1, 2, 3, 4, 5].map(i => (
        <div
          key={i}
          aria-hidden="true"
          style={{
            height: 56,
            borderRadius: 8,
            background:
              'linear-gradient(90deg, rgba(255,255,255,0.03), rgba(255,255,255,0.06), rgba(255,255,255,0.03))',
            backgroundSize: '200% 100%',
            animation: 'pulse 1.4s ease-in-out infinite',
          }}
        />
      ))}
      <style jsx>{`
        @keyframes pulse {
          0%, 100% { background-position: 0% 0%; }
          50% { background-position: 100% 0%; }
        }
      `}</style>
    </div>
  );
}

export default function DispatchesPage() {
  const { data, error, isLoading, mutate } = useDispatches();
  const [stageFilter, setStageFilter] = useState<DispatchStage | 'all'>('all');
  const [trackFilter, setTrackFilter] = useState<string>('');
  const [terminalFilter, setTerminalFilter] = useState<string>('');
  const [query, setQuery] = useState<string>('');

  const allDispatches: DispatchSummary[] = useMemo(() => {
    if (!data?.stages) return [];
    const out: DispatchSummary[] = [];
    for (const stage of ['pending', 'active', 'review', 'done', 'staging'] as DispatchStage[]) {
      const group = data.stages[stage];
      if (group) out.push(...group);
    }
    return out;
  }, [data]);

  const stageCounts = useMemo(() => {
    const counts: Record<string, number> = { all: allDispatches.length };
    for (const stage of ALL_STAGES) {
      if (stage === 'all') continue;
      counts[stage] = data?.stages?.[stage]?.length ?? 0;
    }
    return counts;
  }, [data, allDispatches]);

  const { tracks, terminals } = useMemo(() => {
    const t = new Set<string>();
    const term = new Set<string>();
    for (const d of allDispatches) {
      if (d.track && d.track !== '—') t.add(d.track);
      if (d.terminal && d.terminal !== '—') term.add(d.terminal);
    }
    return {
      tracks: Array.from(t).sort(),
      terminals: Array.from(term).sort(),
    };
  }, [allDispatches]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return allDispatches.filter(d => {
      if (stageFilter !== 'all' && d.stage !== stageFilter) return false;
      if (trackFilter && d.track !== trackFilter) return false;
      if (terminalFilter && d.terminal !== terminalFilter) return false;
      if (q && !(
        d.id.toLowerCase().includes(q) ||
        (d.gate ?? '').toLowerCase().includes(q) ||
        (d.pr_id ?? '').toLowerCase().includes(q) ||
        (d.role ?? '').toLowerCase().includes(q)
      )) return false;
      return true;
    });
  }, [allDispatches, stageFilter, trackFilter, terminalFilter, query]);

  return (
    <div>
      <div className="flex items-center justify-between" style={{ marginBottom: 24 }}>
        <div className="flex items-center gap-3">
          <div
            style={{
              height: 28,
              width: 4,
              borderRadius: 2,
              background: 'var(--color-accent)',
            }}
          />
          <div>
            <h2
              style={{
                fontSize: '1.5rem',
                fontWeight: 700,
                letterSpacing: '-0.02em',
                color: 'var(--color-foreground)',
              }}
            >
              Dispatches
            </h2>
            <p style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
              Browse dispatch queue and replay execution from archived events
            </p>
          </div>
        </div>
        <button
          onClick={() => mutate()}
          data-testid="dispatches-refresh"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '7px 14px',
            borderRadius: 8,
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.1)',
            cursor: 'pointer',
            fontSize: 12,
            color: 'var(--color-muted)',
          }}
        >
          <RefreshCw size={13} />
          Refresh
        </button>
      </div>

      {/* Stage tabs */}
      <div
        role="tablist"
        aria-label="Filter by stage"
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 6,
          marginBottom: 16,
        }}
      >
        {ALL_STAGES.map(s => {
          const active = stageFilter === s;
          return (
            <button
              key={s}
              role="tab"
              aria-selected={active}
              data-testid={`stage-tab-${s}`}
              onClick={() => setStageFilter(s)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                padding: '6px 12px',
                borderRadius: 8,
                fontSize: 11,
                fontWeight: active ? 600 : 400,
                background: active ? 'rgba(249, 115, 22, 0.12)' : 'rgba(255,255,255,0.04)',
                border: `1px solid ${active ? 'rgba(249, 115, 22, 0.35)' : 'rgba(255,255,255,0.08)'}`,
                color: active ? 'var(--color-accent)' : 'var(--color-muted)',
                cursor: 'pointer',
              }}
            >
              <span>{STAGE_LABELS[s]}</span>
              <span
                style={{
                  fontSize: 10,
                  padding: '1px 6px',
                  borderRadius: 10,
                  background: 'rgba(255,255,255,0.06)',
                  color: active ? 'var(--color-accent)' : 'var(--color-muted)',
                }}
              >
                {stageCounts[s] ?? 0}
              </span>
            </button>
          );
        })}
      </div>

      {/* Secondary filters */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 12,
          marginBottom: 20,
          padding: 12,
          borderRadius: 10,
          background: 'rgba(255,255,255,0.02)',
          border: '1px solid rgba(255,255,255,0.05)',
        }}
      >
        <div style={{ position: 'relative', flex: '1 1 240px', minWidth: 200 }}>
          <Search
            size={12}
            style={{
              position: 'absolute',
              left: 10,
              top: '50%',
              transform: 'translateY(-50%)',
              color: 'var(--color-muted)',
            }}
          />
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search id, gate, PR, role…"
            data-testid="dispatches-search"
            style={{
              width: '100%',
              padding: '7px 10px 7px 28px',
              borderRadius: 7,
              fontSize: 12,
              background: 'rgba(0,0,0,0.25)',
              border: '1px solid rgba(255,255,255,0.08)',
              color: 'var(--color-foreground)',
              outline: 'none',
            }}
          />
        </div>
        <select
          value={trackFilter}
          onChange={e => setTrackFilter(e.target.value)}
          data-testid="track-filter"
          style={{
            padding: '7px 10px',
            borderRadius: 7,
            fontSize: 12,
            background: 'rgba(0,0,0,0.25)',
            border: '1px solid rgba(255,255,255,0.08)',
            color: 'var(--color-foreground)',
          }}
        >
          <option value="">All tracks</option>
          {tracks.map(t => (
            <option key={t} value={t}>Track {t}</option>
          ))}
        </select>
        <select
          value={terminalFilter}
          onChange={e => setTerminalFilter(e.target.value)}
          data-testid="terminal-filter"
          style={{
            padding: '7px 10px',
            borderRadius: 7,
            fontSize: 12,
            background: 'rgba(0,0,0,0.25)',
            border: '1px solid rgba(255,255,255,0.08)',
            color: 'var(--color-foreground)',
          }}
        >
          <option value="">All terminals</option>
          {terminals.map(t => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </div>

      {/* Result count */}
      <div
        className="flex items-center gap-2"
        style={{ marginBottom: 10 }}
      >
        <ClipboardList size={14} style={{ color: 'var(--color-accent)' }} />
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-foreground)' }}>
          {isLoading ? 'Loading dispatches…' : `${filtered.length} dispatch${filtered.length === 1 ? '' : 'es'}`}
        </span>
      </div>

      {/* Body */}
      {error ? (
        <div
          style={{
            padding: 16,
            borderRadius: 10,
            background: 'rgba(239, 68, 68, 0.08)',
            border: '1px solid rgba(239, 68, 68, 0.25)',
            color: '#fca5a5',
            fontSize: 13,
          }}
        >
          Failed to load dispatches: {String(error)}
        </div>
      ) : isLoading ? (
        <LoadingSkeleton />
      ) : (
        <DispatchList dispatches={filtered} />
      )}
    </div>
  );
}
