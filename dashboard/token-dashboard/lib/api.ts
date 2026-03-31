import type { TokenStats, SessionDetail, GroupBy, SortOrder, ConversationsResponse } from './types';

const BASE_URL = '/api/token-stats';

export async function fetchTokenStats(
  from: string,
  to: string,
  group: GroupBy,
  terminal?: string,
  model?: string
): Promise<TokenStats[]> {
  const params = new URLSearchParams({ from, to, group });
  if (terminal) params.set('terminal', terminal);
  if (model) params.set('model', model);

  const res = await fetch(`${BASE_URL}?${params.toString()}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch token stats: ${res.status} ${res.statusText}`);
  }
  const json = await res.json();
  return json.data;
}

export async function fetchSessions(
  date: string,
  terminal?: string
): Promise<SessionDetail[]> {
  const params = new URLSearchParams({ date });
  if (terminal) params.set('terminal', terminal);

  const res = await fetch(`${BASE_URL}/sessions?${params.toString()}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch sessions: ${res.status} ${res.statusText}`);
  }
  const json = await res.json();
  return json.data;
}

export async function fetchConversations(
  sortOrder: SortOrder,
  terminal?: string,
  worktree?: string,
  limit?: number
): Promise<ConversationsResponse> {
  const params = new URLSearchParams({ sort: sortOrder });
  if (terminal) params.set('terminal', terminal);
  if (worktree) params.set('worktree', worktree);
  if (limit) params.set('limit', String(limit));
  params.set('group', 'worktree');

  const res = await fetch(`/api/conversations?${params.toString()}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch conversations: ${res.status} ${res.statusText}`);
  }
  return res.json();
}
