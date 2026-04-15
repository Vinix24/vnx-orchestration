'use client';

import { useState } from 'react';
import { X, ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import { useTranscript } from '@/lib/hooks';
import type { ConversationSession, TranscriptMessage } from '@/lib/types';

const PAGE_SIZE = 50;

const TERMINAL_COLORS: Record<string, string> = {
  T0: '#6B8AE6',
  T1: '#50fa7b',
  T2: '#facc15',
  T3: '#9B6BE6',
};

// ── Content parsing ────────────────────────────────────────────────────────────

interface ContentBlock {
  type: 'text' | 'tool_use' | 'tool_result' | 'unknown';
  text?: string;
  name?: string;
  input?: unknown;
  content?: string;
  raw?: string;
}

function parseContent(raw: string): ContentBlock[] {
  if (!raw) return [];

  const trimmed = raw.trim();

  // Try JSON array of blocks
  if (trimmed.startsWith('[')) {
    try {
      const blocks = JSON.parse(trimmed);
      if (Array.isArray(blocks)) {
        return blocks.map((b) => {
          if (b.type === 'text') return { type: 'text', text: b.text ?? '' };
          if (b.type === 'tool_use') return { type: 'tool_use', name: b.name ?? '', input: b.input };
          if (b.type === 'tool_result') {
            const inner = Array.isArray(b.content)
              ? b.content.map((c: { text?: string }) => c.text ?? '').join('\n')
              : String(b.content ?? '');
            return { type: 'tool_result', content: inner };
          }
          return { type: 'unknown', raw: JSON.stringify(b) };
        });
      }
    } catch {
      // fall through
    }
  }

  // Try single JSON object
  if (trimmed.startsWith('{')) {
    try {
      const b = JSON.parse(trimmed);
      if (b.type === 'tool_use') return [{ type: 'tool_use', name: b.name ?? '', input: b.input }];
      if (b.type === 'text') return [{ type: 'text', text: b.text ?? '' }];
    } catch {
      // fall through
    }
  }

  return [{ type: 'text', text: raw }];
}

// ── Block renderers ────────────────────────────────────────────────────────────

function TextBlock({ text }: { text: string }) {
  // Split on ``` code fences
  const parts = text.split(/(```[\s\S]*?```)/g);
  return (
    <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.6 }}>
      {parts.map((part, i) => {
        if (part.startsWith('```')) {
          const inner = part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '');
          return (
            <pre
              key={i}
              style={{
                margin: '8px 0',
                padding: '10px 12px',
                borderRadius: 6,
                background: 'rgba(0,0,0,0.35)',
                border: '1px solid rgba(255,255,255,0.08)',
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                fontSize: 11,
                lineHeight: 1.5,
                color: 'rgba(244,244,249,0.85)',
                overflowX: 'auto',
                whiteSpace: 'pre',
              }}
            >
              {inner}
            </pre>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </div>
  );
}

function ToolUseBlock({ name, input }: { name: string; input: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      style={{
        margin: '4px 0',
        borderRadius: 6,
        border: '1px solid rgba(107,138,230,0.3)',
        background: 'rgba(107,138,230,0.06)',
        overflow: 'hidden',
      }}
    >
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '6px 10px',
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span style={{ fontSize: 10, fontWeight: 700, color: '#6B8AE6', letterSpacing: '0.04em' }}>
          TOOL
        </span>
        <span style={{ fontSize: 11, color: 'rgba(244,244,249,0.7)', fontFamily: 'ui-monospace, monospace' }}>
          {name}
        </span>
      </button>
      {open && (
        <pre
          style={{
            margin: 0,
            padding: '6px 10px 10px',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            fontSize: 10,
            lineHeight: 1.5,
            color: 'rgba(244,244,249,0.7)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            borderTop: '1px solid rgba(107,138,230,0.15)',
          }}
        >
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
    </div>
  );
}

function ToolResultBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);
  const preview = content.slice(0, 80).replace(/\n/g, ' ');
  return (
    <div
      style={{
        margin: '4px 0',
        borderRadius: 6,
        border: '1px solid rgba(80,250,123,0.2)',
        background: 'rgba(80,250,123,0.04)',
        overflow: 'hidden',
      }}
    >
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '6px 10px',
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span style={{ fontSize: 10, fontWeight: 700, color: '#50fa7b', letterSpacing: '0.04em' }}>
          RESULT
        </span>
        {!open && (
          <span style={{ fontSize: 10, color: 'rgba(244,244,249,0.45)', fontFamily: 'ui-monospace, monospace' }}>
            {preview}{content.length > 80 ? '…' : ''}
          </span>
        )}
      </button>
      {open && (
        <pre
          style={{
            margin: 0,
            padding: '6px 10px 10px',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            fontSize: 10,
            lineHeight: 1.5,
            color: 'rgba(244,244,249,0.65)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            borderTop: '1px solid rgba(80,250,123,0.1)',
          }}
        >
          {content}
        </pre>
      )}
    </div>
  );
}

function MessageBubble({ msg }: { msg: TranscriptMessage }) {
  const isUser = msg.role === 'user';
  const blocks = parseContent(msg.content);

  const ts = msg.timestamp
    ? (() => {
        try {
          return new Date(msg.timestamp).toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
          });
        } catch {
          return msg.timestamp;
        }
      })()
    : null;

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: isUser ? 'flex-end' : 'flex-start',
        marginBottom: 12,
      }}
    >
      <div
        style={{
          maxWidth: '85%',
          padding: '10px 14px',
          borderRadius: isUser ? '14px 14px 4px 14px' : '14px 14px 14px 4px',
          background: isUser ? 'rgba(107,138,230,0.15)' : 'rgba(255,255,255,0.04)',
          border: `1px solid ${isUser ? 'rgba(107,138,230,0.3)' : 'rgba(255,255,255,0.08)'}`,
          fontSize: 13,
          color: 'var(--color-foreground)',
        }}
      >
        {blocks.map((block, i) => {
          if (block.type === 'text') return <TextBlock key={i} text={block.text ?? ''} />;
          if (block.type === 'tool_use') return <ToolUseBlock key={i} name={block.name ?? ''} input={block.input} />;
          if (block.type === 'tool_result') return <ToolResultBlock key={i} content={block.content ?? ''} />;
          return (
            <pre key={i} style={{ fontSize: 10, color: 'var(--color-muted)', whiteSpace: 'pre-wrap' }}>
              {block.raw}
            </pre>
          );
        })}
      </div>
      <div
        style={{
          marginTop: 3,
          fontSize: 10,
          color: 'var(--color-muted)',
          display: 'flex',
          gap: 6,
        }}
      >
        <span style={{ textTransform: 'capitalize' }}>{msg.role}</span>
        {ts && <span>{ts}</span>}
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

interface TranscriptViewerProps {
  session: ConversationSession;
  onClose: () => void;
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)}m`;
  return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

export default function TranscriptViewer({ session, onClose }: TranscriptViewerProps) {
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const { data, isLoading, error } = useTranscript(session.session_id);

  const messages = data?.messages ?? [];
  const visible = messages.slice(0, visibleCount);
  const hasMore = visibleCount < messages.length;

  const termColor = session.terminal ? (TERMINAL_COLORS[session.terminal] ?? '#6B6B6B') : '#6B6B6B';

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        maxHeight: 'calc(100vh - 80px)',
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: '16px 20px 12px',
          borderBottom: '1px solid rgba(255,255,255,0.07)',
          flexShrink: 0,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 10 }}>
          {session.terminal && (
            <span
              style={{
                padding: '2px 8px',
                borderRadius: 6,
                fontSize: 10,
                fontWeight: 700,
                background: `${termColor}18`,
                border: `1px solid ${termColor}35`,
                color: termColor,
                letterSpacing: '0.04em',
                flexShrink: 0,
                marginTop: 2,
              }}
            >
              {session.terminal}
            </span>
          )}
          <h3
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: 'var(--color-foreground)',
              margin: 0,
              flex: 1,
              lineHeight: 1.4,
            }}
          >
            {session.title || 'Untitled session'}
          </h3>
          <button
            onClick={onClose}
            aria-label="Close transcript"
            style={{
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              color: 'var(--color-muted)',
              padding: 4,
              borderRadius: 4,
              flexShrink: 0,
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <X size={16} />
          </button>
        </div>

        {/* Metadata pills */}
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <MetaPill label="Messages" value={String(session.message_count)} />
          <MetaPill label="Tokens" value={formatTokens(session.total_tokens)} />
          {data && (
            <MetaPill label="Loaded" value={`${messages.length} msgs`} />
          )}
        </div>
      </div>

      {/* Body */}
      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '16px 20px',
        }}
      >
        {isLoading && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 8,
              padding: '40px 0',
              color: 'var(--color-muted)',
              fontSize: 13,
            }}
          >
            <Loader2 size={16} className="animate-spin" />
            Loading transcript…
          </div>
        )}

        {error && (
          <div
            style={{
              padding: '16px',
              borderRadius: 8,
              background: 'rgba(255,107,107,0.08)',
              border: '1px solid rgba(255,107,107,0.25)',
              color: 'var(--color-error)',
              fontSize: 13,
            }}
          >
            Failed to load transcript. Ensure the API server is running.
          </div>
        )}

        {data && messages.length === 0 && (
          <div
            style={{
              textAlign: 'center',
              padding: '40px 0',
              color: 'var(--color-muted)',
              fontSize: 13,
            }}
          >
            No messages found for this session.
          </div>
        )}

        {visible.map((msg, i) => (
          <MessageBubble key={i} msg={msg} />
        ))}

        {hasMore && (
          <div style={{ textAlign: 'center', paddingTop: 8, paddingBottom: 4 }}>
            <button
              onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
              style={{
                padding: '7px 18px',
                borderRadius: 8,
                fontSize: 12,
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.12)',
                color: 'var(--color-muted)',
                cursor: 'pointer',
              }}
            >
              Load more ({messages.length - visibleCount} remaining)
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function MetaPill({ label, value }: { label: string; value: string }) {
  return (
    <span
      style={{
        fontSize: 10,
        padding: '2px 8px',
        borderRadius: 10,
        background: 'rgba(255,255,255,0.05)',
        border: '1px solid rgba(255,255,255,0.1)',
        color: 'var(--color-muted)',
      }}
    >
      <span style={{ color: 'rgba(244,244,249,0.4)', marginRight: 4 }}>{label}</span>
      {value}
    </span>
  );
}
