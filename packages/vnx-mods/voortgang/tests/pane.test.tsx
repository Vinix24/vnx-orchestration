import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import {
  BIN,
  CONTENT,
  NOW,
  PANE,
  ROOT,
  PANE_PROPS,
  SESSION,
  command,
  healthyFiles,
  healthyRun,
  indexJson,
  indexOf,
  stateJson,
  stateOf,
  storeOf,
  worldOf,
} from './fixtures'
import type { WorldSetup } from './fixtures'

const SURFACES = ['terminal', 'mobile'] as const

type Drawn = { findAll: (query: { type: string }) => Promise<{ text: string }[]>; unmount: () => Promise<void> }

/** The rows a mounted pane shows, one per Text, in drawing order. */
const linesOf = async (ui: Drawn): Promise<string[]> => (await ui.findAll({ type: 'Text' })).map(found => found.text)

/** Starts a session over `setup` and lets its first refresh finish. */
async function started($: Engine, on: On, setup: WorldSetup) {
  const world = worldOf(on, setup)
  const clock = mock.clock(on, { now: NOW })
  await $.session.start(SESSION)
  await clock.settle()

  return { world, clock }
}

describe('the pane', () => {
  test('shows a healthy project: counts, PR seats, now-tracks and the proposed count', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a') })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)
    const text = lines.join('\n')

    expect(lines).toContain('project-a · index 3 min')
    expect(lines).toContain('  werk: 1 live · 0 start · 4 stale · 0 ongemeten')
    expect(lines).toContain('  live: d-live-1')
    expect(lines).toContain('  uitkomsten: 2 open (investigate 2)')
    expect(lines).toContain('  PR\'s: 1 open · state 3 min')
    expect(lines).toContain('  #2063 · ci pass · blocked · feat: voortgang')
    expect(lines).toContain('    geslaagd: codex_gate · open: kimi_gate')
    expect(lines).toContain('  tracks: 2 now · 2 next · 3 later · 1 zonder horizon')
    expect(lines).toContain('  P1 ci-profile · queued → in_progress · 2 proposed · 1 failed · 3 done')
    expect(lines).toContain('  P2 docs · active · 1 ready')
    expect(lines).toContain('  te promoveren: 2 now · 3 totaal')
    expect(text).not.toContain('onbeschikbaar')
    expect(text).not.toContain(CONTENT)
    await ui.unmount()
  })

  test('shows live work that is unavailable as onbeschikbaar with its reason, never as 0 live', async ($, on) => {
    const files = {
      [indexOf('project-a')]: indexJson({ liveWork: { available: false, reason: 'lease store unreadable' } }),
      [stateOf('project-a')]: stateJson(),
    }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  werk: onbeschikbaar: lease store unreadable')
    expect(lines.some(line => /\b0 live\b/.test(line))).toBe(false)
    expect(lines).toContain('  uitkomsten: 2 open (investigate 2)')
    expect(world.statuses.at(-1)).toBe('voortgang · ? live · 1 PR · 2 open · 1 onleesbaar')
    await ui.unmount()
  })

  test('lists a data root child without a store nowhere, and touches nothing of it', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a'), dirs: [`${ROOT}/project-c`, `${ROOT}/project-c/state`] })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines.some(line => line.startsWith('project-c'))).toBe(false)
    expect(lines).toContain('project-a · index 3 min')
    expect(world.runs.some(one => one.argv.includes('project-c'))).toBe(false)
    expect(world.reads.some(path => path.includes('/project-c/'))).toBe(false)
    await ui.unmount()
  })

  test('shows a store whose files are broken as onbeschikbaar, and the other store still', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-b')]: '{"broken', [stateOf('project-b')]: '{"broken' }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('project-a · index 3 min')
    expect(lines.some(line => line.startsWith('project-b'))).toBe(true)
    expect(lines).toContain('  werk: 1 live · 0 start · 4 stale · 0 ongemeten')
    await ui.unmount()
  })

  test('shows onbeschikbaar for the whole pane when the data root cannot be listed', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a'), unlistable: 'EACCES: permission denied' })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

    const lines = await linesOf(ui)

    expect(lines.some(line => /^onbeschikbaar: .*EACCES: permission denied/.test(line))).toBe(true)
    expect(lines.some(line => line.startsWith('project-a'))).toBe(false)
    expect(world.reads).toEqual([])
    expect(world.runs).toEqual([])
    expect(world.statuses.at(-1)).toMatch(/^voortgang · onbeschikbaar: .*EACCES: permission denied/)
    await ui.unmount()
  })

  test('shows tracks and deliverables onbeschikbaar when vnx exits 1 or answers no JSON, the index part still shown', async ($, on) => {
    const run = (argv: readonly string[]) =>
      argv[1] === 'objective' ? { exitCode: 1, stdout: '' } : { exitCode: 0, stdout: 'Traceback (most recent call last):' }
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  tracks: onbeschikbaar: vnx exit 1')
    expect(lines).toContain('  leveringen: onbeschikbaar: vnx gaf ongeldige JSON')
    expect(lines).toContain('  werk: 1 live · 0 start · 4 stale · 0 ongemeten')
    expect(lines).toContain('  #2063 · ci pass · blocked · feat: voortgang')
    await ui.unmount()
  })

  test('marks a two-hour-old index verouderd next to the live and outcome numbers', async ($, on) => {
    const files = { [indexOf('project-a')]: indexJson({ ageMinutes: 120 }), [stateOf('project-a')]: stateJson() }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('project-a · index 2 u')
    expect(lines).toContain('  werk: 1 live · 0 start · 4 stale · 0 ongemeten · verouderd')
    expect(lines).toContain('  uitkomsten: 2 open (investigate 2) · verouderd')
    expect(world.statuses.at(-1)).toBe('voortgang · 1 live · 1 PR · 2 open · 1 verouderd')
    await ui.unmount()
  })

  test('mounts and validates on the terminal and on mobile, and ververs refreshes', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a') })

    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ ...PANE, surface })
      const button = await ui.find({ type: 'Button', key: 'ververs' })

      expect(button?.props.label, surface).toBe('ververs')
      expect(button?.props.hotkey, surface).toBe('r')
      expect(await linesOf(ui), surface).toContain('project-a · index 3 min')

      const runs = world.runs.length
      const reads = world.reads.length
      await ui.press({ key: 'ververs' })

      expect(world.runs.length - runs, surface).toBe(2)
      expect(world.reads.length - reads, surface).toBe(2)
      await ui.unmount()
    }
  })

  test('fits its rows to a narrow pane and cuts a long list with +N meer', async ($, on) => {
    const many = Array.from({ length: 12 }, (_, n) => ({ track_id: `track-${n}`, priority: 'P1', phase: 'queued', derived_status: 'queued', horizon: 'now' }))
    const run = (argv: readonly string[]) =>
      argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(many) } : healthyRun(argv)
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'mobile', props: { ...PANE_PROPS, bodyColumns: 30, scroll: { offset: 0, bodyRows: 12 } } })
    const lines = await linesOf(ui)

    expect(lines.every(line => Array.from(line).length <= 30)).toBe(true)
    expect(lines.some(line => /^ {2}\+\d+ meer$/.test(line))).toBe(true)
    await ui.unmount()
  })
})

describe('the session', () => {
  test('registers /voortgang, refreshes once and sets the status line, without opening the pane', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a') })

    expect(world.commands).toEqual(['voortgang'])
    expect(world.opened).toEqual([])
    expect(world.runs).toHaveLength(2)
    expect(world.lists).toEqual([ROOT])
    expect(world.statuses.at(-1)).toBe('voortgang · 1 live · 1 PR · 2 open')
  })

  test('refreshes again every refresh_seconds', { options: { refresh_seconds: 30 } }, async ($, on) => {
    const { world, clock } = await started($, on, { files: healthyFiles('project-a') })

    expect(world.runs).toHaveLength(2)
    await clock.advance(29_000)
    expect(world.runs).toHaveLength(2)
    await clock.advance(1_000)
    expect(world.runs).toHaveLength(4)
  })

  test('/voortgang refreshes, opens the pane and answers a short text', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a') })
    const answer = await $.command.run(command('voortgang'))

    expect(world.runs).toHaveLength(4)
    expect(world.opened).toEqual([{ id: 'voortgang', title: 'Voortgang' }])
    expect(answer.text).toBe('Voortgang geopend: 1 live · 1 PR · 2 open')
  })

  test('/voortgang answers the summary lines where no surface places the pane', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), unplaced: 'no surface attached' })
    const answer = await $.command.run(command('voortgang'))

    expect(answer.text).toBe(
      [
        'Voortgang (paneel niet getoond: no surface attached)',
        'project-a: 1 live · 1 PR · 2 open · now ci-profile, docs · next 2 · later 3 · 3 te promoveren',
      ].join('\n'),
    )
  })

  test('shows onbeschikbaar and reads nothing when HOME is unknown', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a'), env: {} })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

    expect(await linesOf(ui)).toContain('onbeschikbaar: HOME onbekend, ~ niet op te lossen')
    expect(world.reads).toEqual([])
    expect(world.runs).toEqual([])
    expect(world.statuses.at(-1)).toBe('voortgang · onbeschikbaar: HOME onbekend, ~ niet op te lossen')
    await ui.unmount()
  })
})

describe('the content boundary', () => {
  test('a full refresh reads the data root once, the two files per store, and runs only the two vnx shapes', async ($, on) => {
    const files = { ...healthyFiles('project-a'), ...healthyFiles('project-b') }
    const { world } = await started($, on, { files, dirs: [`${ROOT}/project-c`] })
    const sorted = (list: string[]) => [...list].sort()
    const argvOf = (verb: string, project: string) => [BIN, verb, 'list', '--json', '--project-id', project, '--state-dir', storeOf(project)]

    expect(world.lists).toEqual([ROOT])
    expect(sorted(world.reads)).toEqual(sorted([indexOf('project-a'), stateOf('project-a'), indexOf('project-b'), stateOf('project-b')]))
    expect(sorted(world.exists)).toEqual(
      sorted([
        indexOf('project-a'),
        indexOf('project-b'),
        indexOf('project-c'),
        stateOf('project-c'),
        storeOf('project-a'),
        indexOf('project-a'),
        stateOf('project-a'),
        storeOf('project-b'),
        indexOf('project-b'),
        stateOf('project-b'),
      ]),
    )
    expect(sorted(world.runs.map(one => JSON.stringify(one.argv)))).toEqual(
      sorted([
        JSON.stringify(argvOf('objective', 'project-a')),
        JSON.stringify(argvOf('deliverable', 'project-a')),
        JSON.stringify(argvOf('objective', 'project-b')),
        JSON.stringify(argvOf('deliverable', 'project-b')),
      ]),
    )
    expect(world.runs.every(one => one.cwd === storeOf(one.argv[5] ?? '') && one.timeoutMs === 10_000)).toBe(true)
    expect(world.forbidden).toEqual([])
  })
})
