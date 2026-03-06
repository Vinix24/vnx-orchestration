'use client';

import { useState, useMemo, Suspense } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
import { usePolling } from '@/lib/use-polling';
import type { OpenItemsDigest, OpenItem } from '@/lib/types';
import { severityColor, severityLabel, severityBgColor, severityBorderColor } from '@/lib/utils';
import { Search } from 'lucide-react';

type SeverityFilter = 'all' | 'blocker' | 'warning' | 'info';
type SortMode = 'newest' | 'severity';

const SEVERITY_ORDER: Record<string, number> = { blocker: 0, warn: 1, warning: 1, info: 2 };

export default function OpenItemsPage() {
  return (
    <Suspense fallback={
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin w-8 h-8 border-2 rounded-full" style={{ borderColor: 'var(--color-card-border)', borderTopColor: 'var(--color-accent)' }} />
      </div>
    }>
      <OpenItemsContent />
    </Suspense>
  );
}

function OpenItemsContent() {
  const searchParams = useSearchParams();
  const router = useRouter();

  const [severity, setSeverity] = useState<SeverityFilter>(
    (searchParams.get('severity') as SeverityFilter) || 'all'
  );
  const [query, setQuery] = useState(searchParams.get('q') || '');
  const [sort, setSort] = useState<SortMode>('newest');

  const { data, loading } = usePolling<OpenItemsDigest>('/state/open_items_digest.json');

  function updateFilter(newSev: SeverityFilter, newQ?: string) {
    setSeverity(newSev);
    const params = new URLSearchParams();
    if (newSev !== 'all') params.set('severity', newSev);
    const q = newQ ?? query;
    if (q) params.set('q', q);
    const qs = params.toString();
    router.replace(qs ? `?${qs}` : '/open-items', { scroll: false });
  }

  const filteredItems = useMemo(() => {
    if (!data?.open_items) return [];
    let items = [...data.open_items];

    if (severity !== 'all') {
      items = items.filter((item) => {
        const sev = item.severity.toLowerCase();
        if (severity === 'warning') return sev === 'warn' || sev === 'warning';
        return sev === severity;
      });
    }

    if (query.trim()) {
      const q = query.trim().toLowerCase();
      items = items.filter((item) => item.title.toLowerCase().includes(q));
    }

    if (sort === 'severity') {
      items.sort((a, b) => (SEVERITY_ORDER[a.severity] ?? 3) - (SEVERITY_ORDER[b.severity] ?? 3));
    }

    return items;
  }, [data?.open_items, severity, query, sort]);

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

  const summary = data.summary;
  const summaryItems: { label: string; count: number; filter: SeverityFilter | null; color: string }[] = [
    { label: 'Open', count: summary.open_count, filter: 'all', color: 'var(--color-foreground)' },
    { label: 'Blockers', count: summary.blocker_count, filter: 'blocker', color: '#ff6b6b' },
    { label: 'Warnings', count: summary.warn_count, filter: 'warning', color: '#facc15' },
    { label: 'Info', count: summary.info_count, filter: 'info', color: '#60a5fa' },
    { label: 'Done', count: summary.done_count, filter: null, color: '#50fa7b' },
    { label: 'Deferred', count: summary.deferred_count, filter: null, color: 'var(--color-muted)' },
    { label: 'Wontfix', count: summary.wontfix_count, filter: null, color: 'var(--color-muted)' },
  ];

  return (
    <div>
      <div className="section-header">
        <div className="accent-bar" />
        <h2>Open Items</h2>
      </div>

      {/* Summary Bar */}
      <div className="flex flex-wrap gap-3 mb-5 stagger-children">
        {summaryItems.map((item) => (
          <button
            key={item.label}
            onClick={() => item.filter !== null && updateFilter(item.filter)}
            className="glass-card flex items-center gap-2 transition-all"
            style={{
              padding: '10px 18px',
              cursor: item.filter !== null ? 'pointer' : 'default',
              borderColor: item.filter === severity ? `${item.color}60` : undefined,
              opacity: item.filter === null ? 0.7 : 1,
            }}
          >
            <span className="kpi-value-sm" style={{ color: item.color }}>{item.count}</span>
            <span className="text-xs" style={{ color: 'var(--color-muted)' }}>{item.label}</span>
          </button>
        ))}
      </div>

      {/* Filter Toolbar */}
      <div className="flex flex-wrap items-center gap-3 mb-5 animate-in-fast">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.03em' }}>
            Severity
          </span>
          {(['all', 'blocker', 'warning', 'info'] as SeverityFilter[]).map((sev) => (
            <button
              key={sev}
              onClick={() => updateFilter(sev)}
              className="text-xs font-medium transition-all"
              style={{
                padding: '5px 14px',
                borderRadius: 20,
                border: `1.5px solid ${severity === sev ? 'rgba(249, 115, 22, 0.5)' : 'rgba(255, 255, 255, 0.06)'}`,
                background: severity === sev ? 'rgba(249, 115, 22, 0.1)' : 'rgba(255, 255, 255, 0.03)',
                color: severity === sev ? 'var(--color-accent)' : 'var(--color-muted)',
                cursor: 'pointer',
              }}
            >
              {sev === 'all' ? 'All' : sev.charAt(0).toUpperCase() + sev.slice(1)}
            </button>
          ))}
        </div>

        <div
          className="flex items-center gap-2"
          style={{
            padding: '5px 12px',
            borderRadius: 10,
            border: '1px solid rgba(255, 255, 255, 0.08)',
            background: 'rgba(10, 20, 48, 0.8)',
          }}
        >
          <Search size={14} style={{ color: 'var(--color-muted)' }} />
          <input
            type="text"
            placeholder="Search items..."
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              const params = new URLSearchParams();
              if (severity !== 'all') params.set('severity', severity);
              if (e.target.value) params.set('q', e.target.value);
              const qs = params.toString();
              router.replace(qs ? `?${qs}` : '/open-items', { scroll: false });
            }}
            className="text-sm outline-none"
            style={{
              background: 'transparent',
              color: 'var(--color-foreground)',
              border: 'none',
              width: 180,
            }}
          />
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs font-medium" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.03em' }}>
            Sort
          </span>
          {(['newest', 'severity'] as SortMode[]).map((s) => (
            <button
              key={s}
              onClick={() => setSort(s)}
              className="text-xs font-medium transition-all"
              style={{
                padding: '5px 14px',
                borderRadius: 20,
                border: `1.5px solid ${sort === s ? 'rgba(249, 115, 22, 0.5)' : 'rgba(255, 255, 255, 0.06)'}`,
                background: sort === s ? 'rgba(249, 115, 22, 0.1)' : 'rgba(255, 255, 255, 0.03)',
                color: sort === s ? 'var(--color-accent)' : 'var(--color-muted)',
                cursor: 'pointer',
              }}
            >
              {s === 'newest' ? 'Newest' : 'Severity'}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Items List */}
        <div className="lg:col-span-2">
          <div style={{ display: 'grid', gap: 8 }}>
            {filteredItems.length === 0 && (
              <div className="glass-card" style={{ padding: 40, textAlign: 'center', color: 'var(--color-muted)', fontSize: 14 }}>
                No items match the current filters.
              </div>
            )}
            {filteredItems.map((item) => (
              <OpenItemCard key={item.id} item={item} />
            ))}
          </div>
        </div>

        {/* Recent Closures */}
        <div>
          <div className="glass-card animate-in" style={{ padding: 20 }}>
            <h3 className="text-xs font-medium mb-4" style={{ color: 'var(--color-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
              Recent Closures
            </h3>
            <div style={{ display: 'grid', gap: 10 }}>
              {(data.recent_closures ?? []).slice(0, 10).map((item) => (
                <div
                  key={item.id}
                  style={{
                    padding: '10px 12px',
                    borderRadius: 10,
                    border: '1px solid rgba(255, 255, 255, 0.06)',
                    background: 'rgba(255, 255, 255, 0.03)',
                  }}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-mono font-semibold" style={{ color: 'var(--color-muted)' }}>{item.id}</span>
                  </div>
                  <div className="text-xs" style={{ color: 'var(--color-foreground)', lineHeight: 1.4 }}>{item.title}</div>
                  <div className="text-xs mt-1" style={{ color: 'var(--color-muted)', opacity: 0.7 }}>{item.closed_reason}</div>
                </div>
              ))}
              {(!data.recent_closures || data.recent_closures.length === 0) && (
                <div className="text-xs" style={{ color: 'var(--color-muted)' }}>No recent closures</div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function OpenItemCard({ item }: { item: OpenItem }) {
  return (
    <div
      style={{
        padding: '12px 16px',
        borderRadius: 12,
        border: '1px solid rgba(255, 255, 255, 0.08)',
        borderLeft: `3px solid ${severityBorderColor(item.severity)}`,
        background: severityBgColor(item.severity),
      }}
    >
      <div className="text-sm" style={{ color: 'var(--color-foreground)', lineHeight: 1.5 }}>
        {item.title}
      </div>
      <div className="flex flex-wrap items-center gap-2 mt-2">
        <span
          className="text-xs font-mono font-semibold"
          style={{
            padding: '2px 8px',
            borderRadius: 6,
            border: '1px solid rgba(255, 255, 255, 0.12)',
            background: 'rgba(0, 0, 0, 0.2)',
            color: 'var(--color-muted)',
          }}
        >
          {item.id}
        </span>
        <span
          className="text-xs font-bold"
          style={{
            padding: '2px 8px',
            borderRadius: 6,
            background: `${severityColor(item.severity)}20`,
            color: severityColor(item.severity),
            letterSpacing: '0.05em',
            textTransform: 'uppercase',
          }}
        >
          {severityLabel(item.severity)}
        </span>
        {item.pr_id && (
          <span
            className="text-xs"
            style={{
              padding: '2px 8px',
              borderRadius: 20,
              border: '1px solid rgba(255, 255, 255, 0.12)',
              background: 'rgba(0, 0, 0, 0.2)',
              color: 'var(--color-muted)',
            }}
          >
            PR {item.pr_id}
          </span>
        )}
      </div>
    </div>
  );
}
