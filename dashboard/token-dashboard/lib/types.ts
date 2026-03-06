export interface TokenStats {
  period: string;
  terminal: string;
  model: string;
  sessions: number;
  api_calls: number;
  context_per_call_K: number;
  cache_hit_pct: number;
  new_per_call_K: number;
  output_per_call_K: number;
  total_output_tokens: number;
  total_input_tokens: number;
  total_cache_creation_tokens: number;
  total_cache_read_tokens: number;
  context_rotations: number;
  activities: string;
}

export interface SessionDetail {
  session_id: string;
  terminal: string;
  model: string;
  date: string;
  api_calls: number;
  context_per_call_K: number;
  cache_hit_pct: number;
  output_per_call_K: number;
  duration_minutes: number;
  primary_activity: string;
  tool_calls_total: number;
  has_error_recovery: boolean;
}

export type GroupBy = 'day' | 'week' | 'month';

export const TERMINAL_COLORS: Record<string, string> = {
  'T-MANAGER': '#f97316',
  'T0': '#6B8AE6',
  'T1': '#50fa7b',
  'T2': '#facc15',
  'T3': '#9B6BE6',
  'unknown': '#6B6B6B',
};

export const MODEL_COLORS: Record<string, string> = {
  'claude-opus': '#f97316',
  'claude-sonnet': '#6B8AE6',
  'unknown': '#6B6B6B',
};

// ===== System State Types =====

export interface ProcessInfo {
  pid: string;
  running: boolean;
}

export interface TerminalInfo {
  status: 'working' | 'idle' | 'blocked' | 'unknown' | 'offline';
  model: string;
  provider: string;
  is_active: boolean;
  current_command: string;
  directory: string;
  last_update: string;
  current_task?: string;
}

export interface OpenItem {
  id: string;
  severity: 'blocker' | 'warn' | 'info';
  title: string;
  pr_id: string | null;
}

export interface OpenItemsSummaryBlock {
  summary: {
    open_count: number;
    blocker_count: number;
    warn_count: number;
    info_count: number;
    done_count: number;
    deferred_count: number;
    wontfix_count: number;
  };
  open_count: number;
  blocker_count: number;
  top_blockers: OpenItem[];
  top_warnings: OpenItem[];
  open_items: OpenItem[];
  last_updated: string;
}

export interface GateInfo {
  phase: string;
  status: string;
}

export interface LockInfo {
  locked: boolean;
  cursor: string;
  pending: number;
}

export interface PRQueueBlock {
  active_feature: string;
  total_prs: number;
  completed_prs: number;
  progress_percent: number;
  current_pr?: string;
  prs: {
    id: string;
    description: string;
    status: 'done' | 'in_progress' | 'pending';
    blocked: boolean;
    deps: string[];
  }[];
}

export interface DashboardStatus {
  timestamp: string;
  processes: Record<string, ProcessInfo>;
  intelligence_daemon?: Record<string, unknown>;
  open_items: OpenItemsSummaryBlock;
  pr_queue: PRQueueBlock;
  recommendations: string[];
  queues: { queue: number; pending: number; active: number };
  gates: Record<string, GateInfo>;
  metrics: Record<string, unknown>;
  locks: Record<string, LockInfo>;
  terminals: Record<string, TerminalInfo>;
  recentActivity: unknown[];
  quality_intelligence?: Record<string, unknown>;
}

export interface TerminalStateEntry {
  claimed_at: string | null;
  claimed_by: string | null;
  last_activity: string;
  lease_expires_at: string | null;
  status: string;
  terminal_id: string;
  version: number;
}

export interface TerminalStateFile {
  schema_version: number;
  terminals: Record<string, TerminalStateEntry>;
}

export interface OpenItemsDigest {
  summary: {
    open_count: number;
    blocker_count: number;
    warn_count: number;
    info_count: number;
    done_count: number;
    deferred_count: number;
    wontfix_count: number;
  };
  top_blockers: OpenItem[];
  top_warnings: OpenItem[];
  open_items: OpenItem[];
  recent_closures: {
    id: string;
    title: string;
    closed_reason: string;
  }[];
  last_updated: string;
  digest_generated: string;
}

export interface PRQueue {
  active_feature: { name: string | null; plan_file: string | null };
  completed_prs: string[];
  in_progress: string[];
  blocked: string[];
  next_available: string[];
  execution_order: string[];
  updated_at: string;
}
