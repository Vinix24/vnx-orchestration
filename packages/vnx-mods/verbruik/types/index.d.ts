/** How a segment is meant to be drawn; `dim` is a figure that is missing or could not be measured. */
export type Tone = 'success' | 'warning' | 'error' | 'dim'

/** The other providers `vnx providers usage --json` reports; its `claude` row is ignored. */
export type ProviderName = 'codex' | 'kimi' | 'deepseek' | 'openrouter'

/** One balance of a provider, in its own currency. */
export type Balance = { currency: string; amount: number }

/**
 * What one provider row holds. A figure that could not be read is `unavailable`
 * with its reason, never a zero.
 */
export type Figure =
  | { kind: 'percent'; percent: number }
  | { kind: 'status'; text: string }
  | { kind: 'balances'; balances: Balance[] }
  | { kind: 'unavailable'; reason: string }

/** A parsed `vnx.providers.usage/1` report: only the figures, no `source`, `store` or path. */
export type Report = {
  measuredAt: string | null
  rows: { name: ProviderName; figure: Figure }[]
}

/** What one run of `vnx providers usage --json` came to. */
export type Outcome = { kind: 'ok'; report: Report } | { kind: 'failed'; cause: string } | { kind: 'too-old' }

/** The other providers as the status line knows them: the last good report and why it may be stale. */
export type Fleet = {
  report: Report | null
  /** Why the last run failed; null when it succeeded or none has failed yet. */
  cause: string | null
  /** True when the installed vnx has no `providers usage`. */
  isTooOld: boolean
}

/** The window of the Claude subscription as `$.session.usage().rateLimits` holds it. */
export type RateLimit = { kind: string; percentUsed: number; resetsAt?: string }

export type SegmentKey = 'claude5' | 'claudeWeek' | 'codex' | 'kimi' | 'deepseek' | 'openrouter' | 'notice'

/** A part of the status line. The text carries the meaning; a warning or error also ends in `!` or `!!`. */
export type Segment = { key: SegmentKey; text: string; tone: Tone }

export type Config = {
  vnxBin: string
  refreshMinutes: number
  warnPercent: number
  errorPercent: number
  warnBalance: number
  errorBalance: number
}
