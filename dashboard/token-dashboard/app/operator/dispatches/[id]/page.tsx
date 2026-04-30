'use client';

import { use, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import {
  ArrowLeft,
  FileText,
  GitCommit,
  Activity,
  ClipboardList,
  RefreshCw,
  Copy,
  Check,
} from 'lucide-react';
import {
  useDispatchDetail,
  useDispatchEvents,
  useDispatchResult,
} from '@/lib/hooks';
import type { DispatchStage } from '@/lib/types';
import DispatchStageBadge from '@/components/operator/dispatch-stage-badge';
import EventTimeline from '@/components/operator/event-timeline';

type Tab = 'overview' | 'events' | 'instruction' | 'result';

const TABS: { id: Tab; label: string; icon: typeof FileText }[] = [
  { id: 'overview', label: 'Overview', icon: ClipboardList },
  { id: 'events', label: 'Event Replay', icon: Activity },
  { id: 'instruction', label: 'Instruction', icon: FileText },
  { id: 'result', label: 'Result', icon: GitCommit },
];

function Card({
  title,
  children,
  right,
}: {
  title: string;
  children: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <section
      style={{
        padding: 20,
        borderRadius: 12,
        background: 'rgba(255,255,255,0.02)',
        border: '1px solid rgba(255,255,255,0.05)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 12,
        }}
      >
        <h3
          style={{
            fontSize: 12,
            fontWeight: 700,
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
            color: 'var(--color-muted)',
          }}
        >
          {title}
        </h3>
        {right}
      </div>
      {children}
    </section>
  );
}

function MetaRow({ label, value }: { label: string; value: string | undefined | null }) {
  if (!value) return null;
  return (
    <div style={{ display: 'flex', gap: 10, padding: '6px 0', fontSize: 12 }}>
      <span
        style={{
          color: 'var(--color-muted)',
          minWidth: 90,
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: '0.04em',
          fontWeight: 600,
        }}
      >
        {label}
      </span>
      <span
        style={{
          color: 'var(--color-foreground)',
          fontFamily: 'var(--font-mono, monospace)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {value}
      </span>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  return (
    <button
      onClick={() => {
        if (typeof navigator !== 'undefined' && navigator.clipboard) {
          navigator.clipboard.writeText(text).then(() => {
            setCopied(true);
            if (timerRef.current) clearTimeout(timerRef.current);
            timerRef.current = setTimeout(() => {
              timerRef.current = null;
              setCopied(false);
            }, 1500);
          });
        }
      }}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        padding: '4px 8px',
        borderRadius: 6,
        fontSize: 10,
        background: 'rgba(255,255,255,0.05)',
        border: '1px solid rgba(255,255,255,0.08)',
        color: copied ? '#22c55e' : 'var(--color-muted)',
        cursor: 'pointer',
      }}
    >
      {copied ? <Check size={11} /> : <Copy size={11} />}
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

export default function DispatchDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  let dispatchId = id;
  let decodeError: string | null = null;
  try {
    dispatchId = decodeURIComponent(id);
  } catch {
    decodeError = `Invalid dispatch id in URL: ${id}`;
  }
  const [tab, setTab] = useState<Tab>('overview');

  const { data: detail, error: detailErr, isLoading: detailLoading, mutate: mutateDetail } =
    useDispatchDetail(dispatchId);
  const { data: events, error: eventsErr, isLoading: eventsLoading, mutate: mutateEvents } =
    useDispatchEvents(dispatchId);
  const { data: result, error: resultErr, isLoading: resultLoading, mutate: mutateResult } =
    useDispatchResult(dispatchId);

  const stage = (detail?.stage ?? 'staging') as DispatchStage;
  const meta = detail?.metadata ?? {};

  function refreshAll() {
    mutateDetail();
    mutateEvents();
    mutateResult();
  }

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <Link
          href="/operator/dispatches"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            fontSize: 11,
            color: 'var(--color-muted)',
            textDecoration: 'none',
            marginBottom: 10,
          }}
        >
          <ArrowLeft size={12} />
          Back to dispatches
        </Link>
        <div className="flex items-center justify-between" style={{ gap: 12, flexWrap: 'wrap' }}>
          <div className="flex items-center gap-3" style={{ minWidth: 0 }}>
            <div
              style={{
                height: 28,
                width: 4,
                borderRadius: 2,
                background: 'var(--color-accent)',
                flexShrink: 0,
              }}
            />
            <div style={{ minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <h2
                  style={{
                    fontSize: '1.25rem',
                    fontWeight: 700,
                    letterSpacing: '-0.02em',
                    color: 'var(--color-foreground)',
                    fontFamily: 'var(--font-mono, monospace)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {dispatchId}
                </h2>
                {detail && <DispatchStageBadge stage={stage} />}
              </div>
              {meta.gate && (
                <p style={{ fontSize: 12, color: 'var(--color-accent)', marginTop: 2 }}>
                  {meta.gate}
                </p>
              )}
            </div>
          </div>
          <button
            onClick={refreshAll}
            data-testid="dispatch-refresh"
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
      </div>

      {/* Error / 404 */}
      {decodeError || detailErr || detail?.error ? (
        <div
          data-testid="dispatch-detail-error"
          style={{
            padding: 16,
            borderRadius: 10,
            background: 'rgba(239, 68, 68, 0.08)',
            border: '1px solid rgba(239, 68, 68, 0.25)',
            color: '#fca5a5',
            fontSize: 13,
          }}
        >
          {decodeError ?? detail?.error ?? `Failed to load dispatch: ${String(detailErr)}`}
        </div>
      ) : (
        <>
          {/* Tabs */}
          <div
            role="tablist"
            aria-label="Dispatch sections"
            style={{
              display: 'flex',
              gap: 2,
              marginBottom: 20,
              borderBottom: '1px solid rgba(255,255,255,0.06)',
            }}
          >
            {TABS.map(({ id: tabId, label, icon: Icon }) => {
              const active = tab === tabId;
              return (
                <button
                  key={tabId}
                  role="tab"
                  aria-selected={active}
                  data-testid={`tab-${tabId}`}
                  onClick={() => setTab(tabId)}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '10px 16px',
                    fontSize: 12,
                    fontWeight: active ? 600 : 400,
                    background: 'transparent',
                    border: 'none',
                    borderBottom: `2px solid ${active ? 'var(--color-accent)' : 'transparent'}`,
                    color: active ? 'var(--color-accent)' : 'var(--color-muted)',
                    cursor: 'pointer',
                    marginBottom: -1,
                  }}
                >
                  <Icon size={13} />
                  {label}
                </button>
              );
            })}
          </div>

          {/* Panels */}
          {tab === 'overview' && (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
                gap: 16,
              }}
            >
              <Card title="Metadata">
                {detailLoading ? (
                  <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>Loading…</p>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <MetaRow label="Terminal" value={meta.terminal} />
                    <MetaRow label="Track" value={meta.track} />
                    <MetaRow label="Role" value={meta.role} />
                    <MetaRow label="Gate" value={meta.gate} />
                    <MetaRow label="PR" value={meta.pr ?? meta.pr_id} />
                    <MetaRow label="Priority" value={meta.priority} />
                    <MetaRow label="Model" value={meta.model} />
                    <MetaRow label="Cognition" value={meta.cognition} />
                    <MetaRow label="Skill" value={meta.skill} />
                    <MetaRow label="Stage" value={detail?.stage} />
                    <MetaRow label="File" value={detail?.file} />
                  </div>
                )}
              </Card>
              <Card title="Receipt">
                {resultLoading ? (
                  <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>Loading…</p>
                ) : result?.receipt ? (
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <MetaRow label="Status" value={String(result.receipt.status ?? '')} />
                    <MetaRow label="Terminal" value={String(result.receipt.terminal ?? '')} />
                    <MetaRow label="Track" value={String(result.receipt.track ?? '')} />
                    <MetaRow label="Gate" value={String(result.receipt.gate ?? '')} />
                    <MetaRow
                      label="PR"
                      value={result.receipt.pr !== undefined ? String(result.receipt.pr) : undefined}
                    />
                    <MetaRow
                      label="Commit"
                      value={
                        result.receipt.commit_after
                          ? String(result.receipt.commit_after).slice(0, 10)
                          : undefined
                      }
                    />
                    <MetaRow
                      label="Duration"
                      value={
                        result.receipt.duration_secs !== undefined
                          ? `${result.receipt.duration_secs}s`
                          : undefined
                      }
                    />
                    <MetaRow label="Timestamp" value={String(result.receipt.timestamp ?? '')} />
                  </div>
                ) : (
                  <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                    No receipt filed yet for this dispatch.
                  </p>
                )}
              </Card>
            </div>
          )}

          {tab === 'events' && (
            <Card
              title={`Event Replay${events?.events ? ` · ${events.events.filter(e => e.type === 'tool_use').length} events` : ''}`}
            >
              {eventsErr || events?.error ? (
                <p
                  data-testid="events-error"
                  style={{ fontSize: 12, color: '#fca5a5' }}
                >
                  {events?.error ?? `Failed to load events: ${String(eventsErr)}`}
                </p>
              ) : eventsLoading ? (
                <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>Loading event archive…</p>
              ) : events?.events ? (
                <EventTimeline events={events.events} />
              ) : null}
            </Card>
          )}

          {tab === 'instruction' && (
            <Card
              title="Dispatch Instruction"
              right={detail?.instruction ? <CopyButton text={detail.instruction} /> : null}
            >
              {detailLoading ? (
                <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>Loading…</p>
              ) : detail?.instruction ? (
                <pre
                  data-testid="dispatch-instruction"
                  style={{
                    maxHeight: 600,
                    overflow: 'auto',
                    padding: 14,
                    borderRadius: 8,
                    background: 'rgba(0,0,0,0.3)',
                    border: '1px solid rgba(255,255,255,0.06)',
                    fontSize: 11,
                    lineHeight: 1.6,
                    fontFamily: 'var(--font-mono, monospace)',
                    color: 'var(--color-foreground)',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >
                  {detail.instruction}
                </pre>
              ) : (
                <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>No instruction text.</p>
              )}
            </Card>
          )}

          {tab === 'result' && (
            <Card
              title={result?.report_file ? `Report · ${result.report_file}` : 'Result Report'}
              right={result?.report ? <CopyButton text={result.report} /> : null}
            >
              {resultErr || result?.error ? (
                <p
                  data-testid="result-error"
                  style={{ fontSize: 12, color: 'var(--color-muted)' }}
                >
                  {result?.error ?? `Failed to load result: ${String(resultErr)}`}
                </p>
              ) : resultLoading ? (
                <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>Loading report…</p>
              ) : result?.report ? (
                <pre
                  data-testid="dispatch-report"
                  style={{
                    maxHeight: 600,
                    overflow: 'auto',
                    padding: 14,
                    borderRadius: 8,
                    background: 'rgba(0,0,0,0.3)',
                    border: '1px solid rgba(255,255,255,0.06)',
                    fontSize: 11,
                    lineHeight: 1.6,
                    fontFamily: 'var(--font-mono, monospace)',
                    color: 'var(--color-foreground)',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >
                  {result.report}
                </pre>
              ) : (
                <p style={{ fontSize: 12, color: 'var(--color-muted)' }}>
                  No result report available yet.
                </p>
              )}
            </Card>
          )}
        </>
      )}
    </div>
  );
}
