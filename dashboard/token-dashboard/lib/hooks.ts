import useSWR from 'swr';
import { fetchTokenStats, fetchSessions, fetchConversations } from './api';
import type { TokenStats, SessionDetail, GroupBy, SortOrder, ConversationsResponse } from './types';

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
