import type { On, SessionRateLimit } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { FULL_REPORT, KIMI_FULL, OLD_VNX, RATE_LIMITS, reportJson } from './fixtures'

type Answer = { exitCode: number; stdout: string; stderr: string }

type World = { statuses: (string | undefined)[]; runs: string[][]; commands: string[]; rateLimits: SessionRateLimit[]; answer: Answer | null }

const NOW = 1_760_000_000_000
const START = { surface: 'terminal', isInteractive: true, cwd: '/work/project' } as const

const OK: Answer = { exitCode: 0, stdout: FULL_REPORT, stderr: '' }

/** The engine under the mod: a vnx that answers `world.answer`, a session with `world.rateLimits`, and the status the mod sets. */
function worldOf(on: On, answer: Answer = OK): World {
  const world: World = { statuses: [], runs: [], commands: [], rateLimits: [], answer }

  on('process.run', ($, e) => {
    world.runs.push([...e.argv])

    if (world.answer === null) {
      return { deny: 'no such binary' }
    }

    return { value: { ...world.answer, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('env.get', ($, e) => ({ value: e.name === 'HOME' ? '/home/someone' : undefined }))
  on('session.usage', () => ({ value: { startedAt: NOW, context: { window: 200000 } as never, rateLimits: world.rateLimits } }))
  on('ui.status', ($, e) => {
    world.statuses.push(e.text)

    return { value: undefined }
  })
  on('command.register', ($, e) => {
    world.commands.push(e.name)

    return { value: { command: e.name } }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.measure', ($, e) => ({ changed: e.changed }))

  return world
}

const lastOf = (world: World): string | undefined => world.statuses[world.statuses.length - 1]

async function started($: Engine, on: On, answer: Answer = OK, rateLimits: SessionRateLimit[] = [...RATE_LIMITS]) {
  const world = worldOf(on, answer)
  const clock = mock.clock(on, { now: NOW })

  world.rateLimits = rateLimits
  await $.session.start(START)
  await clock.settle()

  return { world, clock }
}

describe('the mod in a session', () => {
  test('shows the Claude part at once and every provider after the first run, from the vnx binary of the config', async ($, on) => {
    const { world } = await started($, on)

    expect(world.statuses[0]).toBe('claude 5u 34% · week 61%')
    expect(lastOf(world)).toBe('claude 5u 34% · week 61% · codex 58% · kimi vol tot reset !! · deepseek $12.92 · openrouter $4.91 !')
    expect(world.runs).toEqual([['/home/someone/.vnx-system/current/bin/vnx', 'providers', 'usage', '--json']])
    expect(world.commands).toEqual(['verbruik'])
  })

  test('shows claude: nog geen meting at the start and the figures after a session.measure', async ($, on) => {
    const { world } = await started($, on, OK, [])

    expect(world.statuses[0]).toBe('claude: nog geen meting')

    await $.session.measure({
      context: {} as never,
      rateLimits: [{ kind: 'five_hour', percentUsed: 71 }],
      changed: ['rateLimits'],
    })

    expect(lastOf(world)).toContain('claude 5u 71% !')
    expect(lastOf(world)).toContain('codex 58%')
  })

  test('runs again after refreshMinutes and not before', async ($, on) => {
    const { world, clock } = await started($, on)

    await clock.advance(9 * 60_000)
    expect(world.runs.length).toBe(1)

    await clock.advance(60_000)
    await clock.settle()
    expect(world.runs.length).toBe(2)
  })

  test('keeps the last good figures and marks them (oud) when a later run fails', async ($, on) => {
    const { world, clock } = await started($, on)

    world.answer = { exitCode: 1, stdout: '', stderr: 'boom' }
    await clock.advance(10 * 60_000)
    await clock.settle()

    expect(lastOf(world)).toBe(
      'claude 5u 34% · week 61% · codex 58% (oud) · kimi vol tot reset !! (oud) · deepseek $12.92 (oud) · openrouter $4.91 ! (oud) · vnx-meting mislukt: vnx gaf exit 1',
    )
  })

  test('shows only the Claude part and the notice for an older vnx', async ($, on) => {
    const { world } = await started($, on, OLD_VNX)

    expect(lastOf(world)).toBe('claude 5u 34% · week 61% · vnx te oud voor providers usage')
  })

  test('answers /verbruik with the full figures after a refresh of its own', async ($, on) => {
    const { world } = await started($, on)

    const answer = await $.command.run({ command: 'verbruik', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 180 } })
    const text = 'text' in answer ? answer.text : ''

    expect(world.runs.length).toBe(2)
    expect(text).toContain('codex: 58%')
    expect(text).toContain('deepseek: $12.92')
    expect(text).toContain('gemeten: 2026-10-21T19:21:21Z')
  })

  test('shows a failing run at the start without a thrown error', async ($, on) => {
    const world = worldOf(on, OK)

    const clock = mock.clock(on, { now: NOW })

    world.answer = null
    world.rateLimits = [...RATE_LIMITS]
    await $.session.start(START)
    await clock.settle()

    expect(lastOf(world)).toBe('claude 5u 34% · week 61% · vnx-meting mislukt: vnx niet uitgevoerd')
  })

  test('turns the kimi countdown into the stored bound when the clock passes it, on the next refresh', async ($, on) => {
    const bound = new Date(NOW + 30 * 60_000)
    const at = `${String(bound.getDate()).padStart(2, '0')}-${String(bound.getMonth() + 1).padStart(2, '0')} ${String(bound.getHours()).padStart(2, '0')}:${String(bound.getMinutes()).padStart(2, '0')}`
    const answer: Answer = {
      exitCode: 0,
      stdout: reportJson({ kimi: { ...KIMI_FULL, resets_at_latest: bound.toISOString() } }),
      stderr: '',
    }
    const { world, clock } = await started($, on, answer, [])

    expect(lastOf(world)).toBe(`claude: nog geen meting · kimi terug over 30m (uiterlijk ${at}) !!`)

    await clock.advance(10 * 60_000)
    await clock.settle()
    expect(lastOf(world)).toBe(`claude: nog geen meting · kimi terug over 20m (uiterlijk ${at}) !!`)

    await clock.advance(20 * 60_000)
    await clock.settle()
    expect(lastOf(world)).toBe(`claude: nog geen meting · kimi laatst vol, grens ${at} voorbij !`)
  })
})
