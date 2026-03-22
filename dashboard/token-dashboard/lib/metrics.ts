import type { TokenStats } from './types';

export interface PeriodAggregate {
  period: string;
  sessions: number;
  api_calls: number;
  total_output_tokens: number;
  context_per_call_K: number;
  cache_hit_pct: number;
  new_per_call_K: number;
  output_per_call_K: number;
}

export interface TerminalAggregate {
  terminal: string;
  sessions: number;
  api_calls: number;
  total_output_tokens: number;
  context_per_call_K: number;
  cache_hit_pct: number;
}

export interface ModelAggregate {
  model: string;
  sessions: number;
  api_calls: number;
  total_output_tokens: number;
  context_per_call_K: number;
  cache_hit_pct: number;
}

export function weightedAverage(
  data: TokenStats[],
  valueKey: keyof TokenStats,
  weightKey: keyof TokenStats
): number {
  let totalWeight = 0;
  let weightedSum = 0;
  for (const row of data) {
    const weight = Number(row[weightKey]) || 0;
    const value = Number(row[valueKey]) || 0;
    weightedSum += value * weight;
    totalWeight += weight;
  }
  return totalWeight > 0 ? weightedSum / totalWeight : 0;
}

export function aggregateByPeriod(data: TokenStats[]): PeriodAggregate[] {
  const map = new Map<string, TokenStats[]>();
  for (const row of data) {
    const existing = map.get(row.period) ?? [];
    existing.push(row);
    map.set(row.period, existing);
  }

  const result: PeriodAggregate[] = [];
  for (const [period, rows] of map) {
    result.push({
      period,
      sessions: rows.reduce((s, r) => s + r.sessions, 0),
      api_calls: rows.reduce((s, r) => s + r.api_calls, 0),
      total_output_tokens: rows.reduce((s, r) => s + r.total_output_tokens, 0),
      context_per_call_K: weightedAverage(rows, 'context_per_call_K', 'api_calls'),
      cache_hit_pct: weightedAverage(rows, 'cache_hit_pct', 'api_calls'),
      new_per_call_K: weightedAverage(rows, 'new_per_call_K', 'api_calls'),
      output_per_call_K: weightedAverage(rows, 'output_per_call_K', 'api_calls'),
    });
  }

  return result.sort((a, b) => a.period.localeCompare(b.period));
}

export function aggregateByTerminal(data: TokenStats[]): TerminalAggregate[] {
  const map = new Map<string, TokenStats[]>();
  for (const row of data) {
    const existing = map.get(row.terminal) ?? [];
    existing.push(row);
    map.set(row.terminal, existing);
  }

  const result: TerminalAggregate[] = [];
  for (const [terminal, rows] of map) {
    result.push({
      terminal,
      sessions: rows.reduce((s, r) => s + r.sessions, 0),
      api_calls: rows.reduce((s, r) => s + r.api_calls, 0),
      total_output_tokens: rows.reduce((s, r) => s + r.total_output_tokens, 0),
      context_per_call_K: weightedAverage(rows, 'context_per_call_K', 'api_calls'),
      cache_hit_pct: weightedAverage(rows, 'cache_hit_pct', 'api_calls'),
    });
  }

  return result;
}

export function aggregateByModel(data: TokenStats[]): ModelAggregate[] {
  const map = new Map<string, TokenStats[]>();
  for (const row of data) {
    const existing = map.get(row.model) ?? [];
    existing.push(row);
    map.set(row.model, existing);
  }

  const result: ModelAggregate[] = [];
  for (const [model, rows] of map) {
    result.push({
      model,
      sessions: rows.reduce((s, r) => s + r.sessions, 0),
      api_calls: rows.reduce((s, r) => s + r.api_calls, 0),
      total_output_tokens: rows.reduce((s, r) => s + r.total_output_tokens, 0),
      context_per_call_K: weightedAverage(rows, 'context_per_call_K', 'api_calls'),
      cache_hit_pct: weightedAverage(rows, 'cache_hit_pct', 'api_calls'),
    });
  }

  return result;
}

/** Build stacked chart data: each row has period + one key per terminal */
export function buildStackedByTerminal(
  data: TokenStats[],
  valueKey: keyof TokenStats
): Record<string, string | number>[] {
  const periodMap = new Map<string, Record<string, string | number>>();
  const terminals = new Set<string>();

  for (const row of data) {
    terminals.add(row.terminal);
    if (!periodMap.has(row.period)) {
      periodMap.set(row.period, { period: row.period });
    }
    const entry = periodMap.get(row.period)!;
    entry[row.terminal] = ((entry[row.terminal] as number) || 0) + Number(row[valueKey]);
  }

  return Array.from(periodMap.values()).sort((a, b) =>
    (a.period as string).localeCompare(b.period as string)
  );
}

/** Build line chart data: each row has period + one key per terminal for per-call metrics */
export function buildLineByTerminal(
  data: TokenStats[],
  valueKey: keyof TokenStats
): Record<string, string | number>[] {
  // Group by period+terminal, weighted average by api_calls
  const grouped = new Map<string, Map<string, TokenStats[]>>();

  for (const row of data) {
    if (!grouped.has(row.period)) {
      grouped.set(row.period, new Map());
    }
    const periodMap = grouped.get(row.period)!;
    if (!periodMap.has(row.terminal)) {
      periodMap.set(row.terminal, []);
    }
    periodMap.get(row.terminal)!.push(row);
  }

  const result: Record<string, string | number>[] = [];
  for (const [period, terminalMap] of grouped) {
    const entry: Record<string, string | number> = { period };
    for (const [terminal, rows] of terminalMap) {
      entry[terminal] = Number(weightedAverage(rows, valueKey, 'api_calls').toFixed(1));
    }
    result.push(entry);
  }

  return result.sort((a, b) =>
    (a.period as string).localeCompare(b.period as string)
  );
}

/** Extract unique terminal names from data */
export function getTerminals(data: TokenStats[]): string[] {
  return Array.from(new Set(data.map((r) => r.terminal))).sort();
}

/** Extract unique model names from data */
export function getModels(data: TokenStats[]): string[] {
  return Array.from(new Set(data.map((r) => r.model))).sort();
}
