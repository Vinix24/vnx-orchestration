import useSWR from 'swr';
import { fetchTokenStats, fetchSessions, fetchConversations } from './api';
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
} from './operator-api';
import type {
  TokenStats, SessionDetail, GroupBy, SortOrder, ConversationsResponse,
  ProjectsEnvelope, SessionEnvelope, TerminalsEnvelope,
  OpenItemsEnvelope, AggregateOpenItemsEnvelope, KanbanEnvelope,
  GateConfigResponse, GovernanceDigestEnvelope,
  ReportsEnvelope, AgentsEnvelope,
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
