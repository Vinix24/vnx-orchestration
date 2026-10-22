import { describe, expect, test } from 'claude-code/testing'

import {
  NEVER_RAN,
  STATUS_OVERHEAD,
  advance,
  balanceTone,
  commandText,
  configOf,
  fit,
  outcomeOf,
  parseReport,
  percentTone,
  segmentsOf,
  shortReason,
} from '../hooks/usage'
import type { Fleet, Report } from '../types'

import {
  CLAUDE_ROW,
  CODEX_ROW,
  CODEX_UNAVAILABLE,
  CONFIG,
  DEEPSEEK_ROW,
  DEEPSEEK_UNAVAILABLE,
  FULL_REPORT,
  KIMI_FULL,
  KIMI_REACHABLE,
  OLD_VNX,
  OPENROUTER_ROW,
  OPENROUTER_UNAVAILABLE,
  RATE_LIMITS,
  reportJson,
} from './fixtures'

const reportOf = (json: string): Report => {
  const parsed = parseReport(json)

  if (!parsed.isOk) {
    throw new Error(parsed.cause)
  }

  return parsed.report
}

const fleetOf = (json: string, cause: string | null = null): Fleet => ({ report: reportOf(json), cause, isTooOld: false })

const lineOf = (json: string, columns: number | null = null): string => fit(segmentsOf(RATE_LIMITS, fleetOf(json), CONFIG), columns)

describe('the config', () => {
  test('takes the defaults of the manifest', () => {
    expect(configOf({})).toEqual({ ...CONFIG, vnxBin: '~/.vnx-system/current/bin/vnx' })
  })

  test('takes an interval no shorter than 2 minutes', () => {
    expect(configOf({ refreshMinutes: 1 }).refreshMinutes).toBe(2)
    expect(configOf({ refreshMinutes: 2 }).refreshMinutes).toBe(2)
    expect(configOf({ refreshMinutes: 30 }).refreshMinutes).toBe(30)
  })

  test('falls back to the default for a value that is no number', () => {
    expect(configOf({ warnPercent: 'veel', errorBalance: Number.NaN }).warnPercent).toBe(70)
    expect(configOf({ errorBalance: Number.NaN }).errorBalance).toBe(1)
  })
})

describe('the provider rows of vnx providers usage', () => {
  test('shows codex as its percentage, kimi as its status and both balances in their currency', () => {
    expect(lineOf(FULL_REPORT)).toBe(
      'claude 5u 34% · week 61% · codex 58% · kimi vol tot reset !! · deepseek $12.92 · openrouter $4.91 !',
    )
  })

  test('ignores the claude row of the report: the session is the source', () => {
    const rows = reportOf(FULL_REPORT).rows.map(row => row.name)

    expect(rows).toEqual(['codex', 'kimi', 'deepseek', 'openrouter'])
  })

  test('shows kimi full with a known upper bound as "reset uiterlijk" in local time, still as an error', () => {
    const latest = new Date(2026, 9, 25, 14, 5)
    const line = lineOf(reportJson({ kimi: { ...KIMI_FULL, resets_at_latest: latest.toISOString() } }))

    expect(line).toBe('claude 5u 34% · week 61% · kimi reset uiterlijk 25-10 14:05 !!')
  })

  test('keeps "vol tot reset" when the upper bound is onbekend or unreadable', () => {
    for (const bound of ['onbekend', 'niet een datum', 7, null]) {
      expect(lineOf(reportJson({ kimi: { ...KIMI_FULL, resets_at_latest: bound } }))).toBe(
        'claude 5u 34% · week 61% · kimi vol tot reset !!',
      )
    }
  })

  test('shows a reachable kimi without the bound even when the row carries one', () => {
    const row = { ...KIMI_REACHABLE, resets_at_latest: new Date(2026, 9, 25, 14, 5).toISOString() }

    expect(lineOf(reportJson({ kimi: row }))).toBe('claude 5u 34% · week 61% · kimi bereikbaar')
  })

  test('shows kimi reachable as success, and a later kimi row with a percentage as that percentage', () => {
    expect(lineOf(reportJson({ kimi: KIMI_REACHABLE }))).toBe('claude 5u 34% · week 61% · kimi bereikbaar')
    expect(lineOf(reportJson({ kimi: { available: true, used_percent: 75, status: 'bereikbaar' } }))).toBe(
      'claude 5u 34% · week 61% · kimi 75% !',
    )
  })

  test('shows an unavailable row as the provider and a short form of its reason, dim, without the file name', () => {
    const segments = segmentsOf(
      RATE_LIMITS,
      fleetOf(reportJson({ codex: CODEX_UNAVAILABLE, deepseek: DEEPSEEK_UNAVAILABLE, openrouter: OPENROUTER_UNAVAILABLE })),
      CONFIG,
    )

    expect(segments.map(segment => segment.text)).toEqual([
      'claude 5u 34%',
      'week 61%',
      'codex geen rate_limits-regel',
      'deepseek geen DEEPSEEK_API_KEY',
      'openrouter http 402: onvoldoende saldo',
    ])
    expect(segments.slice(2).every(segment => segment.tone === 'dim')).toBe(true)
  })

  test('shows a deepseek row with several currencies, and one without a readable balance as unavailable', () => {
    const several = {
      available: true,
      is_available: true,
      balances: [
        { currency: 'USD', total: '12.92' },
        { currency: 'CNY', total: '3.5' },
      ],
    }

    expect(lineOf(reportJson({ deepseek: several }))).toBe('claude 5u 34% · week 61% · deepseek $12.92 / 3.50 CNY !')
    expect(lineOf(reportJson({ deepseek: { available: true, balances: [] } }))).toBe(
      'claude 5u 34% · week 61% · deepseek saldo onleesbaar',
    )
    expect(lineOf(reportJson({ openrouter: { available: true } }))).toBe('claude 5u 34% · week 61% · openrouter saldo onleesbaar')
  })

  test('never prints a path, whatever a reason holds', () => {
    const reason = { available: false, reason: 'kan /Users/someone/.state/x niet lezen in ~/data/y.json' }
    const text = commandText(RATE_LIMITS, fleetOf(reportJson({ codex: reason })), CONFIG) + lineOf(reportJson({ codex: reason }))

    expect(text).not.toMatch(/\/Users|~\/|\.json|\.state/)
    expect(shortReason('')).toBe('niet beschikbaar')
    expect(shortReason('x'.repeat(80)).length).toBeLessThanOrEqual(32)
  })
})

describe('a run that does not give a report', () => {
  const good = (): Fleet => fleetOf(FULL_REPORT)

  test('keeps the last good figures, marks each (oud) and names the cause once', () => {
    const fleet = advance(good(), outcomeOf({ exitCode: 1, stdout: '', stderr: 'boom' }))
    const line = fit(segmentsOf(RATE_LIMITS, fleet, CONFIG), null)

    expect(line).toBe(
      'claude 5u 34% · week 61% · codex 58% (oud) · kimi vol tot reset !! (oud) · deepseek $12.92 (oud) · openrouter $4.91 ! (oud) · vnx-meting mislukt: vnx gaf exit 1',
    )
    expect(line.split('vnx-meting mislukt').length).toBe(2)
  })

  test('treats a non-zero exit, invalid JSON, JSON that is no report and another schema each as a failure with its own cause', () => {
    expect(outcomeOf({ exitCode: 3, stdout: FULL_REPORT, stderr: '' })).toEqual({ kind: 'failed', cause: 'vnx gaf exit 3' })
    expect(outcomeOf({ exitCode: 0, stdout: 'geen json', stderr: '' })).toEqual({ kind: 'failed', cause: 'vnx gaf geen geldige JSON' })
    expect(outcomeOf({ exitCode: 0, stdout: '[]', stderr: '' })).toEqual({ kind: 'failed', cause: 'vnx gaf geen geldige JSON' })
    expect(outcomeOf({ exitCode: 0, stdout: '{"schema":"vnx.providers.usage/1"}', stderr: '' })).toEqual({
      kind: 'failed',
      cause: 'vnx gaf geen geldige JSON',
    })
    expect(outcomeOf({ exitCode: 0, stdout: reportJson({ codex: CODEX_ROW }, 'vnx.providers.usage/2'), stderr: '' })).toEqual({
      kind: 'failed',
      cause: 'ander schema',
    })
  })

  test('keeps the last good figures for invalid JSON and for another schema too', () => {
    for (const stdout of ['geen json', reportJson({ codex: CODEX_ROW }, 'vnx.providers.usage/2')]) {
      const fleet = advance(good(), outcomeOf({ exitCode: 0, stdout, stderr: '' }))

      expect(fleet.report).toEqual(good().report)
      expect(fleet.cause).not.toBeNull()
    }
  })

  test('shows a failure before any good run as the Claude part and the cause', () => {
    const fleet = advance(NEVER_RAN, outcomeOf({ exitCode: 1, stdout: '', stderr: '' }))

    expect(fit(segmentsOf(RATE_LIMITS, fleet, CONFIG), null)).toBe('claude 5u 34% · week 61% · vnx-meting mislukt: vnx gaf exit 1')
  })

  test('takes the figures back, and drops the cause, at the next good run', () => {
    const failed = advance(good(), outcomeOf({ exitCode: 1, stdout: '', stderr: '' }))
    const fleet = advance(failed, outcomeOf({ exitCode: 0, stdout: FULL_REPORT, stderr: '' }))

    expect(fleet).toEqual(good())
  })
})

describe('an older vnx without providers usage', () => {
  test('is told by the refusal of the command itself, not by a version', () => {
    expect(outcomeOf(OLD_VNX)).toEqual({ kind: 'too-old' })
    expect(outcomeOf({ exitCode: 2, stdout: '', stderr: 'iets anders' })).toEqual({ kind: 'failed', cause: 'vnx gaf exit 2' })
  })

  test('shows the Claude part and the notice, and no other provider', () => {
    const fleet = advance(fleetOf(FULL_REPORT), outcomeOf(OLD_VNX))

    expect(fit(segmentsOf(RATE_LIMITS, fleet, CONFIG), null)).toBe('claude 5u 34% · week 61% · vnx te oud voor providers usage')
    expect(segmentsOf(RATE_LIMITS, fleet, CONFIG).at(-1)?.tone).toBe('dim')
  })
})

describe('the tones', () => {
  test('puts a used percentage on its boundaries: 69 success, 70 warning, 89 warning, 90 error', () => {
    expect([69, 70, 89, 90].map(percent => percentTone(percent, CONFIG))).toEqual(['success', 'warning', 'warning', 'error'])
    expect(percentTone(69.9, CONFIG)).toBe('success')
    expect(percentTone(100, CONFIG)).toBe('error')
  })

  test('puts a balance on its boundaries: above warn success, at warn warning, at error error', () => {
    expect([5.01, 5, 1.01, 1, 0.5, 0].map(amount => balanceTone(amount, CONFIG))).toEqual([
      'success',
      'warning',
      'warning',
      'error',
      'error',
      'error',
    ])
  })

  test('follows the thresholds of the config', () => {
    const strict = { ...CONFIG, warnPercent: 50, errorPercent: 60, warnBalance: 20, errorBalance: 15 }

    expect(percentTone(55, strict)).toBe('warning')
    expect(balanceTone(12.92, strict)).toBe('error')
  })

  test('gives every segment its tone, and the text carries the meaning without it', () => {
    const segments = segmentsOf(
      [
        { kind: 'five_hour', percentUsed: 90 },
        { kind: 'seven_day', percentUsed: 70 },
      ],
      fleetOf(FULL_REPORT),
      CONFIG,
    )

    expect(segments.map(segment => [segment.key, segment.tone])).toEqual([
      ['claude5', 'error'],
      ['claudeWeek', 'warning'],
      ['codex', 'success'],
      ['kimi', 'error'],
      ['deepseek', 'success'],
      ['openrouter', 'warning'],
    ])
    expect(segments[0]?.text).toBe('claude 5u 90% !!')
    expect(segments[1]?.text).toBe('week 70% !')
  })

  test('takes the worst tone of the balances of one provider', () => {
    const several = { available: true, balances: [{ currency: 'USD', total: '12' }, { currency: 'EUR', total: '0.5' }] }

    expect(segmentsOf([], fleetOf(reportJson({ deepseek: several })), CONFIG).find(s => s.key === 'deepseek')?.tone).toBe('error')
  })
})

describe('the Claude part', () => {
  test('says nog geen meting, dim, and never a zero, when the session has no reading', () => {
    const segments = segmentsOf([], NEVER_RAN, CONFIG)

    expect(segments).toEqual([{ key: 'claude5', text: 'claude: nog geen meting', tone: 'dim' }])
  })

  test('shows only the window it has', () => {
    expect(fit(segmentsOf([{ kind: 'seven_day', percentUsed: 12 }], NEVER_RAN, CONFIG), null)).toBe('claude week 12%')
    expect(fit(segmentsOf([{ kind: 'five_hour', percentUsed: 8.25 }], NEVER_RAN, CONFIG), null)).toBe('claude 5u 8.3%')
    expect(fit(segmentsOf([{ kind: 'spend_limit', percentUsed: 8 }], NEVER_RAN, CONFIG), null)).toBe('claude: nog geen meting')
  })
})

describe('the width', () => {
  const widthOf = (text: string): number => STATUS_OVERHEAD + [...text].length
  const full = lineOf(FULL_REPORT)

  test('keeps everything where it fits, and where the width is not known', () => {
    expect(lineOf(FULL_REPORT, widthOf(full))).toBe(full)
    expect(lineOf(FULL_REPORT, null)).toBe(full)
  })

  test('drops openrouter, then deepseek, then kimi, then codex, then the weekly figure, and keeps the 5-hour figure', () => {
    const steps = [
      'claude 5u 34% · week 61% · codex 58% · kimi vol tot reset !! · deepseek $12.92',
      'claude 5u 34% · week 61% · codex 58% · kimi vol tot reset !!',
      'claude 5u 34% · week 61% · codex 58%',
      'claude 5u 34% · week 61%',
      'claude 5u 34%',
    ]

    for (const step of steps) {
      expect(lineOf(FULL_REPORT, widthOf(step))).toBe(step)
    }

    expect(lineOf(FULL_REPORT, 1)).toBe('claude 5u 34%')
  })

  test('drops the cause and the old-vnx notice before any figure', () => {
    const failed = fit(segmentsOf(RATE_LIMITS, fleetOf(FULL_REPORT, 'vnx gaf exit 1'), CONFIG), null)
    const narrower = failed.replace(' · vnx-meting mislukt: vnx gaf exit 1', '')

    expect(fit(segmentsOf(RATE_LIMITS, fleetOf(FULL_REPORT, 'vnx gaf exit 1'), CONFIG), widthOf(narrower))).toBe(narrower)
    expect(fit(segmentsOf(RATE_LIMITS, advance(NEVER_RAN, { kind: 'too-old' }), CONFIG), widthOf('claude 5u 34% · week 61%'))).toBe(
      'claude 5u 34% · week 61%',
    )
  })
})

describe('/verbruik', () => {
  test('lists every provider with its numbers or its reason, and when the report was measured', () => {
    const fleet = fleetOf(reportJson({ codex: CODEX_UNAVAILABLE, kimi: KIMI_FULL, deepseek: DEEPSEEK_ROW, openrouter: OPENROUTER_ROW }))

    expect(commandText(RATE_LIMITS, fleet, CONFIG).split('\n')).toEqual([
      'Verbruik',
      'claude 5u: 34%, reset 2026-10-21T23:00:00.000Z',
      'claude week: 61%, reset 2026-10-27T16:00:00.000Z',
      'codex: niet beschikbaar, geen rate_limits-regel',
      'kimi: vol tot reset !!',
      'deepseek: $12.92',
      'openrouter: $4.91 !',
      'gemeten: 2026-10-21T19:21:21Z',
    ])
  })

  test('says so when there is no reading, when the last run failed and when the vnx is too old', () => {
    expect(commandText([], NEVER_RAN, CONFIG).split('\n')).toEqual(['Verbruik', 'claude: nog geen meting', 'andere providers: nog geen meting'])
    expect(commandText(RATE_LIMITS, fleetOf(reportJson({ codex: CODEX_ROW }), 'ander schema'), CONFIG)).toContain('codex: 58% (oud)')
    expect(commandText(RATE_LIMITS, fleetOf(reportJson({ codex: CODEX_ROW }), 'ander schema'), CONFIG)).toContain('laatste meting mislukt: ander schema')
    expect(commandText(RATE_LIMITS, { report: null, cause: null, isTooOld: true }, CONFIG)).toContain('vnx te oud voor providers usage')
  })

  test('carries no key of a row the report had besides its figures', () => {
    const text = commandText(RATE_LIMITS, fleetOf(FULL_REPORT), CONFIG)

    expect(text).not.toMatch(/auth_401|rollout|plus|source|store/)
    expect(CLAUDE_ROW.available).toBe(false)
  })
})
