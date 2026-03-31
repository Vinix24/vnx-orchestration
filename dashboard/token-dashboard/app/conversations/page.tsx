'use client';

import { useState, useCallback } from 'react';
import { useConversations } from '@/lib/hooks';
import ConversationTimeline from '@/components/conversation-timeline';
import type { SortOrder, ConversationSession } from '@/lib/types';

const ALL_TERMINALS = new Set(['T0', 'T1', 'T2', 'T3']);

export default function ConversationsPage() {
  const [sortOrder, setSortOrder] = useState<SortOrder>('DESC');
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [terminals, setTerminals] = useState<Set<string>>(new Set(ALL_TERMINALS));

  const { data, error, isLoading } = useConversations(sortOrder);

  const handleSortToggle = useCallback(() => {
    setSortOrder((prev) => (prev === 'DESC' ? 'ASC' : 'DESC'));
  }, []);

  const handleSelectSession = useCallback((id: string) => {
    setSelectedSessionId((prev) => (prev === id ? null : id));
  }, []);

  const selected = data?.sessions.find((s) => s.session_id === selectedSessionId) ?? null;

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Conversations</h2>
      </div>

      {error && (
        <div
          className="glass-card"
          style={{
            padding: '16px 20px',
            marginBottom: 24,
            borderColor: 'var(--color-error)',
            color: 'var(--color-error)',
            fontSize: 14,
          }}
        >
          Failed to load conversations. Ensure the API server is running.
        </div>
      )}

      {isLoading && (
        <div className="flex items-center justify-center py-20">
          <div
            className="animate-spin w-8 h-8 border-2 rounded-full"
            style={{
              borderColor: 'var(--color-card-border)',
              borderTopColor: 'var(--color-accent)',
            }}
          />
        </div>
      )}

      {data && (
        <div className="flex gap-6" style={{ alignItems: 'flex-start' }}>
          {/* Timeline list */}
          <div style={{ flex: '1 1 0%', minWidth: 0 }}>
            <ConversationTimeline
              sessions={data.sessions}
              sortOrder={sortOrder}
              onSortToggle={handleSortToggle}
              selectedSessionId={selectedSessionId}
              onSelectSession={handleSelectSession}
              rotationChains={data.rotation_chains}
              worktreeGroups={data.worktree_groups}
              terminalFilter={terminals}
              onTerminalFilterChange={setTerminals}
            />
          </div>

          {/* Detail panel */}
          {selected && (
            <div
              className="glass-card animate-in-fast"
              style={{
                width: 340,
                padding: 20,
                flexShrink: 0,
                position: 'sticky',
                top: 32,
              }}
            >
              <SessionDetailPanel session={selected} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SessionDetailPanel({ session }: { session: ConversationSession }) {
  const terminalColors: Record<string, string> = {
    T0: '#6B8AE6',
    T1: '#50fa7b',
    T2: '#facc15',
    T3: '#9B6BE6',
  };
  const color = session.terminal ? terminalColors[session.terminal] ?? '#6B6B6B' : '#6B6B6B';

  return (
    <div>
      <div className="flex items-center gap-2 mb-4">
        {session.terminal && (
          <span
            className="text-xs font-semibold"
            style={{
              padding: '2px 8px',
              borderRadius: 6,
              backgroundColor: `${color}18`,
              color,
              border: `1px solid ${color}30`,
            }}
          >
            {session.terminal}
          </span>
        )}
        <h3
          className="text-sm font-semibold truncate"
          style={{ color: 'var(--color-foreground)' }}
        >
          {session.title || 'Untitled session'}
        </h3>
      </div>

      <div className="flex flex-col gap-3">
        <DetailRow label="Session ID" value={session.session_id} mono />
        <DetailRow label="Last Activity" value={session.last_message ?? 'None'} />
        <DetailRow label="Messages" value={String(session.message_count)} />
        <DetailRow label="User Messages" value={String(session.user_message_count)} />
        <DetailRow label="Total Tokens" value={formatDetailTokens(session.total_tokens)} />
        {session.worktree_root && (
          <DetailRow
            label="Worktree"
            value={session.worktree_root}
            mono
            warn={!session.worktree_exists}
          />
        )}
        <DetailRow label="Project" value={session.project_path} mono />
        <DetailRow label="CWD" value={session.cwd} mono />
      </div>
    </div>
  );
}

function DetailRow({
  label,
  value,
  mono,
  warn,
}: {
  label: string;
  value: string;
  mono?: boolean;
  warn?: boolean;
}) {
  return (
    <div>
      <div
        className="text-xs font-medium mb-0.5"
        style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}
      >
        {label}
      </div>
      <div
        className="text-sm break-all"
        style={{
          color: warn ? 'var(--color-error)' : 'var(--color-foreground)',
          fontFamily: mono ? 'ui-monospace, monospace' : undefined,
          fontSize: mono ? 12 : undefined,
          opacity: mono ? 0.85 : 1,
        }}
      >
        {value}
        {warn && <span style={{ fontSize: 11, marginLeft: 6 }}>(stale)</span>}
      </div>
    </div>
  );
}

function formatDetailTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(2)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return String(tokens);
}
