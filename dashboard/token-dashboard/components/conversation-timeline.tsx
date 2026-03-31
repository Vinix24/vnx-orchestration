'use client';

import { TERMINAL_COLORS } from '@/lib/types';
import type { ConversationSession, RotationChain, SortOrder } from '@/lib/types';

interface SortToggleProps {
  sortOrder: SortOrder;
  onToggle: () => void;
}

function SortToggle({ sortOrder, onToggle }: SortToggleProps) {
  const isLatestFirst = sortOrder === 'DESC';
  return (
    <button
      onClick={onToggle}
      className="flex items-center gap-2 text-xs font-medium transition-all"
      style={{
        padding: '6px 14px',
        borderRadius: 20,
        border: '1.5px solid rgba(255, 255, 255, 0.12)',
        backgroundColor: 'rgba(255, 255, 255, 0.04)',
        color: 'var(--color-foreground)',
        cursor: 'pointer',
      }}
      title={isLatestFirst ? 'Showing newest first — click for oldest first' : 'Showing oldest first — click for newest first'}
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 14 14"
        fill="none"
        style={{
          transform: isLatestFirst ? 'rotate(0deg)' : 'rotate(180deg)',
          transition: 'transform 0.25s ease',
        }}
      >
        <path
          d="M7 2L7 12M7 2L3.5 5.5M7 2L10.5 5.5"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span>{isLatestFirst ? 'Latest first' : 'Oldest first'}</span>
    </button>
  );
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return 'No activity';
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return 'Just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d ago`;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(0)}K`;
  return String(tokens);
}

function worktreeLabel(root: string | null): string {
  if (!root) return 'No worktree';
  const parts = root.split('/');
  return parts[parts.length - 1] || root;
}

interface SessionCardProps {
  session: ConversationSession;
  isSelected: boolean;
  onSelect: (id: string) => void;
  rotationChain?: RotationChain;
}

function SessionCard({ session, isSelected, onSelect, rotationChain }: SessionCardProps) {
  const terminalColor = session.terminal
    ? TERMINAL_COLORS[session.terminal] ?? TERMINAL_COLORS.unknown
    : 'rgba(255, 255, 255, 0.3)';

  return (
    <button
      onClick={() => onSelect(session.session_id)}
      className="w-full text-left transition-all"
      style={{
        display: 'block',
        padding: '14px 18px',
        borderRadius: 12,
        border: `1.5px solid ${isSelected ? `${terminalColor}50` : 'rgba(255, 255, 255, 0.06)'}`,
        backgroundColor: isSelected ? `${terminalColor}0C` : 'rgba(255, 255, 255, 0.02)',
        cursor: 'pointer',
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      {/* Active indicator */}
      {isSelected && (
        <div
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            bottom: 0,
            width: 3,
            background: `linear-gradient(180deg, ${terminalColor}, ${terminalColor}80)`,
            borderRadius: '0 2px 2px 0',
          }}
        />
      )}

      {/* Header row */}
      <div className="flex items-start justify-between gap-3" style={{ marginBottom: 8 }}>
        <div className="flex items-center gap-2 min-w-0">
          {session.terminal && (
            <span
              className="text-xs font-semibold shrink-0"
              style={{
                padding: '2px 7px',
                borderRadius: 6,
                backgroundColor: `${terminalColor}18`,
                color: terminalColor,
                border: `1px solid ${terminalColor}30`,
              }}
            >
              {session.terminal}
            </span>
          )}
          <span
            className="text-sm font-medium truncate"
            style={{ color: 'var(--color-foreground)' }}
          >
            {session.title || 'Untitled session'}
          </span>
        </div>
        <span
          className="text-xs shrink-0"
          style={{ color: 'var(--color-muted)', whiteSpace: 'nowrap' }}
        >
          {formatTimestamp(session.last_message)}
        </span>
      </div>

      {/* Metadata row */}
      <div className="flex items-center gap-4 text-xs" style={{ color: 'var(--color-muted)' }}>
        <span>{session.message_count} msgs</span>
        <span>{formatTokens(session.total_tokens)} tokens</span>
        {session.worktree_root && (
          <span
            className="truncate"
            style={{
              maxWidth: 180,
              opacity: session.worktree_exists ? 1 : 0.5,
            }}
            title={session.worktree_root}
          >
            {worktreeLabel(session.worktree_root)}
            {!session.worktree_exists && ' (stale)'}
          </span>
        )}
      </div>

      {/* Rotation chain indicator */}
      {rotationChain && rotationChain.chain_depth > 1 && (
        <div
          className="flex items-center gap-1.5 text-xs mt-2"
          style={{ color: 'var(--color-warning)' }}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path
              d="M6 1V5L8.5 6.5M11 6A5 5 0 1 1 1 6a5 5 0 0 1 10 0Z"
              stroke="currentColor"
              strokeWidth="1.2"
              strokeLinecap="round"
            />
          </svg>
          <span>
            Rotation chain ({rotationChain.chain_depth} sessions)
          </span>
        </div>
      )}
    </button>
  );
}

interface WorktreeGroupHeaderProps {
  root: string;
  exists: boolean;
  count: number;
}

function WorktreeGroupHeader({ root, exists, count }: WorktreeGroupHeaderProps) {
  return (
    <div
      className="flex items-center gap-2 text-xs font-medium"
      style={{
        padding: '8px 4px',
        color: exists ? 'var(--color-muted)' : 'var(--color-error)',
        borderBottom: '1px solid rgba(255, 255, 255, 0.06)',
        marginBottom: 8,
      }}
    >
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path
          d="M2 3.5C2 2.67 2.67 2 3.5 2H5.59c.4 0 .78.16 1.06.44l.7.7c.28.28.66.44 1.06.44H10.5c.83 0 1.5.67 1.5 1.5V10.5c0 .83-.67 1.5-1.5 1.5H3.5C2.67 12 2 11.33 2 10.5V3.5Z"
          stroke="currentColor"
          strokeWidth="1.2"
        />
      </svg>
      <span className="truncate" title={root || 'Unlinked sessions'}>
        {root ? worktreeLabel(root) : 'Unlinked sessions'}
      </span>
      {!exists && root && (
        <span style={{ color: 'var(--color-error)', opacity: 0.8 }}>(removed)</span>
      )}
      <span style={{ opacity: 0.6 }}>({count})</span>
    </div>
  );
}

interface ConversationTimelineProps {
  sessions: ConversationSession[];
  sortOrder: SortOrder;
  onSortToggle: () => void;
  selectedSessionId: string | null;
  onSelectSession: (id: string) => void;
  rotationChains?: RotationChain[];
  worktreeGroups?: { worktree_root: string; worktree_exists: boolean; session_ids: string[] }[];
  terminalFilter: Set<string>;
  onTerminalFilterChange: (terminals: Set<string>) => void;
}

const ALL_CONV_TERMINALS = ['T0', 'T1', 'T2', 'T3'];

export default function ConversationTimeline({
  sessions,
  sortOrder,
  onSortToggle,
  selectedSessionId,
  onSelectSession,
  rotationChains,
  worktreeGroups,
  terminalFilter,
  onTerminalFilterChange,
}: ConversationTimelineProps) {
  const chainBySession = new Map<string, RotationChain>();
  if (rotationChains) {
    for (const chain of rotationChains) {
      for (const sid of chain.session_ids) {
        chainBySession.set(sid, chain);
      }
    }
  }

  // Filter sessions by terminal
  const filtered = sessions.filter(
    (s) => !s.terminal || terminalFilter.has(s.terminal)
  );

  // Group sessions by worktree if groups are provided
  const grouped = worktreeGroups && worktreeGroups.length > 0;
  const sessionMap = new Map(filtered.map((s) => [s.session_id, s]));

  function toggleTerminal(t: string) {
    const next = new Set(terminalFilter);
    if (next.has(t)) {
      if (next.size > 1) next.delete(t);
    } else {
      next.add(t);
    }
    onTerminalFilterChange(next);
  }

  return (
    <div>
      {/* Controls bar */}
      <div className="flex items-center justify-between gap-3 mb-4 animate-in-fast">
        {/* Terminal chips */}
        <div className="flex items-center gap-2">
          <span
            className="text-xs font-medium mr-1"
            style={{ color: 'var(--color-muted)', letterSpacing: '0.03em', textTransform: 'uppercase' }}
          >
            Terminals
          </span>
          {ALL_CONV_TERMINALS.map((t) => {
            const active = terminalFilter.has(t);
            const color = TERMINAL_COLORS[t] ?? TERMINAL_COLORS.unknown;
            return (
              <button
                key={t}
                onClick={() => toggleTerminal(t)}
                className="flex items-center gap-1.5 text-xs font-medium transition-all"
                style={{
                  padding: '4px 12px',
                  borderRadius: 20,
                  backgroundColor: active ? `${color}18` : 'rgba(255, 255, 255, 0.03)',
                  border: `1.5px solid ${active ? `${color}60` : 'rgba(255, 255, 255, 0.06)'}`,
                  color: active ? color : 'var(--color-muted)',
                  opacity: active ? 1 : 0.55,
                  cursor: 'pointer',
                }}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    backgroundColor: active ? color : 'rgba(255, 255, 255, 0.15)',
                    transition: 'all 0.2s ease',
                  }}
                />
                {t}
              </button>
            );
          })}
        </div>

        <SortToggle sortOrder={sortOrder} onToggle={onSortToggle} />
      </div>

      {/* Timeline list */}
      {filtered.length === 0 ? (
        <div
          className="glass-card"
          style={{
            padding: 40,
            textAlign: 'center',
            color: 'var(--color-muted)',
            fontSize: 14,
          }}
        >
          No conversations found for the selected filters.
        </div>
      ) : grouped ? (
        <div className="flex flex-col gap-6 stagger-children">
          {worktreeGroups!.map((group) => {
            const groupSessions = group.session_ids
              .map((id) => sessionMap.get(id))
              .filter((s): s is ConversationSession => s !== undefined);
            if (groupSessions.length === 0) return null;
            return (
              <div key={group.worktree_root || '__unlinked'}>
                <WorktreeGroupHeader
                  root={group.worktree_root}
                  exists={group.worktree_exists}
                  count={groupSessions.length}
                />
                <div className="flex flex-col gap-2">
                  {groupSessions.map((session) => (
                    <SessionCard
                      key={session.session_id}
                      session={session}
                      isSelected={selectedSessionId === session.session_id}
                      onSelect={onSelectSession}
                      rotationChain={chainBySession.get(session.session_id)}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="flex flex-col gap-2 stagger-children">
          {filtered.map((session) => (
            <SessionCard
              key={session.session_id}
              session={session}
              isSelected={selectedSessionId === session.session_id}
              onSelect={onSelectSession}
              rotationChain={chainBySession.get(session.session_id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
