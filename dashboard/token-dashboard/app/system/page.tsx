'use client';

import { useState } from 'react';
import { usePolling } from '@/lib/use-polling';
import type { DashboardStatus } from '@/lib/types';
import { Settings, RotateCw } from 'lucide-react';

export default function SystemPage() {
  const { data, loading } = usePolling<DashboardStatus>('/state/dashboard_status.json');

  if (loading || !data) {
    return (
      <div className="flex items-center justify-center py-20">
        <div
          className="animate-spin w-8 h-8 border-2 rounded-full"
          style={{ borderColor: 'var(--color-card-border)', borderTopColor: 'var(--color-accent)' }}
        />
      </div>
    );
  }

  const processEntries = Object.entries(data.processes ?? {});
  const runningCount = processEntries.filter(([, info]) => info.running).length;
  const totalCount = processEntries.length;
  const allRunning = totalCount > 0 && runningCount === totalCount;

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>System Monitor</h2>
      </div>

      {/* Process Monitor */}
      <div className="glass-card animate-in" style={{ padding: 24, marginBottom: 20 }}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <span
              style={{
                width: 10,
                height: 10,
                borderRadius: '50%',
                display: 'inline-block',
                background: allRunning ? '#50fa7b' : runningCount === 0 ? '#ff6b6b' : '#f97316',
                boxShadow: allRunning
                  ? '0 0 12px rgba(80, 250, 123, 0.55)'
                  : runningCount === 0
                  ? '0 0 12px rgba(255, 107, 107, 0.55)'
                  : '0 0 12px rgba(249, 115, 22, 0.55)',
              }}
            />
            <h3 className="text-sm font-semibold" style={{ color: 'var(--color-foreground)' }}>
              Processes — {allRunning ? `All ${totalCount} running` : `${runningCount}/${totalCount} running`}
            </h3>
          </div>
        </div>

        <div style={{ display: 'grid', gap: 8 }}>
          {processEntries.map(([name, info]) => (
            <ProcessItem key={name} name={name} running={info.running} pid={info.pid} />
          ))}
        </div>
      </div>

      {/* Queue Stats + Gates + Locks */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 stagger-children">
        {/* Queue Stats */}
        <div className="glass-card" style={{ padding: 24 }}>
          <h3 className="text-xs font-medium mb-4" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
            Queue Stats
          </h3>
          <div style={{ display: 'grid', gap: 12 }}>
            {[
              ['Queue', data.queues?.queue ?? 0],
              ['Pending', data.queues?.pending ?? 0],
              ['Active', data.queues?.active ?? 0],
            ].map(([label, value]) => (
              <div key={String(label)} className="flex justify-between text-sm">
                <span style={{ color: 'var(--color-muted)' }}>{label}</span>
                <span className="font-semibold" style={{ color: 'var(--color-foreground)' }}>{String(value)}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Gate Status */}
        <div className="glass-card" style={{ padding: 24 }}>
          <h3 className="text-xs font-medium mb-4" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
            Gate Status
          </h3>
          <div style={{ display: 'grid', gap: 12 }}>
            {Object.entries(data.gates ?? {}).map(([name, gate]) => (
              <div key={name} className="flex justify-between items-center text-sm">
                <span style={{ color: 'var(--color-muted)' }}>{name}</span>
                <span
                  className="text-xs font-medium"
                  style={{
                    padding: '3px 10px',
                    borderRadius: 20,
                    background: gate.status === 'passed' ? 'rgba(80, 250, 123, 0.15)' : 'rgba(255, 255, 255, 0.06)',
                    color: gate.status === 'passed' ? '#50fa7b' : 'var(--color-muted)',
                  }}
                >
                  {gate.status}
                </span>
              </div>
            ))}
            {Object.keys(data.gates ?? {}).length === 0 && (
              <div className="text-xs" style={{ color: 'var(--color-muted)' }}>No gates configured</div>
            )}
          </div>
        </div>

        {/* Lock Status */}
        <div className="glass-card" style={{ padding: 24 }}>
          <h3 className="text-xs font-medium mb-4" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
            Track Locks
          </h3>
          <div style={{ display: 'grid', gap: 12 }}>
            {Object.entries(data.locks ?? {}).map(([track, lock]) => (
              <div key={track} className="flex justify-between items-center text-sm">
                <span style={{ color: 'var(--color-muted)' }}>Track {track}</span>
                <div className="flex items-center gap-2">
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: '50%',
                      background: lock.locked ? '#ff6b6b' : '#50fa7b',
                      display: 'inline-block',
                    }}
                  />
                  <span className="text-xs" style={{ color: 'var(--color-foreground)' }}>
                    {lock.locked ? 'Locked' : 'Free'}
                  </span>
                </div>
              </div>
            ))}
            {Object.keys(data.locks ?? {}).length === 0 && (
              <div className="text-xs" style={{ color: 'var(--color-muted)' }}>No locks active</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function ProcessItem({ name, running, pid }: { name: string; running: boolean; pid: string }) {
  const [restarting, setRestarting] = useState(false);

  async function handleRestart() {
    if (!confirm(`Restart process "${name}"?`)) return;
    setRestarting(true);
    try {
      const res = await fetch('/api/restart-process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ process: name }),
      });
      if (!res.ok) throw new Error('Restart failed');
    } catch (err) {
      console.error('Restart error:', err);
    } finally {
      setRestarting(false);
    }
  }

  return (
    <div
      className="flex items-center justify-between"
      style={{
        padding: '10px 14px',
        borderRadius: 14,
        border: '1px solid rgba(255, 255, 255, 0.08)',
        background: 'rgba(255, 255, 255, 0.04)',
      }}
    >
      <div>
        <div className="text-sm font-semibold" style={{ color: 'var(--color-foreground)' }}>{name}</div>
        <div className="text-xs" style={{ color: 'var(--color-muted)' }}>
          PID {pid || '—'} · {running ? 'running' : 'stopped'}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <span
          style={{
            width: 9,
            height: 9,
            borderRadius: '50%',
            background: running ? '#50fa7b' : '#ff6b6b',
            boxShadow: running ? '0 0 10px rgba(80, 250, 123, 0.5)' : '0 0 10px rgba(255, 107, 107, 0.5)',
          }}
        />
        <button
          onClick={handleRestart}
          disabled={restarting}
          className="flex items-center gap-1.5 text-xs font-medium transition-all"
          style={{
            padding: '5px 12px',
            borderRadius: 20,
            border: '1px solid rgba(255, 255, 255, 0.12)',
            background: 'rgba(0, 0, 0, 0.25)',
            color: 'var(--color-foreground)',
            cursor: restarting ? 'not-allowed' : 'pointer',
            opacity: restarting ? 0.5 : 1,
          }}
        >
          <RotateCw size={12} className={restarting ? 'animate-spin' : ''} />
          {restarting ? 'Restarting...' : 'Restart'}
        </button>
      </div>
    </div>
  );
}
