'use client';

import { AlertTriangle, Info, Tag } from 'lucide-react';
import type { OpenItem, OpenItemSummary } from '@/lib/types';

interface Props {
  items: OpenItem[];
  summary: OpenItemSummary;
  emptyLabel?: string;
}

const SEVERITY_CONFIG = {
  blocker:  { label: 'BLOCKER', color: 'var(--color-error)',   bg: 'rgba(255, 107, 107, 0.12)', border: 'rgba(255, 107, 107, 0.3)',  Icon: AlertTriangle },
  blocking: { label: 'BLOCKER', color: 'var(--color-error)',   bg: 'rgba(255, 107, 107, 0.12)', border: 'rgba(255, 107, 107, 0.3)',  Icon: AlertTriangle },
  warn:     { label: 'WARN',    color: 'var(--color-warning)', bg: 'rgba(250, 204, 21, 0.10)',  border: 'rgba(250, 204, 21, 0.3)',   Icon: AlertTriangle },
  warning:  { label: 'WARN',    color: 'var(--color-warning)', bg: 'rgba(250, 204, 21, 0.10)',  border: 'rgba(250, 204, 21, 0.3)',   Icon: AlertTriangle },
  info:     { label: 'INFO',    color: 'var(--color-muted)',   bg: 'rgba(255,255,255,0.04)',    border: 'rgba(255,255,255,0.10)',    Icon: Info },
};

function fmtAge(secs: number | null | undefined): string {
  if (secs == null) return '';
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

export default function OpenItemsList({ items, summary, emptyLabel = 'No open items' }: Props) {
  if (items.length === 0) {
    return (
      <div
        style={{
          padding: '28px 20px',
          textAlign: 'center',
          color: 'var(--color-muted)',
          fontSize: 13,
          background: 'rgba(255,255,255,0.02)',
          borderRadius: 12,
          border: '1px dashed rgba(255,255,255,0.08)',
        }}
      >
        {emptyLabel}
      </div>
    );
  }

  return (
    <div>
      {/* Summary bar */}
      <div className="flex items-center gap-4" style={{ marginBottom: 14 }}>
        {summary.blocker_count > 0 && (
          <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--color-error)' }}>
            {summary.blocker_count} blocker{summary.blocker_count !== 1 ? 's' : ''}
          </span>
        )}
        {summary.warn_count > 0 && (
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-warning)' }}>
            {summary.warn_count} warn{summary.warn_count !== 1 ? 's' : ''}
          </span>
        )}
        {summary.info_count > 0 && (
          <span style={{ fontSize: 12, color: 'var(--color-muted)' }}>
            {summary.info_count} info
          </span>
        )}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {items.map((item, i) => {
          const sev = (item.severity || 'info') as keyof typeof SEVERITY_CONFIG;
          const cfg = SEVERITY_CONFIG[sev] ?? SEVERITY_CONFIG.info;
          const { Icon } = cfg;
          return (
            <div
              key={item.id ?? i}
              style={{
                padding: '12px 16px',
                borderRadius: 10,
                background: cfg.bg,
                border: `1px solid ${cfg.border}`,
                display: 'flex',
                gap: 12,
                alignItems: 'flex-start',
              }}
            >
              <Icon size={14} style={{ color: cfg.color, flexShrink: 0, marginTop: 2 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="flex items-center gap-2" style={{ marginBottom: 2, flexWrap: 'wrap' }}>
                  <span
                    style={{
                      fontSize: 10,
                      fontWeight: 700,
                      color: cfg.color,
                      letterSpacing: '0.04em',
                    }}
                  >
                    {cfg.label}
                  </span>
                  {item._project_name && (
                    <span
                      style={{
                        fontSize: 10,
                        color: 'var(--color-accent)',
                        background: 'rgba(249, 115, 22, 0.1)',
                        border: '1px solid rgba(249, 115, 22, 0.2)',
                        borderRadius: 4,
                        padding: '0 5px',
                      }}
                    >
                      {item._project_name}
                    </span>
                  )}
                  {item.age_seconds != null && (
                    <span style={{ fontSize: 10, color: 'var(--color-muted)' }}>
                      {fmtAge(item.age_seconds)} old
                    </span>
                  )}
                </div>
                <p style={{ fontSize: 13, color: 'var(--color-foreground)', lineHeight: 1.4 }}>
                  {item.title || item.id}
                </p>
                {item.description && (
                  <p
                    style={{
                      fontSize: 12,
                      color: 'var(--color-muted)',
                      marginTop: 4,
                      lineHeight: 1.5,
                    }}
                  >
                    {item.description}
                  </p>
                )}
                {item.source && (
                  <div className="flex items-center gap-1" style={{ marginTop: 5 }}>
                    <Tag size={10} style={{ color: 'var(--color-muted)' }} />
                    <span style={{ fontSize: 10, color: 'var(--color-muted)', fontFamily: 'monospace' }}>
                      {item.source}
                    </span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
