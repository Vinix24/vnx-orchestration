export type MeterView = {
  /** Tokens of the last response, or null while there is no reading. */
  tokens: number | null
  /** The context window of the session's model, in tokens. */
  window: number
  /** The warn, force and hard thresholds in tokens, as the guard computes them. */
  warn: number
  force: number
  hard: number
  /** True when the thresholds come from the percentages of `window_tokens`. */
  fromPct: boolean
  windowTokens: number
  enabled: boolean
  showMeter: boolean
  minWorkTokens: number
  /** The guard's recorded start size of this session, or null. */
  startSize: number | null
}

declare module 'claude-code' {
  interface PluginState {
    'context-meter': { view: MeterView | null }
  }
}
