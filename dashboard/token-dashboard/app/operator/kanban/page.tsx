'use client';

import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useKanban, useProjects } from '@/lib/hooks';
import DegradedBanner from '@/components/operator/degraded-banner';
import type { KanbanCard, KanbanStageName } from '@/lib/types';

// ---- Track color palette ----
const TRACK_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  A: {
    bg: 'rgba(80, 250, 123, 0.12)',
    border: 'rgba(80, 250, 123, 0.4)',
    text: '#50fa7b',
  },
  B: {
    bg: 'rgba(250, 204, 21, 0.12)',
    border: 'rgba(250, 204, 21, 0.4)',
    text: '#facc15',
  },
  C: {
    bg: 'rgba(155, 107, 230, 0.12)',
    border: 'rgba(155, 107, 230, 0.4)',
    text: '#9B6BE6',
  },
};

// ---- Column definitions ----
const COLUMNS: { key: KanbanStageName; label: string; accentColor: string }[] = [
  { key: 'staging',  label: 'Staging',  accentColor: 'rgba(107, 138, 230, 0.6)' },
  { key: 'pending',  label: 'Pending',  accentColor: 'rgba(249, 115, 22, 0.6)'  },
  { key: 'active',   label: 'Active',   accentColor: 'rgba(249, 115, 22, 1)'    },
  { key: 'review',   label: 'Review',   accentColor: 'rgba(155, 107, 230, 0.8)' },
  { key: 'done',     label: 'Done',     accentColor: 'rgba(80, 250, 123, 0.7)'  },
];

// ---- Skeleton card ----
function SkeletonCard() {
  return (
    <div
      aria-hidden="true"
      style={{
        borderRadius: 10,
        padding: '12px 14px',
        background: 'linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.07) 50%, rgba(255,255,255,0.04) 75%)',
        backgroundSize: '200% 100%',
        animation: 'shimmer 1.6s infinite',
        marginBottom: 8,
        height: 88,
        border: '1px solid rgba(255,255,255,0.06)',
      }}
    />
  );
}

// ---- Empty column placeholder ----
function EmptyColumn({ stage }: { stage: string }) {
  return (
    <div
      data-testid={`empty-${stage}`}
      style={{
        padding: '28px 16px',
        textAlign: 'center',
        color: 'rgba(244,244,249,0.3)',
        fontSize: 12,
        border: '1px dashed rgba(255,255,255,0.06)',
        borderRadius: 10,
      }}
    >
      No dispatches
    </div>
  );
}

// ---- Dispatch card ----
function DispatchCard({ card }: { card: KanbanCard }) {
  const track = (card.track ?? '').toUpperCase();
  const trackStyle = TRACK_COLORS[track] ?? {
    bg: 'rgba(255,255,255,0.04)',
    border: 'rgba(255,255,255,0.12)',
    text: 'var(--color-muted)',
  };

  return (
    <div
      data-testid="dispatch-card"
      style={{
        borderRadius: 10,
        padding: '12px 14px',
        background: 'linear-gradient(135deg, rgba(10,20,48,0.9) 0%, rgba(10,20,48,0.7) 100%)',
        border: '1px solid rgba(255,255,255,0.08)',
        marginBottom: 8,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        transition: 'border-color 0.2s ease',
      }}
    >
      {/* Top row: PR-id + track badge */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span
          data-testid="card-pr-id"
          style={{
            fontSize: 12,
            fontWeight: 700,
            color: 'var(--color-foreground)',
            letterSpacing: '-0.01em',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            maxWidth: 140,
          }}
          title={card.pr_id}
        >
          {card.pr_id || '—'}
        </span>
        {track && track !== '—' && (
          <span
            data-testid="card-track"
            style={{
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: '0.06em',
              padding: '2px 7px',
              borderRadius: 6,
              background: trackStyle.bg,
              border: `1px solid ${trackStyle.border}`,
              color: trackStyle.text,
              flexShrink: 0,
            }}
          >
            {track}
          </span>
        )}
      </div>

      {/* Middle: terminal + gate */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        {card.terminal && card.terminal !== '—' && (
          <span
            data-testid="card-terminal"
            style={{
              fontSize: 10,
              padding: '2px 7px',
              borderRadius: 5,
              background: 'rgba(107,138,230,0.12)',
              border: '1px solid rgba(107,138,230,0.25)',
              color: '#6B8AE6',
            }}
          >
            {card.terminal}
          </span>
        )}
        {card.gate && card.gate !== '—' && (
          <span
            data-testid="card-gate"
            style={{
              fontSize: 10,
              color: 'var(--color-muted)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: 130,
            }}
            title={card.gate}
          >
            {card.gate}
          </span>
        )}
      </div>

      {/* Bottom row: duration + receipt status */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span
          data-testid="card-duration"
          style={{ fontSize: 10, color: 'rgba(244,244,249,0.45)' }}
        >
          {card.duration_label || '—'}
        </span>
        {card.has_receipt && card.receipt_status && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 600,
              color: card.receipt_status === 'success' ? 'var(--color-success)' : 'var(--color-warning)',
            }}
          >
            {card.receipt_status}
          </span>
        )}
      </div>
    </div>
  );
}

// ---- Column ----
function KanbanColumn({
  colKey,
  label,
  accentColor,
  cards,
  isLoading,
}: {
  colKey: KanbanStageName;
  label: string;
  accentColor: string;
  cards: KanbanCard[];
  isLoading: boolean;
}) {
  return (
    <div
      data-testid={`column-${colKey}`}
      style={{
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
      }}
    >
      {/* Column header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 12,
          paddingBottom: 10,
          borderBottom: `2px solid ${accentColor}`,
        }}
      >
        <span
          style={{
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.07em',
            textTransform: 'uppercase',
            color: 'var(--color-muted)',
          }}
        >
          {label}
        </span>
        {!isLoading && (
          <span
            style={{
              fontSize: 11,
              fontWeight: 600,
              color: cards.length > 0 ? accentColor : 'rgba(244,244,249,0.25)',
              background: 'rgba(255,255,255,0.05)',
              padding: '1px 7px',
              borderRadius: 10,
            }}
          >
            {cards.length}
          </span>
        )}
      </div>

      {/* Cards or skeleton or empty */}
      <div style={{ flex: 1 }}>
        {isLoading ? (
          <>
            <SkeletonCard />
            <SkeletonCard />
          </>
        ) : cards.length === 0 ? (
          <EmptyColumn stage={colKey} />
        ) : (
          cards.map((card) => (
            <DispatchCard key={card.id} card={card} />
          ))
        )}
      </div>
    </div>
  );
}

// ---- Page ----
export default function KanbanPage() {
  const [projectFilter, setProjectFilter] = useState<string | undefined>(undefined);
  const { data, isLoading, error, mutate } = useKanban(projectFilter);
  const { data: projectsEnv } = useProjects();
  const projects = projectsEnv?.data ?? [];

  const stages = data?.stages ?? {};
  const degradedReasons = data?.degraded
    ? (data.degraded_reasons ?? ['Kanban view degraded'])
    : error
    ? ['Failed to load kanban data — check the dashboard server']
    : [];

  return (
    <div>
      {/* Page header */}
      <div className="flex items-center justify-between" style={{ marginBottom: 24 }}>
        <div className="flex items-center gap-3">
          <div
            style={{
              width: 4,
              height: 28,
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
              Kanban Board
            </h2>
            <p style={{ fontSize: 12, color: 'var(--color-muted)', marginTop: 2 }}>
              Dispatch pipeline — live view
            </p>
          </div>
        </div>

        <button
          onClick={() => mutate()}
          aria-label="Refresh kanban board"
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

      {/* Degraded / error banner */}
      <DegradedBanner reasons={degradedReasons} view="KanbanView" />

      {/* Project filter */}
      {projects.length > 0 && (
        <div
          data-testid="project-filter"
          className="flex items-center gap-2"
          style={{ marginBottom: 20, flexWrap: 'wrap' }}
        >
          <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>Project:</span>
          <button
            onClick={() => setProjectFilter(undefined)}
            style={{
              padding: '4px 12px',
              borderRadius: 20,
              fontSize: 11,
              fontWeight: projectFilter === undefined ? 600 : 400,
              background: projectFilter === undefined ? 'rgba(249, 115, 22, 0.15)' : 'rgba(255,255,255,0.04)',
              border: `1px solid ${projectFilter === undefined ? 'rgba(249, 115, 22, 0.4)' : 'rgba(255,255,255,0.08)'}`,
              color: projectFilter === undefined ? 'var(--color-accent)' : 'var(--color-muted)',
              cursor: 'pointer',
            }}
          >
            All projects
          </button>
          {projects.map(p => (
            <button
              key={p.name}
              data-testid={`project-filter-${p.name}`}
              onClick={() => setProjectFilter(projectFilter === p.name ? undefined : p.name)}
              style={{
                padding: '4px 12px',
                borderRadius: 20,
                fontSize: 11,
                fontWeight: projectFilter === p.name ? 600 : 400,
                background: projectFilter === p.name ? 'rgba(107, 138, 230, 0.15)' : 'rgba(255,255,255,0.04)',
                border: `1px solid ${projectFilter === p.name ? 'rgba(107, 138, 230, 0.4)' : 'rgba(255,255,255,0.08)'}`,
                color: projectFilter === p.name ? '#6B8AE6' : 'var(--color-muted)',
                cursor: 'pointer',
              }}
            >
              {p.name}
            </button>
          ))}
        </div>
      )}

      {/* 5-column grid */}
      <div
        data-testid="kanban-grid"
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(5, 1fr)',
          gap: 16,
          alignItems: 'start',
        }}
      >
        {COLUMNS.map(({ key, label, accentColor }) => (
          <KanbanColumn
            key={key}
            colKey={key}
            label={label}
            accentColor={accentColor}
            cards={stages[key] ?? []}
            isLoading={isLoading}
          />
        ))}
      </div>
    </div>
  );
}
