'use client';

import { useEffect, useState } from 'react';
import { CheckCircle2, AlertTriangle, XCircle, X } from 'lucide-react';
import type { ActionOutcome } from '@/lib/types';

interface Props {
  outcome: ActionOutcome | null;
  onDismiss?: () => void;
  autoDismissMs?: number;
}

const STATUS_CFG = {
  success:        { color: 'var(--color-success)', bg: 'rgba(80, 250, 123, 0.10)', border: 'rgba(80, 250, 123, 0.3)',  Icon: CheckCircle2 },
  already_active: { color: 'var(--color-success)', bg: 'rgba(80, 250, 123, 0.08)', border: 'rgba(80, 250, 123, 0.2)',  Icon: CheckCircle2 },
  degraded:       { color: 'var(--color-warning)', bg: 'rgba(250, 204, 21, 0.10)', border: 'rgba(250, 204, 21, 0.3)',  Icon: AlertTriangle },
  failed:         { color: 'var(--color-error)',   bg: 'rgba(255, 107, 107, 0.12)', border: 'rgba(255, 107, 107, 0.4)', Icon: XCircle },
};

export default function ActionToast({ outcome, onDismiss, autoDismissMs = 5000 }: Props) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!outcome) { setVisible(false); return; }
    setVisible(true);
    if (outcome.status === 'success' || outcome.status === 'already_active') {
      const timer = setTimeout(() => { setVisible(false); onDismiss?.(); }, autoDismissMs);
      return () => clearTimeout(timer);
    }
  }, [outcome, autoDismissMs, onDismiss]);

  if (!outcome || !visible) return null;

  const cfg = STATUS_CFG[outcome.status] ?? STATUS_CFG.failed;
  const { Icon } = cfg;

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: 'fixed',
        bottom: 24,
        right: 24,
        zIndex: 100,
        maxWidth: 380,
        padding: '14px 18px',
        borderRadius: 12,
        background: cfg.bg,
        border: `1px solid ${cfg.border}`,
        backdropFilter: 'blur(16px)',
        display: 'flex',
        gap: 12,
        alignItems: 'flex-start',
        animation: 'fadeInUp 0.3s ease-out both',
        boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
      }}
    >
      <Icon size={16} style={{ color: cfg.color, flexShrink: 0, marginTop: 1 }} />
      <div style={{ flex: 1 }}>
        <p style={{ fontSize: 13, fontWeight: 600, color: cfg.color, marginBottom: 2 }}>
          {outcome.action.replace(/\//g, ' ')} — {outcome.status}
        </p>
        <p style={{ fontSize: 12, color: 'var(--color-foreground)', lineHeight: 1.5 }}>
          {outcome.message}
        </p>
      </div>
      <button
        onClick={() => { setVisible(false); onDismiss?.(); }}
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          color: 'var(--color-muted)',
          padding: 2,
          flexShrink: 0,
        }}
        aria-label="Dismiss"
      >
        <X size={14} />
      </button>
    </div>
  );
}
