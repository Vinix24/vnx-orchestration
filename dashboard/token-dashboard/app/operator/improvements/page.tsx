'use client';

import { useState } from 'react';
import { RefreshCw, Lightbulb, TrendingUp, BookOpen, CheckCircle2, XCircle, Play } from 'lucide-react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';
import { useProposals, useConfidenceTrends, useWeeklyDigest } from '@/lib/hooks';
import { acceptProposal, rejectProposal, applyProposals, generateWeeklyDigest } from '@/lib/operator-api';
import type { Proposal, WeeklyDigest } from '@/lib/types';

// ---- Helpers ----

const CATEGORY_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  pattern: { bg: 'rgba(80, 250, 123, 0.08)', border: 'rgba(80, 250, 123, 0.22)', text: '#50fa7b' },
  antipattern: { bg: 'rgba(255, 107, 107, 0.08)', border: 'rgba(255, 107, 107, 0.22)', text: '#ff6b6b' },
  prompt: { bg: 'rgba(107, 138, 230, 0.08)', border: 'rgba(107, 138, 230, 0.22)', text: '#6B8AE6' },
  workflow: { bg: 'rgba(249, 115, 22, 0.08)', border: 'rgba(249, 115, 22, 0.22)', text: '#f97316' },
  default: { bg: 'rgba(255,255,255,0.04)', border: 'rgba(255,255,255,0.1)', text: 'var(--color-muted)' },
};

function categoryStyle(cat: string) {
  return CATEGORY_COLORS[cat.toLowerCase()] ?? CATEGORY_COLORS.default;
}

function SectionHeader({ icon: Icon, title, count }: { icon: React.ElementType; title: string; count?: number }) {
  return (
    <div className="flex items-center gap-2" style={{ marginBottom: 16 }}>
      <Icon size={16} style={{ color: 'var(--color-accent)' }} />
      <h3 style={{ fontSize: 14, fontWeight: 700, color: 'var(--color-foreground)', letterSpacing: '-0.01em' }}>
        {title}
      </h3>
      {count !== undefined && (
        <span style={{ fontSize: 11, color: 'var(--color-muted)', background: 'rgba(255,255,255,0.05)', borderRadius: 20, padding: '1px 8px' }}>
          {count}
        </span>
      )}
    </div>
  );
}

function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-12">
      <div className="animate-spin w-6 h-6 border-2 rounded-full" style={{ borderColor: 'var(--color-card-border)', borderTopColor: 'var(--color-accent)' }} />
    </div>
  );
}

function EmptyState({ label }: { label: string }) {
  return (
    <div style={{ padding: '32px 20px', textAlign: 'center', color: 'var(--color-muted)', fontSize: 13, background: 'rgba(255,255,255,0.02)', borderRadius: 12, border: '1px dashed rgba(255,255,255,0.08)' }}>
      {label}
    </div>
  );
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.min(1, Math.max(0, value)) * 100;
  const color = pct >= 80 ? '#50fa7b' : pct >= 50 ? '#facc15' : '#ff6b6b';
  return (
    <div style={{ height: 4, background: 'rgba(255,255,255,0.1)', borderRadius: 2, overflow: 'hidden', marginTop: 6 }}>
      <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 2, transition: 'width 0.4s ease' }} />
    </div>
  );
}

// ---- Proposal Card ----

function ProposalCard({
  proposal,
  onAccept,
  onReject,
}: {
  proposal: Proposal;
  onAccept: (id: number) => void;
  onReject: (id: number, reason: string) => void;
}) {
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const [busy, setBusy] = useState(false);
  const style = categoryStyle(proposal.category);

  async function handleAccept() {
    setBusy(true);
    await onAccept(proposal.id);
    setBusy(false);
  }

  async function handleReject() {
    if (!rejectOpen) {
      setRejectOpen(true);
      return;
    }
    setBusy(true);
    await onReject(proposal.id, rejectReason);
    setBusy(false);
    setRejectOpen(false);
    setRejectReason('');
  }

  const isPending = proposal.status === 'pending';

  return (
    <div style={{ padding: '14px 16px', borderRadius: 10, background: style.bg, border: `1px solid ${style.border}`, display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div className="flex items-start justify-between gap-2">
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-foreground)', lineHeight: 1.45, flex: 1 }}>
          {proposal.proposed_change}
        </span>
        <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 5, background: style.bg, border: `1px solid ${style.border}`, color: style.text, flexShrink: 0 }}>
          {proposal.category}
        </span>
      </div>

      {proposal.evidence && (
        <p style={{ fontSize: 12, color: 'var(--color-muted)', lineHeight: 1.5 }}>
          {proposal.evidence}
        </p>
      )}

      <div>
        <div className="flex items-center justify-between" style={{ marginBottom: 2 }}>
          <span style={{ fontSize: 11, color: 'var(--color-muted)' }}>
            conf <strong style={{ color: style.text }}>{(proposal.confidence * 100).toFixed(0)}%</strong>
          </span>
          {!isPending && (
            <span style={{ fontSize: 10, fontWeight: 600, color: proposal.status === 'accepted' ? '#50fa7b' : '#ff6b6b' }}>
              {proposal.status.toUpperCase()}
            </span>
          )}
        </div>
        <ConfidenceBar value={proposal.confidence} />
      </div>

      {isPending && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div className="flex gap-2">
            <button
              onClick={handleAccept}
              disabled={busy}
              className="flex items-center gap-1"
              style={{ padding: '5px 12px', borderRadius: 6, background: 'rgba(80,250,123,0.15)', border: '1px solid rgba(80,250,123,0.35)', color: '#50fa7b', fontSize: 12, fontWeight: 600, cursor: busy ? 'not-allowed' : 'pointer', opacity: busy ? 0.6 : 1 }}
            >
              <CheckCircle2 size={12} />
              Accept
            </button>
            <button
              onClick={handleReject}
              disabled={busy}
              className="flex items-center gap-1"
              style={{ padding: '5px 12px', borderRadius: 6, background: 'rgba(255,107,107,0.12)', border: '1px solid rgba(255,107,107,0.3)', color: '#ff6b6b', fontSize: 12, fontWeight: 600, cursor: busy ? 'not-allowed' : 'pointer', opacity: busy ? 0.6 : 1 }}
            >
              <XCircle size={12} />
              {rejectOpen ? 'Confirm Reject' : 'Reject'}
            </button>
          </div>

          {rejectOpen && (
            <div className="flex gap-2">
              <input
                type="text"
                placeholder="Reason (optional)"
                value={rejectReason}
                onChange={e => setRejectReason(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleReject()}
                style={{ flex: 1, padding: '5px 10px', borderRadius: 6, background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', color: 'var(--color-foreground)', fontSize: 12, outline: 'none' }}
              />
              <button
                onClick={() => { setRejectOpen(false); setRejectReason(''); }}
                style={{ padding: '5px 10px', borderRadius: 6, background: 'transparent', border: '1px solid rgba(255,255,255,0.1)', color: 'var(--color-muted)', fontSize: 12, cursor: 'pointer' }}
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Proposals Section ----

function ProposalsSection() {
  const { data, error, isLoading, mutate } = useProposals();
  const [applying, setApplying] = useState(false);
  const [applyMsg, setApplyMsg] = useState<string | null>(null);

  const proposals = data?.proposals ?? [];
  const pending = proposals.filter(p => p.status === 'pending');
  const accepted = proposals.filter(p => p.status === 'accepted');

  async function handleAccept(id: number) {
    await acceptProposal(id);
    mutate();
  }

  async function handleReject(id: number, reason: string) {
    await rejectProposal(id, reason);
    mutate();
  }

  async function handleApplyAll() {
    setApplying(true);
    setApplyMsg(null);
    const result = await applyProposals();
    if (result.error) {
      setApplyMsg(`Error: ${result.error}`);
    } else {
      setApplyMsg(`Applied ${result.applied ?? 0} proposal(s)`);
    }
    setApplying(false);
    mutate();
  }

  return (
    <div style={{ padding: '20px 24px', borderRadius: 14, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', marginBottom: 24 }}>
      <div className="flex items-center justify-between" style={{ marginBottom: 16 }}>
        <SectionHeader icon={Lightbulb} title="Pending Proposals" count={pending.length} />
        {accepted.length > 0 && (
          <button
            onClick={handleApplyAll}
            disabled={applying}
            className="flex items-center gap-1"
            style={{ padding: '6px 14px', borderRadius: 7, background: 'rgba(80,250,123,0.12)', border: '1px solid rgba(80,250,123,0.3)', color: '#50fa7b', fontSize: 12, fontWeight: 600, cursor: applying ? 'not-allowed' : 'pointer', opacity: applying ? 0.6 : 1 }}
          >
            <Play size={12} />
            Apply All Accepted ({accepted.length})
          </button>
        )}
      </div>

      {applyMsg && (
        <div style={{ marginBottom: 12, padding: '8px 12px', borderRadius: 8, background: applyMsg.startsWith('Error') ? 'rgba(255,107,107,0.1)' : 'rgba(80,250,123,0.1)', border: `1px solid ${applyMsg.startsWith('Error') ? 'rgba(255,107,107,0.25)' : 'rgba(80,250,123,0.25)'}`, fontSize: 12, color: applyMsg.startsWith('Error') ? '#ff6b6b' : '#50fa7b' }}>
          {applyMsg}
        </div>
      )}

      {isLoading && <LoadingSpinner />}
      {error && <EmptyState label="Failed to load proposals" />}
      {!isLoading && !error && proposals.length === 0 && (
        <EmptyState label="No proposals — the intelligence loop will generate suggestions as it observes patterns" />
      )}

      {proposals.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {proposals.map(p => (
            <ProposalCard key={p.id} proposal={p} onAccept={handleAccept} onReject={handleReject} />
          ))}
        </div>
      )}
    </div>
  );
}

// ---- Confidence Trends Section ----

function ConfidenceTrendsSection() {
  const { data, error, isLoading } = useConfidenceTrends();
  const trends = data?.trends ?? [];

  const chartData = trends.map(t => ({
    date: t.date.slice(5), // MM-DD
    success: t.avg_success_confidence != null ? parseFloat((t.avg_success_confidence * 100).toFixed(1)) : null,
    antipattern: t.avg_antipattern_severity != null ? parseFloat((t.avg_antipattern_severity * 100).toFixed(1)) : null,
  }));

  return (
    <div style={{ padding: '20px 24px', borderRadius: 14, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', marginBottom: 24 }}>
      <SectionHeader icon={TrendingUp} title="Confidence Trends" />

      {isLoading && <LoadingSpinner />}
      {error && <EmptyState label="Failed to load confidence trends" />}
      {!isLoading && !error && trends.length === 0 && (
        <EmptyState label="No trend data yet — patterns are tracked as the intelligence loop runs" />
      )}

      {trends.length > 0 && (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={chartData} margin={{ top: 4, right: 16, left: -20, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
            <XAxis
              dataKey="date"
              tick={{ fill: 'var(--color-muted)', fontSize: 11 }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              domain={[0, 100]}
              tick={{ fill: 'var(--color-muted)', fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              unit="%"
            />
            <Tooltip
              contentStyle={{ background: '#0c1638', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, fontSize: 12 }}
              labelStyle={{ color: 'var(--color-muted)' }}
              formatter={(value: number, name: string) => [`${value}%`, name === 'success' ? 'Success Confidence' : 'Antipattern Severity']}
            />
            <Legend
              formatter={(value: string) => value === 'success' ? 'Success Confidence' : 'Antipattern Severity'}
              wrapperStyle={{ fontSize: 12, color: 'var(--color-muted)' }}
            />
            <Line type="monotone" dataKey="success" stroke="#50fa7b" dot={false} strokeWidth={2} connectNulls />
            <Line type="monotone" dataKey="antipattern" stroke="#ff6b6b" dot={false} strokeWidth={2} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

// ---- Weekly Digest Section ----

function WeeklyDigestSection() {
  const { data, error, isLoading, mutate } = useWeeklyDigest();
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);

  async function handleGenerate() {
    setGenerating(true);
    setGenError(null);
    const result = await generateWeeklyDigest();
    if (result.error) {
      setGenError(result.error as string);
    } else {
      mutate();
    }
    setGenerating(false);
  }

  const rawData = data as (WeeklyDigest & { error?: string }) | undefined;
  const digest = rawData && !rawData.error ? rawData : null;
  const notFound = error || (rawData && !!rawData.error);

  return (
    <div style={{ padding: '20px 24px', borderRadius: 14, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)' }}>
      <div className="flex items-center justify-between" style={{ marginBottom: 16 }}>
        <SectionHeader icon={BookOpen} title="Weekly Digest" />
        <button
          onClick={handleGenerate}
          disabled={generating}
          className="flex items-center gap-1"
          style={{ padding: '6px 14px', borderRadius: 7, background: 'rgba(107,138,230,0.12)', border: '1px solid rgba(107,138,230,0.3)', color: '#6B8AE6', fontSize: 12, fontWeight: 600, cursor: generating ? 'not-allowed' : 'pointer', opacity: generating ? 0.6 : 1 }}
        >
          <RefreshCw size={12} className={generating ? 'animate-spin' : ''} />
          {generating ? 'Generating…' : 'Generate Now'}
        </button>
      </div>

      {genError && (
        <div style={{ marginBottom: 12, padding: '8px 12px', borderRadius: 8, background: 'rgba(255,107,107,0.1)', border: '1px solid rgba(255,107,107,0.25)', fontSize: 12, color: '#ff6b6b' }}>
          {genError}
        </div>
      )}

      {isLoading && <LoadingSpinner />}
      {!isLoading && notFound && !digest && (
        <EmptyState label="No weekly digest yet — click Generate Now to create one" />
      )}

      {digest && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div style={{ padding: '14px 16px', borderRadius: 10, background: 'rgba(107,138,230,0.06)', border: '1px solid rgba(107,138,230,0.15)' }}>
            <p style={{ fontSize: 13, color: 'var(--color-foreground)', lineHeight: 1.6 }}>
              {digest.narrative}
            </p>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10 }}>
            <MetricTile label="Patterns Learned" value={digest.metrics.patterns_learned} />
            <MetricTile label="Antipatterns Active" value={digest.metrics.antipatterns_active} />
            <MetricTile label="Dispatches" value={digest.metrics.dispatch_outcomes.total} />
            <MetricTile
              label="Success Rate"
              value={
                digest.metrics.dispatch_outcomes.total > 0
                  ? `${((digest.metrics.dispatch_outcomes.success / digest.metrics.dispatch_outcomes.total) * 100).toFixed(0)}%`
                  : '—'
              }
            />
            <MetricTile label="Pending Suggestions" value={digest.metrics.pending_suggestions} />
            <MetricTile label="Accepted Suggestions" value={digest.metrics.accepted_suggestions} />
          </div>

          <p style={{ fontSize: 11, color: 'var(--color-muted)' }}>
            Generated: {digest.generated_at
              ? new Date(digest.generated_at).toLocaleString()
              : '—'}
            {digest.period && ` · Period: ${digest.period.start} → ${digest.period.end}`}
          </p>
        </div>
      )}
    </div>
  );
}

function MetricTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div style={{ padding: '10px 14px', borderRadius: 8, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}>
      <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--color-foreground)', letterSpacing: '-0.02em' }}>
        {value}
      </div>
      <div style={{ fontSize: 11, color: 'var(--color-muted)', marginTop: 2 }}>
        {label}
      </div>
    </div>
  );
}

// ---- Page ----

export default function ImprovementsPage() {
  return (
    <div>
      <div style={{ marginBottom: 28 }}>
        <div className="flex items-center gap-3" style={{ marginBottom: 6 }}>
          <Lightbulb size={22} style={{ color: 'var(--color-accent)' }} />
          <h2 style={{ fontSize: 20, fontWeight: 700, color: 'var(--color-foreground)', letterSpacing: '-0.02em' }}>
            Self-Improvement
          </h2>
        </div>
        <p style={{ fontSize: 13, color: 'var(--color-muted)', marginLeft: 34 }}>
          Proposals from the autonomous intelligence loop · Accept, reject, or apply all accepted changes
        </p>
      </div>

      <ProposalsSection />
      <ConfidenceTrendsSection />
      <WeeklyDigestSection />
    </div>
  );
}
