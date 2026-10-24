// The pure part of the mod: parsing `vnx providers usage --json`, the state between runs, the segments and the fit.
import type { PluginOptions } from 'claude-code'

import type { Config, Figure, Fleet, Outcome, ProviderName, RateLimit, Report, Segment, SegmentKey, Tone } from '../types'

export const SCHEMA = 'vnx.providers.usage/1'
export const SEPARATOR = ' · '
/** The cells the engine spends on a status entry before our text: its indent, the glyph and the `verbruik: ` prefix. */
export const STATUS_OVERHEAD = 14
export const MIN_REFRESH_MINUTES = 2
export const DEFAULTS: Config = {
  vnxBin: '~/.vnx-system/current/bin/vnx',
  refreshMinutes: 10,
  warnPercent: 70,
  errorPercent: 90,
  warnBalance: 5,
  errorBalance: 1,
}

const PROVIDERS: readonly ProviderName[] = ['codex', 'kimi', 'deepseek', 'openrouter']
const KIMI_FULL = 'vol tot reset'
const REASON_CELLS = 32
/** From the most to the least important: the first is dropped last. The Claude 5-hour figure is not in the list: it stays. */
const DROP_ORDER: readonly SegmentKey[] = ['notice', 'openrouter', 'deepseek', 'kimi', 'codex', 'claudeWeek']
const SYMBOLS: Readonly<Record<string, string>> = { USD: '$', EUR: '€', GBP: '£' }

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

const numberOr = (value: unknown, fallback: number): number => (isNumber(value) ? value : fallback)

export function configOf(options: PluginOptions): Config {
  const bin = options['vnxBin']

  return {
    vnxBin: typeof bin === 'string' && bin.trim() !== '' ? bin.trim() : DEFAULTS.vnxBin,
    refreshMinutes: Math.max(MIN_REFRESH_MINUTES, numberOr(options['refreshMinutes'], DEFAULTS.refreshMinutes)),
    warnPercent: numberOr(options['warnPercent'], DEFAULTS.warnPercent),
    errorPercent: numberOr(options['errorPercent'], DEFAULTS.errorPercent),
    warnBalance: numberOr(options['warnBalance'], DEFAULTS.warnBalance),
    errorBalance: numberOr(options['errorBalance'], DEFAULTS.errorBalance),
  }
}

/** A used percentage: from `errorPercent` up an error, from `warnPercent` up a warning. */
export function percentTone(percent: number, config: Config): Tone {
  if (percent >= config.errorPercent) {
    return 'error'
  }

  return percent >= config.warnPercent ? 'warning' : 'success'
}

/** A balance, mirrored: at or below `errorBalance` an error, at or below `warnBalance` a warning. */
export function balanceTone(amount: number, config: Config): Tone {
  if (amount <= config.errorBalance) {
    return 'error'
  }

  return amount <= config.warnBalance ? 'warning' : 'success'
}

// ---------------------------------------------------------------------------
// Reasons: shown short, and never with a path.
// ---------------------------------------------------------------------------

const isPathLike = (word: string): boolean => /[/\\]|^~|\.jsonl$/.test(word)

/** The text without a word that is a path or a file name. */
export function withoutPaths(text: string): string {
  return text.split(/\s+/).filter(word => !isPathLike(word)).join(' ').replace(/\s+in$/, '').trim()
}

const cut = (text: string, cells: number): string => {
  const chars = [...text]

  return chars.length <= cells ? text : `${chars.slice(0, cells - 1).join('')}…`
}

/** The head of a reason, up to ` in ` or a bracket, without paths, cut to a short length. */
export function shortReason(reason: string): string {
  const head = withoutPaths((reason.split(/ in |\(/)[0] ?? '').trim())

  return head === '' ? 'niet beschikbaar' : cut(head, REASON_CELLS)
}

// ---------------------------------------------------------------------------
// Parsing the report
// ---------------------------------------------------------------------------

const reasonOf = (row: Record<string, unknown>): string =>
  typeof row['reason'] === 'string' && row['reason'].trim() !== '' ? row['reason'] : 'niet beschikbaar'

function balancesOf(row: Record<string, unknown>): Figure {
  const list = Array.isArray(row['balances']) ? row['balances'] : []
  const balances = list.flatMap(entry => {
    if (!isRecord(entry) || typeof entry['currency'] !== 'string') {
      return []
    }

    const amount = typeof entry['total'] === 'string' ? Number(entry['total']) : entry['total']

    return isNumber(amount) ? [{ currency: entry['currency'], amount }] : []
  })

  return balances.length > 0 ? { kind: 'balances', balances } : { kind: 'unavailable', reason: 'saldo onleesbaar' }
}

function figureOf(name: ProviderName, row: unknown): Figure {
  if (!isRecord(row)) {
    return { kind: 'unavailable', reason: 'onverwacht antwoord' }
  }

  if (row['available'] !== true) {
    return { kind: 'unavailable', reason: reasonOf(row) }
  }

  if (name === 'deepseek') {
    return balancesOf(row)
  }

  if (name === 'openrouter') {
    return isNumber(row['remaining'])
      ? { kind: 'balances', balances: [{ currency: 'USD', amount: row['remaining'] }] }
      : { kind: 'unavailable', reason: 'saldo onleesbaar' }
  }

  if (isNumber(row['used_percent'])) {
    return { kind: 'percent', percent: row['used_percent'] }
  }

  if (name === 'kimi' && typeof row['status'] === 'string' && row['status'].trim() !== '') {
    const latest = row['resets_at_latest']
    const status = row['status'].trim()

    return typeof latest === 'string' && Number.isFinite(Date.parse(latest))
      ? { kind: 'status', text: status, resetsAtLatest: latest }
      : { kind: 'status', text: status }
  }

  return { kind: 'unavailable', reason: 'geen cijfer in het antwoord' }
}

/** The report of a successful run, or the cause why its stdout is no usable report. */
export function parseReport(stdout: string): { isOk: true; report: Report } | { isOk: false; cause: string } {
  let body: unknown

  try {
    body = JSON.parse(stdout)
  } catch {
    return { isOk: false, cause: 'vnx gaf geen geldige JSON' }
  }

  if (!isRecord(body) || !isRecord(body['providers'])) {
    return { isOk: false, cause: 'vnx gaf geen geldige JSON' }
  }

  if (body['schema'] !== SCHEMA) {
    return { isOk: false, cause: 'ander schema' }
  }

  const providers = body['providers']
  const rows = PROVIDERS.flatMap(name => (name in providers ? [{ name, figure: figureOf(name, providers[name]) }] : []))
  const measuredAt = typeof body['measured_at'] === 'string' ? body['measured_at'] : null

  return { isOk: true, report: { measuredAt, rows } }
}

export type Run = { exitCode: number; stdout: string; stderr: string }

/** What a finished run came to. An older vnx is told by the command's own failure: argparse refusing the verb. */
export function outcomeOf(run: Run): Outcome {
  if (run.exitCode === 2 && /invalid choice/i.test(run.stderr)) {
    return { kind: 'too-old' }
  }

  if (run.exitCode !== 0) {
    return { kind: 'failed', cause: `vnx gaf exit ${run.exitCode}` }
  }

  const parsed = parseReport(run.stdout)

  return parsed.isOk ? { kind: 'ok', report: parsed.report } : { kind: 'failed', cause: parsed.cause }
}

export const NEVER_RAN: Fleet = { report: null, cause: null, isTooOld: false }

/** The state after a run: a failure keeps the last good report, an older vnx never had one. */
export function advance(previous: Fleet, outcome: Outcome): Fleet {
  if (outcome.kind === 'ok') {
    return { report: outcome.report, cause: null, isTooOld: false }
  }

  if (outcome.kind === 'too-old') {
    return { report: null, cause: null, isTooOld: true }
  }

  return { report: previous.report, cause: outcome.cause, isTooOld: false }
}

// ---------------------------------------------------------------------------
// Segments
// ---------------------------------------------------------------------------

const marked = (text: string, tone: Tone): string => {
  if (tone === 'error') {
    return `${text} !!`
  }

  return tone === 'warning' ? `${text} !` : text
}

const pad = (value: number): string => String(value).padStart(2, '0')

/** An upper bound as `dd-mm HH:MM` in local time. */
const latestText = (iso: string): string => {
  const moment = new Date(iso)

  return `${pad(moment.getDate())}-${pad(moment.getMonth() + 1)} ${pad(moment.getHours())}:${pad(moment.getMinutes())}`
}

const MINUTE_MS = 60_000
const HOUR_MS = 60 * MINUTE_MS

/** The time left until `ms` from now, rounded UP on the smallest unit shown so it never promises too early. */
const leftText = (ms: number): string => {
  const minutes = Math.ceil(ms / MINUTE_MS)

  if (minutes < 60) {
    return `${minutes}m`
  }

  if (minutes <= 24 * 60) {
    return `${Math.floor(minutes / 60)}u ${pad(minutes % 60)}m`
  }

  const hours = Math.ceil(ms / HOUR_MS)

  return `${Math.floor(hours / 24)}d ${pad(hours % 24)}u`
}

/** What a full kimi shows: the time left before the upper bound, the stored bound after it, plain text without one. */
function kimiFullText(resetsAtLatest: string | undefined, now: number): { shown: string; tone: Tone } {
  if (resetsAtLatest === undefined) {
    return { shown: KIMI_FULL, tone: 'error' }
  }

  const bound = Date.parse(resetsAtLatest)

  if (now >= bound) {
    return { shown: `laatst vol, grens ${latestText(resetsAtLatest)} voorbij`, tone: 'warning' }
  }

  return { shown: `terug over ${leftText(bound - now)} (uiterlijk ${latestText(resetsAtLatest)})`, tone: 'error' }
}

const percentText = (percent: number): string => `${Number(percent.toFixed(1))}%`

const balanceText = (balance: { currency: string; amount: number }): string => {
  const amount = balance.amount.toFixed(2)
  const symbol = SYMBOLS[balance.currency]

  return symbol === undefined ? `${amount} ${balance.currency}` : `${symbol}${amount}`
}

const WORST: readonly Tone[] = ['success', 'warning', 'error']

const worst = (tones: Tone[]): Tone => tones.reduce<Tone>((a, b) => (WORST.indexOf(b) > WORST.indexOf(a) ? b : a), 'success')

function providerSegment(name: ProviderName, figure: Figure, config: Config, isStale: boolean, now: number): Segment {
  const stale = isStale ? ' (oud)' : ''

  if (figure.kind === 'unavailable') {
    return { key: name, text: `${name} ${shortReason(figure.reason)}${stale}`, tone: 'dim' }
  }

  if (figure.kind === 'percent') {
    const tone = percentTone(figure.percent, config)

    return { key: name, text: `${marked(`${name} ${percentText(figure.percent)}`, tone)}${stale}`, tone }
  }

  if (figure.kind === 'status') {
    if (figure.text === KIMI_FULL) {
      const full = kimiFullText(figure.resetsAtLatest, now)

      return { key: name, text: `${marked(`${name} ${full.shown}`, full.tone)}${stale}`, tone: full.tone }
    }

    const tone: Tone = figure.text === 'bereikbaar' ? 'success' : 'warning'

    return { key: name, text: `${marked(`${name} ${figure.text}`, tone)}${stale}`, tone }
  }

  const tone = worst(figure.balances.map(balance => balanceTone(balance.amount, config)))
  const text = `${name} ${figure.balances.map(balanceText).join(' / ')}`

  return { key: name, text: `${marked(text, tone)}${stale}`, tone }
}

function claudeSegments(rateLimits: readonly RateLimit[], config: Config): Segment[] {
  const five = rateLimits.find(limit => limit.kind === 'five_hour')
  const week = rateLimits.find(limit => limit.kind === 'seven_day')

  if (five === undefined && week === undefined) {
    return [{ key: 'claude5', text: 'claude: nog geen meting', tone: 'dim' }]
  }

  const segments: Segment[] = []

  if (five !== undefined) {
    const tone = percentTone(five.percentUsed, config)

    segments.push({ key: 'claude5', text: marked(`claude 5u ${percentText(five.percentUsed)}`, tone), tone })
  }

  if (week !== undefined) {
    const tone = percentTone(week.percentUsed, config)
    const label = five === undefined ? 'claude week' : 'week'

    segments.push({ key: five === undefined ? 'claude5' : 'claudeWeek', text: marked(`${label} ${percentText(week.percentUsed)}`, tone), tone })
  }

  return segments
}

/** The parts of the status line, most important first. */
/** `now` is the clock reading in milliseconds; the display never reads the time itself. */
export function segmentsOf(rateLimits: readonly RateLimit[], fleet: Fleet, config: Config, now: number): Segment[] {
  const segments = claudeSegments(rateLimits, config)

  if (fleet.isTooOld) {
    return [...segments, { key: 'notice', text: 'vnx te oud voor providers usage', tone: 'dim' }]
  }

  const stale = fleet.cause !== null
  const rows = (fleet.report?.rows ?? []).map(row => providerSegment(row.name, row.figure, config, stale, now))

  if (fleet.cause !== null) {
    rows.push({ key: 'notice', text: `vnx-meting mislukt: ${fleet.cause}`, tone: 'dim' })
  }

  return [...segments, ...rows]
}

/** The line, joined, with the least important segments dropped until it fits `columns` (null: no limit). */
export function fit(segments: readonly Segment[], columns: number | null): string {
  const kept = [...segments]
  const width = () => [...kept.map(segment => segment.text).join(SEPARATOR)].length

  if (columns === null) {
    return kept.map(segment => segment.text).join(SEPARATOR)
  }

  for (const key of DROP_ORDER) {
    if (width() <= columns - STATUS_OVERHEAD) {
      break
    }

    const index = kept.findIndex(segment => segment.key === key)

    if (index >= 0) {
      kept.splice(index, 1)
    }
  }

  return kept.map(segment => segment.text).join(SEPARATOR)
}

// ---------------------------------------------------------------------------
// The /verbruik text
// ---------------------------------------------------------------------------

function figureLines(name: ProviderName, figure: Figure, config: Config, now: number): string {
  if (figure.kind === 'unavailable') {
    return `${name}: niet beschikbaar, ${withoutPaths(figure.reason) || 'geen reden'}`
  }

  return providerSegment(name, figure, config, false, now).text.replace(`${name} `, `${name}: `)
}

/** Every provider with its figures or its reason, and when the report was measured. */
export function commandText(rateLimits: readonly RateLimit[], fleet: Fleet, config: Config, now: number): string {
  const lines = ['Verbruik']
  const five = rateLimits.find(limit => limit.kind === 'five_hour')
  const week = rateLimits.find(limit => limit.kind === 'seven_day')

  if (five === undefined && week === undefined) {
    lines.push('claude: nog geen meting')
  }

  for (const [label, limit] of [['claude 5u', five], ['claude week', week]] as const) {
    if (limit !== undefined) {
      const reset = limit.resetsAt === undefined ? '' : `, reset ${limit.resetsAt}`

      lines.push(`${marked(`${label}: ${percentText(limit.percentUsed)}`, percentTone(limit.percentUsed, config))}${reset}`)
    }
  }

  if (fleet.isTooOld) {
    lines.push('vnx te oud voor providers usage: alleen Claude wordt getoond')

    return lines.join('\n')
  }

  const stale = fleet.cause !== null ? ' (oud)' : ''

  for (const row of fleet.report?.rows ?? []) {
    lines.push(`${figureLines(row.name, row.figure, config, now)}${stale}`)
  }

  if (fleet.report === null && fleet.cause === null) {
    lines.push('andere providers: nog geen meting')
  }

  if (fleet.report !== null) {
    lines.push(`gemeten: ${fleet.report.measuredAt ?? 'onbekend'}`)
  }

  if (fleet.cause !== null) {
    lines.push(`laatste meting mislukt: ${fleet.cause}`)
  }

  return lines.join('\n')
}
