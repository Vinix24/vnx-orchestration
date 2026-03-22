import useSWR from 'swr';
import { fetchTokenStats, fetchSessions } from './api';
import type { TokenStats, SessionDetail, GroupBy } from './types';

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
