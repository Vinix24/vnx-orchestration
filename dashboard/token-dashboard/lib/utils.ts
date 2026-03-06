export function normalizeTerminalStatus(status: string | undefined, terminalId?: string): string {
  const value = String(status || '').toLowerCase();
  if (['working', 'active', 'busy', 'claimed', 'in_progress'].includes(value)) return 'working';
  if (value === 'idle') return 'idle';
  if (['blocked', 'error', 'failed', 'timeout', 'no_confirmation'].includes(value)) return 'blocked';
  // T0 is always the orchestrator — "unknown" means it's running but not tracked
  if (value === 'unknown' && terminalId === 'T0') return 'idle';
  if (value === 'unknown') return 'unknown';
  return 'offline';
}

export function terminalStatusColor(status: string): string {
  if (status === 'working') return '#f97316';
  if (status === 'idle') return '#50fa7b';
  if (status === 'blocked') return '#ff6b6b';
  if (status === 'unknown') return '#facc15';
  return '#ff6b6b';
}

export function getProviderLabel(provider: string, terminalId?: string): string {
  if (terminalId === 'T0') return 'Orchestrator';
  const p = String(provider || '').toLowerCase();
  if (p.includes('codex')) return 'Codex CLI';
  if (p.includes('gemini')) return 'Gemini CLI';
  return 'Claude Code';
}

export function severityColor(severity: string): string {
  const sev = severity.toLowerCase();
  if (sev === 'blocker') return '#ff6b6b';
  if (sev === 'warn' || sev === 'warning') return '#facc15';
  return '#60a5fa';
}

export function severityLabel(severity: string): string {
  const sev = severity.toLowerCase();
  if (sev === 'blocker') return 'BLOCKER';
  if (sev === 'warn' || sev === 'warning') return 'WARNING';
  return 'INFO';
}

export function severityBgColor(severity: string): string {
  const sev = severity.toLowerCase();
  if (sev === 'blocker') return 'rgba(255, 107, 107, 0.08)';
  if (sev === 'warn' || sev === 'warning') return 'rgba(250, 204, 21, 0.07)';
  return 'rgba(96, 165, 250, 0.07)';
}

export function severityBorderColor(severity: string): string {
  const sev = severity.toLowerCase();
  if (sev === 'blocker') return '#ff6b6b';
  if (sev === 'warn' || sev === 'warning') return '#facc15';
  return '#60a5fa';
}
