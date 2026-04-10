import type {
  ProjectsEnvelope,
  SessionEnvelope,
  TerminalsEnvelope,
  TerminalEnvelope,
  OpenItemsEnvelope,
  AggregateOpenItemsEnvelope,
  KanbanEnvelope,
  ActionOutcome,
  GateConfigResponse,
  GateToggleRequest,
  GateToggleResponse,
  GovernanceDigestEnvelope,
  ReportsEnvelope,
  AgentsEnvelope,
} from './types';

const BASE = '/api/operator';

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = params
    ? `${path}?${new URLSearchParams(params).toString()}`
    : path;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  // 4xx/5xx still return structured ActionOutcome — pass through
  return data;
}

export function fetchProjects(): Promise<ProjectsEnvelope> {
  return get(`${BASE}/projects`);
}

export function fetchOperatorSession(projectPath?: string): Promise<SessionEnvelope> {
  return get(`${BASE}/session`, projectPath ? { project_path: projectPath } : undefined);
}

export function fetchTerminals(): Promise<TerminalsEnvelope> {
  return get(`${BASE}/terminals`);
}

export function fetchTerminal(terminalId: string): Promise<TerminalEnvelope> {
  return get(`${BASE}/terminal/${encodeURIComponent(terminalId)}`);
}

export function fetchOpenItems(
  projectPath?: string,
  severity?: string,
  includeResolved?: boolean,
): Promise<OpenItemsEnvelope> {
  const params: Record<string, string> = {};
  if (projectPath) params.project_path = projectPath;
  if (severity) params.severity = severity;
  if (includeResolved) params.include_resolved = 'true';
  return get(`${BASE}/open-items`, Object.keys(params).length ? params : undefined);
}

export function fetchAggregateOpenItems(project?: string): Promise<AggregateOpenItemsEnvelope> {
  return get(`${BASE}/open-items/aggregate`, project ? { project } : undefined);
}

export function actionStartSession(projectPath: string, dryRun = false): Promise<ActionOutcome> {
  return post(`${BASE}/session/start`, { project_path: projectPath, dry_run: dryRun });
}

export function actionStopSession(projectPath: string, dryRun = false): Promise<ActionOutcome> {
  return post(`${BASE}/session/stop`, { project_path: projectPath, dry_run: dryRun });
}

export function actionAttachTerminal(
  projectPath: string,
  terminalId: string,
  dryRun = false,
): Promise<ActionOutcome> {
  return post(`${BASE}/terminal/attach`, { project_path: projectPath, terminal_id: terminalId, dry_run: dryRun });
}

export function actionRefreshProjections(projectPath: string, dryRun = false): Promise<ActionOutcome> {
  return post(`${BASE}/projections/refresh`, { project_path: projectPath, dry_run: dryRun });
}

export function actionRunReconciliation(projectPath: string, dryRun = false): Promise<ActionOutcome> {
  return post(`${BASE}/reconcile`, { project_path: projectPath, dry_run: dryRun });
}

export function actionInspectOpenItem(projectPath: string, itemId: string): Promise<ActionOutcome> {
  return post(`${BASE}/open-item/inspect`, { project_path: projectPath, item_id: itemId });
}

export function fetchKanban(project?: string): Promise<KanbanEnvelope> {
  return get(`${BASE}/kanban`, project ? { project } : undefined);
}

export function fetchGateConfig(project?: string): Promise<GateConfigResponse> {
  return get(`${BASE}/gate/config`, project ? { project } : undefined);
}

export function postGateToggle(req: GateToggleRequest): Promise<GateToggleResponse> {
  return post(`${BASE}/gate/toggle`, req as unknown as Record<string, unknown>);
}

export function fetchGovernanceDigest(): Promise<GovernanceDigestEnvelope> {
  return get(`${BASE}/governance-digest`);
}

export function fetchReports(params?: {
  limit?: number;
  offset?: number;
  terminal?: string;
  track?: string;
}): Promise<ReportsEnvelope> {
  const p: Record<string, string> = {};
  if (params?.limit !== undefined) p.limit = String(params.limit);
  if (params?.offset !== undefined) p.offset = String(params.offset);
  if (params?.terminal) p.terminal = params.terminal;
  if (params?.track) p.track = params.track;
  return get(`${BASE}/reports`, Object.keys(p).length ? p : undefined);
}

export function fetchReportContent(filename: string): Promise<string> {
  return fetch(`${BASE}/reports/${encodeURIComponent(filename)}`).then(res => {
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.text();
  });
}

export function fetchAgents(): Promise<AgentsEnvelope> {
  return get(`${BASE}/agents`);
}
