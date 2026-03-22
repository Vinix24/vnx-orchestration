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
