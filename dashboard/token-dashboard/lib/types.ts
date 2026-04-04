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

export type SortOrder = 'DESC' | 'ASC';

// ===== Operator Dashboard Types =====

export type TerminalStatus =
  | 'active'
  | 'working'
  | 'blocked'
  | 'stale'
  | 'exited'
  | 'idle'
  | 'unknown';

export type HeartbeatClassification = 'fresh' | 'stale' | 'dead' | 'missing' | string;

export type AttentionLevel = 'critical' | 'warning' | 'clear';

export type ActionStatus = 'success' | 'failed' | 'already_active' | 'degraded';

export interface ContextPressure {
  remaining_pct: number;
  warning: boolean;
}

export interface TerminalEntry {
  terminal_id: string;
  lease_state: string;
  dispatch_id: string | null;
  heartbeat_classification: HeartbeatClassification;
  last_heartbeat_at: string | null;
  worker_state: string | null;
  last_output_at: string | null;
  stall_count?: number;
  blocked_reason?: string | null;
  is_terminal?: boolean;
  status: TerminalStatus;
  context_pressure?: ContextPressure;
}

export interface ProjectEntry {
  name: string;
  path: string;
  registered_at: string | null;
  session_active: boolean;
  active_feature: string | null;
  open_blocker_count: number;
  open_warn_count: number;
  attention_level: AttentionLevel;
}

export interface OpenItem {
  id: string;
  severity: 'blocker' | 'blocking' | 'warn' | 'warning' | 'info';
  status: string;
  title: string;
  description?: string;
  source?: string;
  created_at?: string;
  age_seconds?: number | null;
  _project_name?: string;
}

export interface OpenItemSummary {
  blocker_count: number;
  warn_count: number;
  info_count: number;
}

export interface PRProgress {
  id: string;
  title: string | null;
  status: string | null;
  track: string | null;
  gate: string | null;
}

export interface TrackStatus {
  current_gate: string | null;
  status: string | null;
  active_dispatch_id: string | null;
}

export interface SessionData {
  feature_name: string | null;
  pr_progress: PRProgress[];
  track_status: Record<string, TrackStatus>;
  terminal_states: TerminalEntry[];
  last_activity: string | null;
  open_item_summary: OpenItemSummary;
}

export interface FreshnessEnvelope<T = unknown> {
  view: string;
  queried_at?: string;
  source_freshness?: Record<string, string | null>;
  staleness_seconds?: number;
  degraded: boolean;
  degraded_reasons?: string[];
  data: T;
}

export interface ProjectsEnvelope extends FreshnessEnvelope<ProjectEntry[]> {}
export interface SessionEnvelope extends FreshnessEnvelope<SessionData> {}
export interface TerminalsEnvelope extends FreshnessEnvelope<TerminalEntry[]> {}
export interface TerminalEnvelope extends FreshnessEnvelope<TerminalEntry> {}
export interface OpenItemsEnvelope extends FreshnessEnvelope<{ items: OpenItem[]; summary: OpenItemSummary }> {}
export interface AggregateOpenItemsEnvelope extends FreshnessEnvelope<{
  items: OpenItem[];
  per_project_subtotals: Record<string, { status: string; blocker_count: number; warn_count: number; info_count: number }>;
  total_summary: OpenItemSummary;
}> {}

export interface ActionOutcome {
  action: string;
  project: string;
  status: ActionStatus;
  message: string;
  details?: Record<string, unknown>;
  error_code?: string;
  timestamp: string;
}

export interface ConversationSession {
  session_id: string;
  project_path: string;
  cwd: string;
  last_message: string | null;
  title: string;
  message_count: number;
  user_message_count: number;
  total_tokens: number;
  terminal: string | null;
  worktree_root: string | null;
  worktree_exists: boolean;
}

export interface RotationChain {
  dispatch_id: string;
  chain_depth: number;
  latest_message: string | null;
  session_ids: string[];
}

export interface WorktreeGroupInfo {
  worktree_root: string;
  worktree_exists: boolean;
  session_ids: string[];
}

export interface ConversationsResponse {
  sessions: ConversationSession[];
  sort_order: SortOrder;
  total: number;
  worktree_groups?: WorktreeGroupInfo[];
  rotation_chains?: RotationChain[];
}

// ===== Gate Config Types =====

export interface GateEntry {
  enabled: boolean;
}

export interface GateConfigResponse {
  project: string | null;
  gates: Record<string, Record<string, GateEntry> | GateEntry>;
  queried_at: string;
  config_path: string;
  error?: string;
}

export interface GateToggleRequest {
  project: string;
  gate: string;
  enabled: boolean;
}

export interface GateToggleResponse {
  action: string;
  project: string;
  gate: string;
  enabled: boolean;
  status: 'success' | 'failed';
  message: string;
  timestamp: string;
}

// ===== Kanban Board Types =====

export interface KanbanCard {
  id: string;
  pr_id: string;
  track: string;
  terminal: string;
  role: string;
  gate: string;
  priority: string;
  status: string;
  stage: string;
  duration_secs: number;
  duration_label: string;
  has_receipt: boolean;
  receipt_status: string | null;
}

export type KanbanStageName = 'staging' | 'pending' | 'active' | 'review' | 'done';

export interface KanbanEnvelope {
  stages: Partial<Record<KanbanStageName, KanbanCard[]>>;
  total: number;
  degraded?: boolean;
  degraded_reasons?: string[];
}

export interface GovernanceDigestSourceFreshness {
  governance_digest: string | null;
}

export interface DigestRecurrenceRecord {
  defect_family: string;
  count: number;
  representative_content: string;
  severity: string;
  signal_types: string[];
  impacted_features: string[];
  impacted_prs: string[];
  impacted_sessions: string[];
  evidence_pointers: string[];
  providers: string[];
}

export interface DigestRecommendation {
  category: string;
  content: string;
  advisory_only: boolean;
  evidence_basis: string[];
  severity: string;
  recurrence_count: number;
  defect_family: string;
}

export interface GovernanceDigestData {
  runner_version?: string;
  generated_at?: string;
  total_signals_processed?: number;
  recurring_pattern_count?: number;
  single_occurrence_count?: number;
  recurring_patterns?: DigestRecurrenceRecord[];
  recommendations?: DigestRecommendation[];
  source_records?: {
    gate_results?: number;
    queue_anomalies?: number;
  };
}

export interface GovernanceDigestEnvelope {
  view: string;
  queried_at: string;
  source_freshness: GovernanceDigestSourceFreshness;
  staleness_seconds: number | null;
  degraded: boolean;
  degraded_reasons: string[];
  data: GovernanceDigestData;
}
