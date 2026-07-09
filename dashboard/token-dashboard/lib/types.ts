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
  'T-MANAGER': '#f39c12',
  'T0': '#0a2463',
  'T1': '#2e86c1',
  'T2': '#27ae60',
  'T3': '#6B8AE6',
  'unknown': '#8a96ad',
};

export const MODEL_COLORS: Record<string, string> = {
  'claude-opus': '#0a2463',
  'claude-sonnet': '#2e86c1',
  'unknown': '#8a96ad',
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

// ===== Config Control-Plane Types =====
// Mirrors config_registry.all_effective() rows + the config DAO/API contract.

export interface ConfigEntryRow {
  key: string;
  type: 'bool' | 'string' | 'enum';
  category: string; // 'intelligence' | 'dispatch' | 'gate'
  description: string;
  default: string;
  value: string | null;
  is_default: boolean;
  writable_from_ui: boolean;
  requires_approval: boolean;
  planned: boolean;
}

export interface ConfigEnvelope {
  project_id: string;
  config: ConfigEntryRow[];
  queried_at: string;
  error?: string;
}

export interface ConfigSetRequest {
  key: string;
  value: string;
  actor?: string;
  approval_id?: string;
}

export interface ConfigSetResponse {
  status: 'success' | 'failed';
  action?: string;
  project_id?: string;
  key?: string;
  old_value?: string | null;
  new_value?: string;
  event_id?: string;
  actor?: string;
  approval_id?: string | null;
  message?: string;
  timestamp: string;
}

export interface ConfigAuditRow {
  config_key: string;
  old_value: string | null;
  new_value: string;
  changed_by: string;
  changed_at: string;
  approval_id: string | null;
  event_id: string;
}

export interface ConfigAuditEnvelope {
  project_id: string;
  audit: ConfigAuditRow[];
  queried_at: string;
  degraded?: boolean;
}

// ===== Observability (governance / audit-trail) Types =====

export interface ConfidenceEvent {
  dispatch_id: string;
  outcome: string;
  confidence_change: number;
  patterns_boosted: number;
  patterns_decayed: number;
  occurred_at: string;
}

export interface TaggingEvent {
  table_name: string;
  pattern_id: number;
  pattern_title: string | null;
  tags: string[];
  provider: string | null;
  tagged_at: string;
}

export interface ProvenanceRow {
  dispatch_id: string;
  receipt_id: string | null;
  commit_sha: string | null;
  pr_number: number | null;
  chain_status: string;
  gaps: unknown[];
  registered_at: string;
}

export interface CronJob {
  schedule: string;
  command: string;
  last_run: string | null;
}

export interface DaemonProc {
  pid: string;
  name: string;
}

export interface ObservabilityEnvelope {
  project_id: string;
  queried_at: string;
  self_learning: { events: ConfidenceEvent[]; proposals: number; degraded?: boolean };
  tagging: { events: TaggingEvent[]; degraded?: boolean };
  provenance: { by_status: Record<string, number>; recent: ProvenanceRow[]; degraded?: boolean };
  rework: {
    by_role: ReworkRoleStat[];
    by_origin_role: ReworkOriginRole[];
    recent: ReworkEdge[];
    /** Count of benchmark/headless rows excluded from by_role (governed-only scope). */
    benchmark_excluded: number;
    degraded?: boolean;
  };
  runtime: {
    cron: CronJob[];
    daemons: DaemonProc[];
    daemons_running: number;
    cron_degraded?: boolean;
    daemons_degraded?: boolean;
  };
}

export interface ReworkRoleStat {
  role: string;
  total_dispatches: number;
  successes: number;
  success_rate: number;
}

export interface ReworkOriginRole {
  origin_role: string;
  reworked: number;
}

export interface ReworkEdge {
  rework_dispatch: string;
  rework_role: string | null;
  origin_dispatch: string;
  origin_role: string | null;
  dispatched_at: string | null;
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
  reason?: string;
  stage: string;
  domain: string;
  duration_secs: number;
  duration_label: string;
  has_receipt: boolean;
  receipt_status: string | null;
  /** True when a scout pre-pass sidecar exists for this dispatch (door scout producer). */
  scout_enriched?: boolean;
  /** Future-ready lane only: the deliverable output kind (pr|post|deal|doc). */
  output_kind?: string;
  /** Future-ready lane only: coordination-DB state (proposed|ready|queued). */
  state?: string;
  /** Future-ready lane only: true when human-gated promoted (operator_approved_at set). */
  promoted?: boolean;
}

// ---- System health ----
export interface SystemHealthComponent {
  status: string; // healthy | degraded | dead
  details: Record<string, unknown>;
}

export interface SystemHealthEnvelope {
  status: string; // overall: healthy | degraded | dead
  queried_at: string;
  components: Record<string, SystemHealthComponent>;
  health_score: number;
}

// ---- Planning / future-state ----
export interface PlanningDeliverable {
  deliverable_ref: string;
  output_kind: string;
  derived_status: string;
  dispatch_count: number;
}

export interface PlanningOpenItem {
  oi_id: string;
  link_type: string;
  title: string;
  severity: string | null;
  status: string | null;
}

export interface PlanningDependency {
  to_track_id: string;
  to_project_id: string | null;
  kind: string;
  confidence: number | null;
}

export interface PlanningCard {
  track_id: string;
  title: string;
  phase: string;
  horizon: string;
  priority: string | null;
  next_up: boolean;
  pr_ref: string | null;
  dispatch_count: number;
  depends_on: PlanningDependency[];
  deliverables: PlanningDeliverable[];
  open_items: PlanningOpenItem[];
}

export type PlanningHorizon = 'now' | 'next' | 'later';

export interface PlanningDrift {
  generated_at: string | null;
  divergent_count: number;
  total_tracks: number;
  divergent: unknown[];
}

export interface PlanningEnvelope {
  queried_at: string;
  project_id: string;
  horizons: Record<PlanningHorizon, PlanningCard[]>;
  total_tracks: number;
  drift?: PlanningDrift;
  degraded?: boolean;
  degraded_reasons?: string[];
}

export type KanbanStageName = 'queued' | 'staging' | 'pending' | 'active' | 'review' | 'done';

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

// ===== Intelligence Types =====

export interface SuccessPattern {
  title: string;
  confidence: number;
  category: string;
  used_count: number;
  last_seen: string;
}

export interface Antipattern {
  title: string;
  severity: 'critical' | 'high' | 'medium' | 'low' | string;
  occurrence_count: number;
  last_seen: string;
}

export interface PatternsResponse {
  success_patterns: SuccessPattern[];
  antipatterns: Antipattern[];
}

export interface InjectionEvent {
  timestamp: string;
  dispatch_id: string;
  items_injected: number;
  items_suppressed: number;
}

export interface InjectionsResponse {
  injections: InjectionEvent[];
}

export interface ClassificationRecord {
  report_file: string;
  quality_score: string;
  content_type: string;
  complexity: string;
  summary: string;
}

export interface ClassificationsResponse {
  classifications: ClassificationRecord[];
}

export interface DispatchOutcome {
  dispatch_id: string;
  terminal: string;
  track: string;
  status: string;
  timestamp: string;
}

export interface DispatchOutcomesResponse {
  outcomes: DispatchOutcome[];
}

// ===== Self-Improvement Types =====

export interface Proposal {
  id: number;
  category: string;
  proposed_change: string;
  evidence: string;
  confidence: number;
  status: 'pending' | 'accepted' | 'rejected' | string;
  suggested_at: string;
}

export interface ProposalsResponse {
  proposals: Proposal[];
}

export interface ConfidenceTrend {
  date: string;
  avg_success_confidence: number | null;
  avg_antipattern_severity: number | null;
  pattern_count: number;
}

export interface ConfidenceTrendsResponse {
  trends: ConfidenceTrend[];
}

export interface WeeklyDigestPeriod {
  start: string;
  end: string;
  days: number;
}

export interface WeeklyDigestMetrics {
  patterns_learned: number;
  top_patterns: Array<{ title: string; confidence: number }>;
  antipatterns_active: number;
  top_antipatterns: Array<{ title: string; severity: string }>;
  avg_success_confidence: number | null;
  dispatch_outcomes: { total: number; success: number; failure: number; unknown: number };
  pending_suggestions: number;
  accepted_suggestions: number;
}

export interface WeeklyDigest {
  generated_at: string;
  period: WeeklyDigestPeriod;
  metrics: WeeklyDigestMetrics;
  narrative: string;
}

export interface ProposalActionResponse {
  ok?: boolean;
  id?: number;
  status?: string;
  applied?: number;
  errors?: string[];
  error?: string;
}

export interface LearningProposal {
  id: string;
  type: 'skill_refinement' | 'rule' | 'archival';
  target: string;
  summary: string;
  rationale: string;
  confidence: number;
  created_at: string;
  meta?: Record<string, unknown>;
}

export interface LearningProposalsResponse {
  proposals: LearningProposal[];
}

// ===== Dispatch Viewer Types =====

export type DispatchStage = 'staging' | 'pending' | 'active' | 'review' | 'done' | 'rejected';

export interface DispatchSummary {
  id: string;
  file: string;
  pr_id: string;
  track: string;
  terminal: string;
  role: string;
  gate: string;
  priority: string;
  status: string;
  reason: string;
  domain: string;
  dir: string;
  stage: DispatchStage;
  duration_secs: number;
  duration_label: string;
  has_receipt: boolean;
  receipt_status: string | null;
}

export interface DispatchesResponse {
  stages: Record<DispatchStage, DispatchSummary[]>;
  total: number;
}

export interface DispatchDetailMetadata {
  dispatch_id?: string;
  terminal?: string;
  role?: string;
  gate?: string;
  track?: string;
  pr?: string;
  pr_id?: string;
  priority?: string;
  model?: string;
  status?: string;
  cognition?: string;
  skill?: string;
  [key: string]: string | undefined;
}

export interface DispatchDetailResponse {
  dispatch_id: string;
  stage: string;
  file: string;
  instruction: string;
  metadata: DispatchDetailMetadata;
  error?: string;
}

export type DispatchEventPhase = 'explore' | 'implement' | 'commit' | 'test' | 'other';

export interface DispatchPhaseMarker {
  type: 'phase_marker';
  phase: DispatchEventPhase;
}

export interface DispatchToolUseEvent {
  type: 'tool_use';
  timestamp_offset: number | null;
  tool_name: string;
  file_path: string;
  summary: string;
}

export type DispatchEvent = DispatchPhaseMarker | DispatchToolUseEvent;

export interface DispatchEventsResponse {
  dispatch_id: string;
  events: DispatchEvent[];
  error?: string;
}

export interface DispatchReceipt {
  dispatch_id?: string;
  status?: string;
  terminal?: string;
  track?: string;
  gate?: string;
  pr?: string | number;
  commit_after?: string;
  commit_before?: string;
  duration_secs?: number;
  timestamp?: string;
  [key: string]: unknown;
}

export interface DispatchResultResponse {
  dispatch_id: string;
  receipt: DispatchReceipt | null;
  report: string | null;
  report_file?: string;
  error?: string;
}

// ===== Transcript Types =====

export interface TranscriptMessage {
  role: string;
  content: string;
  timestamp: string;
}

export interface TranscriptResponse {
  messages: TranscriptMessage[];
}

// ===== Reports & Agents Types =====

export interface Report {
  filename: string;
  timestamp: string;
  track: string;
  terminal: string;
  dispatch_id: string;
  pr_id: string;
  status: string;
  title: string;
  auto_generated: boolean;
  slug: string;
}

export interface ReportsEnvelope {
  reports: Report[];
  total: number;
  limit: number;
  offset: number;
}

export interface Agent {
  terminal: string;
  name: string;
  role: string;
  track: string | null;
  adapter: string;
}

export interface AgentsEnvelope {
  agents: Agent[];
  total: number;
}
