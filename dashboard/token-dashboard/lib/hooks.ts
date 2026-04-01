import useSWR from 'swr';
import { fetchTokenStats, fetchSessions, fetchConversations } from './api';
import {
  fetchProjects,
  fetchOperatorSession,
  fetchTerminals,
  fetchOpenItems,
  fetchAggregateOpenItems,
} from './operator-api';
import type {
  TokenStats, SessionDetail, GroupBy, SortOrder, ConversationsResponse,
  ProjectsEnvelope, SessionEnvelope, TerminalsEnvelope,
  OpenItemsEnvelope, AggregateOpenItemsEnvelope,
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
