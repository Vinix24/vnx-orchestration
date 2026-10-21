// The rows `vnx providers usage --json` (schema vnx.providers.usage/1) writes, with `source`, `store` and every path left out.
import type { Config, RateLimit } from '../types'

export const CONFIG: Config = {
  vnxBin: '/opt/vnx/bin/vnx',
  refreshMinutes: 10,
  warnPercent: 70,
  errorPercent: 90,
  warnBalance: 5,
  errorBalance: 1,
}

export const CLAUDE_ROW = { available: false, reason: 'alleen in een Claude Code-sessie meetbaar' }

export const CODEX_ROW = {
  available: true,
  used_percent: 58,
  window_minutes: 300,
  resets_at: '2026-10-21T23:00:00Z',
  plan_type: 'plus',
  age_seconds: 12,
}

export const CODEX_UNAVAILABLE = { available: false, reason: 'geen rate_limits-regel in rollout-example.jsonl' }

export const KIMI_FULL = {
  available: true,
  status: 'vol tot reset',
  reason: 'auth_401',
  measured_at: '2026-10-21T19:07:12Z',
  age_seconds: 849,
  resets_at: 'onbekend',
}

export const KIMI_REACHABLE = { ...KIMI_FULL, status: 'bereikbaar', reason: null }

export const DEEPSEEK_ROW = {
  available: true,
  is_available: true,
  balances: [{ currency: 'USD', total: '12.92', granted: '0.00', topped_up: '12.92' }],
}

export const DEEPSEEK_UNAVAILABLE = { available: false, reason: 'geen DEEPSEEK_API_KEY in de omgeving of het provider-env-bestand' }

export const OPENROUTER_ROW = { available: true, total_credits: 505, total_usage: 500.085341063, remaining: 4.914659 }

export const OPENROUTER_UNAVAILABLE = { available: false, reason: 'http 402: onvoldoende saldo' }

export const reportJson = (providers: Record<string, unknown>, schema = 'vnx.providers.usage/1'): string =>
  JSON.stringify({ schema, measured_at: '2026-10-21T19:21:21Z', providers })

export const FULL_REPORT = reportJson({
  claude: CLAUDE_ROW,
  codex: CODEX_ROW,
  kimi: KIMI_FULL,
  deepseek: DEEPSEEK_ROW,
  openrouter: OPENROUTER_ROW,
})

export const RATE_LIMITS: readonly RateLimit[] = [
  { kind: 'five_hour', percentUsed: 34, resetsAt: '2026-10-21T23:00:00.000Z' },
  { kind: 'seven_day', percentUsed: 61, resetsAt: '2026-10-27T16:00:00.000Z' },
]

export const OLD_VNX = {
  exitCode: 2,
  stdout: '',
  stderr:
    "usage: vnx providers [-h] VERB ...\nvnx providers: error: argument VERB: invalid choice: 'usage' (choose from list, off, on, reset, repair, check)\n",
}
