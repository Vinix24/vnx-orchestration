// The settings and the bands of the context-rotation guard, ported from its `lib/context_budget.py`
// and `lib/band_guard.py`, and the one line the meter draws from them. Pure: no `$` in this file.
// tests/fixtures/meter_parity.json in the guard's folder holds the cases both sides must agree on.
import { isNum, parseJson, type Json } from './json'
import type { MeterView } from '../types'

export const SETTINGS_FILE = 'context-rotation.json'
export const BAR_CELLS = 8

export type Band = 'below' | 'warn' | 'force' | 'hard'
export type Tone = 'success' | 'warning' | 'error'
export type Source = { kind: 'missing' } | { kind: 'text'; text: string } | { kind: 'unreadable' }
export type Triple = [number, number, number]

export type Settings = {
  enabled: boolean
  showMeter: boolean
  windowTokens: number
  minWorkTokens: number
  warnPct: number
  forcePct: number
  hardPct: number
  tokens: Triple | null
}

const DEFAULTS: Settings = {
  enabled: true,
  showMeter: true,
  windowTokens: 200000,
  minWorkTokens: 20000,
  warnPct: 55,
  forcePct: 65,
  hardPct: 75,
  tokens: null,
}

type Raw = { [key: string]: Json }

function isPlainObject(value: Json): value is Raw {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && !isNum(value)
}

const has = (raw: Raw, key: string): boolean => key in raw

function boolOf(raw: Raw, key: string, fallback: boolean): boolean {
  const value = raw[key]
  return has(raw, key) && typeof value === 'boolean' ? value : fallback
}

function positiveIntOf(raw: Raw, key: string, fallback: number): number {
  const value = raw[key]
  return has(raw, key) && isNum(value) && value.isInt && value.value > 0 ? value.value : fallback
}

function numberOf(value: Json | undefined): number | null {
  return isNum(value) ? value.value : null
}

function percentages(raw: Raw): Triple | null {
  if (!['warn_pct', 'force_pct', 'hard_pct'].some(key => has(raw, key))) return null
  const warn = has(raw, 'warn_pct') ? numberOf(raw.warn_pct) : DEFAULTS.warnPct
  const force = has(raw, 'force_pct') ? numberOf(raw.force_pct) : DEFAULTS.forcePct
  const hard = has(raw, 'hard_pct') ? numberOf(raw.hard_pct) : DEFAULTS.hardPct
  if (warn === null || force === null || hard === null) return null
  return 0 < warn && warn < force && force < hard && hard <= 100 ? [warn, force, hard] : null
}

function tokenOf(raw: Raw, key: string): number | null {
  const value = raw[key]
  return has(raw, key) && isNum(value) && value.isInt && value.value > 0 ? value.value : null
}

function absolute(raw: Raw): Triple | null {
  if (!['warn_tokens', 'force_tokens', 'hard_tokens'].some(key => has(raw, key))) return null
  const warn = tokenOf(raw, 'warn_tokens')
  const force = tokenOf(raw, 'force_tokens')
  const hard = tokenOf(raw, 'hard_tokens')
  if (warn === null || force === null || hard === null) return null
  return warn < force && force < hard ? [warn, force, hard] : null
}

/** `_apply`: every key falls back to its own default, the percentage triple and the token triple as one. */
export function applySettings(raw: Raw): Settings {
  const pct = percentages(raw)
  return {
    enabled: boolOf(raw, 'enabled', DEFAULTS.enabled),
    showMeter: boolOf(raw, 'show_meter', DEFAULTS.showMeter),
    windowTokens: positiveIntOf(raw, 'window_tokens', DEFAULTS.windowTokens),
    minWorkTokens: positiveIntOf(raw, 'min_work_tokens', DEFAULTS.minWorkTokens),
    warnPct: pct ? pct[0] : DEFAULTS.warnPct,
    forcePct: pct ? pct[1] : DEFAULTS.forcePct,
    hardPct: pct ? pct[2] : DEFAULTS.hardPct,
    tokens: absolute(raw),
  }
}

/** `load_settings`: the first file that exists decides, however bad it is; none means the defaults. */
export function resolveSettings(sources: Source[]): Settings {
  for (const source of sources) {
    if (source.kind === 'missing') continue
    if (source.kind === 'unreadable') return applySettings(Object.create(null) as Raw)
    try {
      const data = parseJson(source.text)
      return applySettings(isPlainObject(data) ? data : (Object.create(null) as Raw))
    } catch {
      return applySettings(Object.create(null) as Raw)
    }
  }
  return { ...DEFAULTS }
}

/** `thresholds`: the absolute values when all three are valid, else the share of the window. */
export function thresholds(settings: Settings): Triple {
  if (settings.tokens) return settings.tokens
  const window = settings.windowTokens
  return [(window * settings.warnPct) / 100, (window * settings.forcePct) / 100, (window * settings.hardPct) / 100]
}

/** `band`: below, warn, force or hard for a measured size. */
export function bandOf(size: number, [warn, force, hard]: Triple): Band {
  if (size >= hard) return 'hard'
  if (size >= force) return 'force'
  return size >= warn ? 'warn' : 'below'
}

/** True while an injected session has not grown `min_work_tokens` past its start size. */
export function isHeld(size: number, startSize: number | null, minWorkTokens: number): boolean {
  return startSize !== null && size - startSize < minWorkTokens
}

/**
 * `band_guard._loop_level`: the level the guard enforces. A session continued from an injected handover
 * is held below force and hard until it grew `min_work_tokens` past its start size.
 */
export function guardLevel(size: number, startSize: number | null, settings: Settings): 'low' | 'force' | 'hard' {
  const band = bandOf(size, thresholds(settings))
  if ((band === 'force' || band === 'hard') && isHeld(size, startSize, settings.minWorkTokens)) return 'low'
  return band === 'force' || band === 'hard' ? band : 'low'
}

/** What a session's view needs from the settings and the guard's own state, as plain data. */
export function viewOf(
  context: { tokens?: number; window: number },
  settings: Settings,
  startSize: number | null,
): MeterView {
  const [warn, force, hard] = thresholds(settings)
  return {
    tokens: typeof context.tokens === 'number' ? context.tokens : null,
    window: context.window,
    warn,
    force,
    hard,
    fromPct: settings.tokens === null,
    windowTokens: settings.windowTokens,
    enabled: settings.enabled,
    showMeter: settings.showMeter,
    minWorkTokens: settings.minWorkTokens,
    startSize,
  }
}

/** The guard's enforcement state `start_size`, a non-negative integer, or null. */
export function startSizeOf(text: string | undefined): number | null {
  if (text === undefined) return null
  try {
    const state = parseJson(text)
    if (!isPlainObject(state)) return null
    const size = state.start_size
    return isNum(size) && size.isInt && size.value >= 0 ? size.value : null
  } catch {
    return null
  }
}

/** A token count as the line shows it: 850, 3.2K, 412K, 1.2M. */
export function tokenText(tokens: number): string {
  const shown = (value: number, unit: string, digits: number) => String(Number(value.toFixed(digits))) + unit
  if (tokens < 1000) return String(Math.round(tokens))
  if (tokens < 10000) return shown(tokens / 1000, 'K', 1)
  if (tokens < 1000000) return shown(Math.round(tokens / 1000), 'K', 0)
  return shown(tokens / 1000000, 'M', 1)
}

export function barOf(size: number, hard: number): string {
  const filled = Math.max(0, Math.min(BAR_CELLS, Math.floor((size / hard) * BAR_CELLS)))
  return '▓'.repeat(filled) + '░'.repeat(BAR_CELLS - filled)
}

export type Piece = { text: string; tone?: Tone; isDim?: boolean }

const ACTION: Record<Band, string> = {
  below: '',
  warn: 'wrap up',
  force: 'write the handover',
  hard: 'only reads are allowed',
}
const TONE: Record<Band, Tone> = { below: 'success', warn: 'warning', force: 'error', hard: 'error' }
const BAND_WORD: Record<Band, string> = { below: 'ok', warn: 'warn', force: 'force', hard: 'hard' }

function nextThreshold(view: MeterView, band: Band): string | null {
  if (band === 'hard') return null
  if (band === 'force') return 'hard at ' + tokenText(view.hard)
  const afterStart = view.startSize === null ? 0 : view.startSize + view.minWorkTokens
  if (afterStart > view.force) return 'force from ' + tokenText(afterStart)
  return band === 'warn' ? 'force at ' + tokenText(view.force) : 'warn at ' + tokenText(view.warn)
}

function notes(view: MeterView): string[] {
  const found: string[] = []
  if (view.fromPct && view.window !== view.windowTokens) {
    found.push('window ' + tokenText(view.window) + ', bands assume ' + tokenText(view.windowTokens))
  }
  if (view.hard > view.window) {
    found.push('hard ' + tokenText(view.hard) + ' is above the ' + tokenText(view.window) + ' window')
  }
  return found
}

/** What the line says for a view, as pieces in the order they are dropped from the end. Null: nothing to draw. */
export function pieces(view: MeterView): Piece[] | null {
  if (!view.showMeter) return null
  if (view.tokens === null) return [{ text: 'context: no reading yet', isDim: true }]
  const head = 'context ' + barOf(view.tokens, view.hard) + ' ' + tokenText(view.tokens)
  if (!view.enabled) return [{ text: head, isDim: true }, { text: ' · rotation off', isDim: true }]

  const raw = bandOf(view.tokens, [view.warn, view.force, view.hard])
  const held = (raw === 'force' || raw === 'hard') && isHeld(view.tokens, view.startSize, view.minWorkTokens)
  const band: Band = held ? 'warn' : raw
  const tone = TONE[band]
  const out: Piece[] = [{ text: head, tone }, { text: ' · ' + BAND_WORD[band], tone }]
  if (ACTION[band]) out.push({ text: ': ' + ACTION[band], tone })
  const next = nextThreshold(view, band)
  if (next) out.push({ text: ' · ' + next, tone })
  for (const note of notes(view)) out.push({ text: ' · ' + note, tone: 'warning' })
  return out
}

const cells = (list: Piece[]) => list.reduce((sum, piece) => sum + [...piece.text].length, 0)

/** Fits the line to `columns` cells: trailing pieces go first, then the bar, never the band word. */
export function fit(list: Piece[], columns: number): Piece[] {
  const [first] = list
  if (first === undefined) return list
  const floor = first.text.startsWith('context:') ? 1 : 2
  let kept = list
  while (kept.length > floor && cells(kept) > columns) kept = kept.slice(0, -1)
  if (cells(kept) > columns) {
    kept = [{ ...first, text: first.text.replace(/ [▓░]+/, '') }, ...kept.slice(1)]
  }
  return kept
}
