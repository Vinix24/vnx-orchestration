import useSWR from 'swr';
import { fetchTokenStats, fetchSessions, fetchConversations, fetchTranscript } from './api';
import {
  fetchProjects,
  fetchOperatorSession,
  fetchTerminals,
  fetchOpenItems,
  fetchAggregateOpenItems,
  fetchKanban,
  fetchGateConfig,
  fetchGovernanceDigest,
  fetchReports,
  fetchReportContent,
  fetchAgents,
  fetchIntelligencePatterns,
  fetchIntelligenceInjections,
  fetchIntelligenceClassifications,
  fetchIntelligenceDispatchOutcomes,
  fetchProposals,
  fetchConfidenceTrends,
  fetchWeeklyDigest,
  fetchDispatches,
  fetchDispatchDetail,
  fetchDispatchEvents,
  fetchDispatchResult,
} from './operator-api';
import type {
  TokenStats, SessionDetail, GroupBy, SortOrder, ConversationsResponse, TranscriptResponse,
  ProjectsEnvelope, SessionEnvelope, TerminalsEnvelope,
  OpenItemsEnvelope, AggregateOpenItemsEnvelope, KanbanEnvelope,
  GateConfigResponse, GovernanceDigestEnvelope,
  ReportsEnvelope, AgentsEnvelope,
  PatternsResponse, InjectionsResponse, ClassificationsResponse, DispatchOutcomesResponse,
  ProposalsResponse, ConfidenceTrendsResponse, WeeklyDigest,
  DispatchesResponse, DispatchDetailResponse, DispatchEventsResponse, DispatchResultResponse,
} from './types';

export function useTokenStats(
  from: string,
  to: string,
  group: GroupBy,
  terminal?: string,
  model?: string
) {
  const key = from && to
    ? ['token-stats', from, to, group, terminal ?? '', model ?? '']
    : null;

  return useSWR<TokenStats[]>(
    key,
    () => fetchTokenStats(from, to, group, terminal, model),
    {
      revalidateOnFocus: false,
      dedupingInterval: 30000,
    }
  );
}

export function useSessions(date: string, terminal?: string) {
  const key = date
    ? ['sessions', date, terminal ?? '']
    : null;

  return useSWR<SessionDetail[]>(
    key,
    () => fetchSessions(date, terminal),
    {
      revalidateOnFocus: false,
      dedupingInterval: 30000,
    }
  );
}

export function useConversations(
  sortOrder: SortOrder,
  terminal?: string,
  worktree?: string,
  limit?: number
) {
  const key = ['conversations', sortOrder, terminal ?? '', worktree ?? '', String(limit ?? 50)];

  return useSWR<ConversationsResponse>(
    key,
    () => fetchConversations(sortOrder, terminal, worktree, limit),
    {
      revalidateOnFocus: false,
      dedupingInterval: 15000,
    }
  );
}

export function useTranscript(sessionId: string | null) {
  return useSWR<TranscriptResponse>(
    sessionId ? ['transcript', sessionId] : null,
    () => fetchTranscript(sessionId!),
    { revalidateOnFocus: false, dedupingInterval: 60000 }
  );
}

// ===== Operator Dashboard Hooks =====

export function useProjects() {
  return useSWR<ProjectsEnvelope>(
    'operator-projects',
    () => fetchProjects(),
    { refreshInterval: 30000, revalidateOnFocus: true, dedupingInterval: 10000 }
  );
}

export function useOperatorSession(projectPath?: string) {
  const key = ['operator-session', projectPath ?? ''];
  return useSWR<SessionEnvelope>(
    key,
    () => fetchOperatorSession(projectPath),
    { refreshInterval: 30000, revalidateOnFocus: true, dedupingInterval: 10000 }
  );
}

export function useTerminals() {
  return useSWR<TerminalsEnvelope>(
    'operator-terminals',
    () => fetchTerminals(),
    { refreshInterval: 15000, revalidateOnFocus: true, dedupingInterval: 8000 }
  );
}

export function useOpenItems(projectPath?: string, severity?: string) {
  const key = ['operator-open-items', projectPath ?? '', severity ?? ''];
  return useSWR<OpenItemsEnvelope>(
    key,
    () => fetchOpenItems(projectPath, severity),
    { refreshInterval: 20000, revalidateOnFocus: true, dedupingInterval: 8000 }
  );
}

export function useAggregateOpenItems(project?: string) {
  const key = ['operator-open-items-aggregate', project ?? ''];
  return useSWR<AggregateOpenItemsEnvelope>(
    key,
    () => fetchAggregateOpenItems(project),
    { refreshInterval: 20000, revalidateOnFocus: true, dedupingInterval: 8000 }
  );
}

export function useKanban(project?: string) {
  const key = project ? ['operator-kanban', project] : 'operator-kanban';
  return useSWR<KanbanEnvelope>(
    key,
    () => fetchKanban(project),
    { refreshInterval: 15000, revalidateOnFocus: true, dedupingInterval: 8000 }
  );
}

export function useGateConfig(project?: string) {
  const key = project ? ['operator-gate-config', project] : 'operator-gate-config';
  return useSWR<GateConfigResponse>(
    key,
    () => fetchGateConfig(project),
    { refreshInterval: 30000, revalidateOnFocus: true, dedupingInterval: 10000 }
  );
}

export function useGovernanceDigest() {
  return useSWR<GovernanceDigestEnvelope>(
    'operator-governance-digest',
    fetchGovernanceDigest,
    { refreshInterval: 60000, revalidateOnFocus: true, dedupingInterval: 15000 }
  );
}

export function useReports(params?: { limit?: number; offset?: number; terminal?: string; track?: string }) {
  const key = [
    'operator-reports',
    String(params?.limit ?? 50),
    String(params?.offset ?? 0),
    params?.terminal ?? '',
    params?.track ?? '',
  ];
  return useSWR<ReportsEnvelope>(
    key,
    () => fetchReports(params),
    { refreshInterval: 30000, revalidateOnFocus: true, dedupingInterval: 10000 }
  );
}

export function useReportContent(filename: string | null) {
  return useSWR<string>(
    filename ? ['operator-report-content', filename] : null,
    () => fetchReportContent(filename!),
    { revalidateOnFocus: false, dedupingInterval: 60000 }
  );
}

export function useAgents() {
  return useSWR<AgentsEnvelope>(
    'operator-agents',
    fetchAgents,
    { refreshInterval: 60000, revalidateOnFocus: true, dedupingInterval: 20000 }
  );
}

// ===== Intelligence Hooks =====

export function useIntelligencePatterns() {
  return useSWR<PatternsResponse>(
    'intelligence-patterns',
    () => fetchIntelligencePatterns(),
    { refreshInterval: 60000, revalidateOnFocus: true, dedupingInterval: 20000 }
  );
}

export function useIntelligenceInjections() {
  return useSWR<InjectionsResponse>(
    'intelligence-injections',
    () => fetchIntelligenceInjections(),
    { refreshInterval: 60000, revalidateOnFocus: true, dedupingInterval: 20000 }
  );
}

export function useIntelligenceClassifications() {
  return useSWR<ClassificationsResponse>(
    'intelligence-classifications',
    () => fetchIntelligenceClassifications(),
    { refreshInterval: 60000, revalidateOnFocus: true, dedupingInterval: 20000 }
  );
}

export function useIntelligenceDispatchOutcomes() {
  return useSWR<DispatchOutcomesResponse>(
    'intelligence-dispatch-outcomes',
    () => fetchIntelligenceDispatchOutcomes(),
    { refreshInterval: 60000, revalidateOnFocus: true, dedupingInterval: 20000 }
  );
}

// ===== Self-Improvement Hooks =====

export function useProposals() {
  return useSWR<ProposalsResponse>(
    'intelligence-proposals',
    () => fetchProposals(),
    { refreshInterval: 30000, revalidateOnFocus: true, dedupingInterval: 10000 }
  );
}

export function useConfidenceTrends() {
  return useSWR<ConfidenceTrendsResponse>(
    'intelligence-confidence-trends',
    () => fetchConfidenceTrends(),
    { refreshInterval: 120000, revalidateOnFocus: true, dedupingInterval: 30000 }
  );
}

export function useWeeklyDigest() {
  return useSWR<WeeklyDigest>(
    'intelligence-weekly-digest',
    () => fetchWeeklyDigest(),
    { refreshInterval: 300000, revalidateOnFocus: true, dedupingInterval: 60000 }
  );
}

// ===== Dispatch Viewer Hooks =====

export function useDispatches(params?: {
  terminal?: string;
  role?: string;
  status?: string;
  limit?: number;
}) {
  const key = [
    'dispatches',
    params?.terminal ?? '',
    params?.role ?? '',
    params?.status ?? '',
    String(params?.limit ?? 100),
  ];
  return useSWR<DispatchesResponse>(
    key,
    () => fetchDispatches(params),
    { refreshInterval: 30000, revalidateOnFocus: true, dedupingInterval: 10000 }
  );
}

export function useDispatchDetail(id: string | null) {
  return useSWR<DispatchDetailResponse>(
    id ? ['dispatch-detail', id] : null,
    () => fetchDispatchDetail(id!),
    { revalidateOnFocus: false, dedupingInterval: 30000 }
  );
}

export function useDispatchEvents(id: string | null) {
  return useSWR<DispatchEventsResponse>(
    id ? ['dispatch-events', id] : null,
    () => fetchDispatchEvents(id!),
    { revalidateOnFocus: false, dedupingInterval: 30000 }
  );
}

export function useDispatchResult(id: string | null) {
  return useSWR<DispatchResultResponse>(
    id ? ['dispatch-result', id] : null,
    () => fetchDispatchResult(id!),
    { revalidateOnFocus: false, dedupingInterval: 30000 }
  );
}
