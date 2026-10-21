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
  SPEC,
  SESSION,
  command,
  dbOf,
  healthyFiles,
  healthyRun,
  indexJson,
  indexOf,
  instructionOf,
  instructionText,
  producerLiveWork,
  specOf,
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
    expect(world.statuses.at(-1)).toBe('voortgang · ? live · 1 PR · 2 open · ? bouwers · 1 onleesbaar')
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
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ ageMinutes: 120 }) }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('project-a · index 2 u')
    expect(lines).toContain('  werk: 1 live · 0 start · 4 stale · 0 ongemeten · verouderd')
    expect(lines).toContain('  uitkomsten: 2 open (investigate 2) · verouderd')
    expect(world.statuses.at(-1)).toBe('voortgang · 1 live · 1 PR · 2 open · 1 bouwers · 1 verouderd')
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
      expect(world.reads.length - reads, surface).toBe(4)
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
    expect(world.statuses.at(-1)).toBe('voortgang · 1 live · 1 PR · 2 open · 1 bouwers')
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
    expect(answer.text).toBe('Voortgang geopend: 1 live · 1 PR · 2 open · 1 bouwers')
  })

  test('/voortgang answers the summary lines where no surface places the pane', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), unplaced: 'no surface attached' })
    const answer = await $.command.run(command('voortgang'))

    expect(answer.text).toBe(
      [
        'Voortgang (paneel niet getoond: no surface attached)',
        'project-a: 1 live · 1 PR · 2 open · now ci-profile, docs · next 2 · later 3 · 1 bouwers · 3 te promoveren',
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
    expect(sorted(world.reads)).toEqual(
      sorted([
        indexOf('project-a'),
        stateOf('project-a'),
        specOf('project-a', 'd-live-1'),
        instructionOf('project-a', 'd-live-1'),
        indexOf('project-b'),
        stateOf('project-b'),
        specOf('project-b', 'd-live-1'),
        instructionOf('project-b', 'd-live-1'),
      ]),
    )
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
        dbOf('project-a'),
        dbOf('project-b'),
        specOf('project-a', 'd-live-1'),
        instructionOf('project-a', 'd-live-1'),
        specOf('project-b', 'd-live-1'),
        instructionOf('project-b', 'd-live-1'),
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

describe('the block Bouwers', () => {
  const RUNNING = {
    available: true,
    counts: { live: 1, starting: 1, stale: 1, unmeasured: 1 },
    live: [{ dispatch_id: 'd-live-1', state: 'running', age_seconds: 725, pr: null }],
    starting: [{ dispatch_id: 'd-start-1', state: 'starting', age_seconds: 59 }],
    stale: [{ dispatch_id: 'd-stale-1', state: 'stale', age_seconds: 9000, lock: 'x' }],
    unmeasured: [{ dispatch_id: 'd-unmeasured-1', state: 'unmeasured' }],
  }
  const withRunning = (project: string): Record<string, string> => ({
    ...healthyFiles(project),
    [indexOf(project)]: indexJson({ liveWork: RUNNING }),
    [specOf(project, 'd-start-1')]: JSON.stringify({ ...SPEC, role: 'reviewer', model: 'opus' }),
    [instructionOf(project, 'd-start-1')]: instructionText('Invented task: review the block'),
    [specOf(project, 'd-stale-1')]: JSON.stringify({ ...SPEC, role: 'STALEBOUWER' }),
    [instructionOf(project, 'd-stale-1')]: instructionText('STALEBOUWER'),
    [specOf(project, 'd-unmeasured-1')]: JSON.stringify({ ...SPEC, role: 'STALEBOUWER' }),
    [instructionOf(project, 'd-unmeasured-1')]: instructionText('STALEBOUWER'),
  })

  test('lists a live builder with its role and model, its first # line and its minutes, nothing else of the instruction', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a') })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  bouwers: 1')
    expect(lines).toContain('  bouwer: backend-developer · sonnet · 12 min bezig')
    expect(lines).toContain('    taak: Invented task: add the block')
    expect(lines.join('\n')).not.toContain(CONTENT)
    expect(lines.join('\n')).not.toContain('Not a title')
    await ui.unmount()
  })

  test('takes live and starting entries, leaves out stale and unmeasured ones, counts them in the status line and reads nothing of them', async ($, on) => {
    const { world } = await started($, on, { files: withRunning('project-a') })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  bouwers: 2')
    expect(lines).toContain('  bouwer: backend-developer · sonnet · 12 min bezig')
    expect(lines).toContain('  bouwer: reviewer · opus · 0 min bezig')
    expect(lines).toContain('    taak: Invented task: review the block')
    expect(lines.join('\n')).not.toContain('STALEBOUWER')
    expect(world.reads.some(path => path.includes('d-stale-1') || path.includes('d-unmeasured-1'))).toBe(false)
    expect(world.statuses.at(-1)).toBe('voortgang · 1 live · 1 PR · 2 open · 2 bouwers')
    await ui.unmount()
  })

  test('shows the total from the counts and one line for the builders the capped index does not list', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: producerLiveWork(9, 0) }) }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE_PROPS, scroll: { offset: 0, bodyRows: 60 } } })
    const lines = await linesOf(ui)

    expect(lines).toContain('  bouwers: 9')
    expect(lines.filter(line => line.startsWith('  bouwer:')).length).toBe(8)
    expect(lines).toContain('  +1 niet getoond (index toont er max. 8)')
    expect(world.statuses.at(-1)).toContain('9 bouwers')
    await ui.unmount()
  })

  test('shows onbeschikbaar for the one field of the one builder that cannot be read', async ($, on) => {
    const files = withRunning('project-a')
    delete files[specOf('project-a', 'd-live-1')]
    files[instructionOf('project-a', 'd-start-1')] = 'no title here\nonly body\n'
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  bouwer: onbeschikbaar: dispatch-spec.json ontbreekt · 12 min bezig')
    expect(lines).toContain('    taak: Invented task: add the block')
    expect(lines).toContain('  bouwer: reviewer · opus · 0 min bezig')
    expect(lines).toContain('    taak: onbeschikbaar: geen titelregel')
    await ui.unmount()
  })

  test('shows a dispatch id that names a path as an invalid id and reads nothing for it', async ($, on) => {
    const live = { ...RUNNING, counts: { live: 1, starting: 0, stale: 0, unmeasured: 0 }, live: [{ dispatch_id: '../../state', age_seconds: 60 }], starting: [] }
    const { world } = await started($, on, { files: { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: live }) } })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  bouwer: onbeschikbaar: ongeldige dispatch-id · 1 min bezig')
    expect(world.reads.some(path => path.includes('..'))).toBe(false)
    await ui.unmount()
  })

  test('shows no builders at all when the live work cannot be read, never an empty list', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: { available: false, reason: 'lease store unreadable' } }) }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  bouwers: onbeschikbaar: lease store unreadable')
    expect(lines.some(line => line.startsWith('  bouwer:'))).toBe(false)
    await ui.unmount()
  })

  test('cuts a wide title to the pane width in display cells', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [instructionOf('project-a', 'd-live-1')]: instructionText('日本語'.repeat(20)) }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'mobile', props: { ...PANE_PROPS, bodyColumns: 30 } })
    const lines = await linesOf(ui)
    const task = lines.find(line => line.startsWith('    taak: '))

    expect(task?.endsWith('…')).toBe(true)
    expect(lines.every(line => Array.from(line).reduce((sum, char) => sum + ((char.codePointAt(0) ?? 0) >= 0x2e80 ? 2 : 1), 0) <= 30)).toBe(true)
    await ui.unmount()
  })

  test('divides the rows over the builders, the PRs and the tracks, and keeps a row for each +N meer', async ($, on) => {
    const many = Array.from({ length: 12 }, (_, n) => ({ track_id: `track-${n}`, priority: 'P1', phase: 'queued', derived_status: 'queued', horizon: 'now' }))
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(many) } : healthyRun(argv))
    const files = withRunning('project-a')
    const prs = { ...JSON.parse(stateJson()).pr_queue }
    const manyPrs = { ...prs, open_prs: [prs.open_prs[0], { ...prs.open_prs[0], number: 2064 }, { ...prs.open_prs[0], number: 2065 }] }
    files[stateOf('project-a')] = stateJson({ prQueue: manyPrs })
    await started($, on, { files, run })

    for (const bodyRows of [20, 30]) {
      const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE_PROPS, scroll: { offset: 0, bodyRows } } })
      const lines = await linesOf(ui)

      // The header is the first Text, so the header and the body share the pane's rows.
      expect(lines.length, `rows ${bodyRows}`).toBeLessThanOrEqual(bodyRows)
      expect(lines.some(line => line.startsWith('  bouwer:')), `rows ${bodyRows}`).toBe(true)
      expect(lines.some(line => line.startsWith('  #206')), `rows ${bodyRows}`).toBe(true)
      expect(lines.some(line => /^ {2}P1 track-/.test(line)), `rows ${bodyRows}`).toBe(true)
      await ui.unmount()
    }
  })

  test('shows no more rows than the pane has when one PR takes two rows and twelve tracks wait', async ($, on) => {
    const many = Array.from({ length: 12 }, (_, n) => ({ track_id: `track-${n}`, priority: 'P1', phase: 'queued', derived_status: 'queued', horizon: 'now' }))
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(many) } : healthyRun(argv))
    const idle = { available: true, counts: { live: 0, starting: 0, stale: 0, unmeasured: 0 }, live: [], stale: [], unmeasured: [] }
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: idle }) }
    await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE_PROPS, scroll: { offset: 0, bodyRows: 12 } } })
    const lines = await linesOf(ui)

    expect(lines.length).toBeLessThanOrEqual(12)
    await ui.unmount()
  })
})

describe('a store without runtime_coordination.db', () => {
  test('runs no vnx command for it, creates no file in it and shows tracks and deliverables onbeschikbaar', async ($, on) => {
    const files = healthyFiles('project-a')
    delete files[dbOf('project-a')]
    const before = Object.keys(files).sort()
    // What `vnx objective list` and `vnx deliverable list` do on such a store: they create the database.
    const run = (argv: readonly string[]) => {
      files[dbOf('project-a')] = ''

      return healthyRun(argv)
    }
    const { world } = await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(world.runs).toEqual([])
    expect(Object.keys(files).sort()).toEqual(before)
    expect(world.forbidden).toEqual([])
    expect(lines).toContain('  tracks: onbeschikbaar: runtime_coordination.db ontbreekt')
    expect(lines).toContain('  leveringen: onbeschikbaar: runtime_coordination.db ontbreekt')
    expect(lines).toContain('  werk: 1 live · 0 start · 4 stale · 0 ongemeten')
    expect(world.statuses.at(-1)).toBe('voortgang · 1 live · 1 PR · 2 open · 1 bouwers · 1 onleesbaar')
    await ui.unmount()
  })
})

describe('the header and the empty state', () => {
  test('says the count is unknown when discovery failed, instead of 0 projecten', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), unlistable: 'EACCES: permission denied' })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines[0]).toBe('Voortgang · aantal onbekend')
    expect(lines.join('\n')).not.toContain('0 projecten')
    await ui.unmount()
  })

  test('points to the options that exist when no store is found', async ($, on) => {
    await started($, on, { files: {} })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const text = (await linesOf(ui)).join('\n')

    expect(text).toContain('opties exclude, data_root')
    expect(text).not.toContain('projects')
    await ui.unmount()
  })
})

describe('a refresh that rejects', () => {
  const DENY = 'env store unreachable'

  test('at the start of a session is shown in the status line and the pane, and rejects nothing', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a'), envDeny: DENY })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(world.statuses.at(-1)).toMatch(new RegExp(`^voortgang · verversen mislukt: .*${DENY}`))
    expect(lines.some(line => line.startsWith('verversen mislukt: ') && line.includes(DENY))).toBe(true)
    await ui.unmount()
  })

  test('on the timer is shown, and the next refresh runs again', { options: { refresh_seconds: 30 } }, async ($, on) => {
    const { world, clock } = await started($, on, { files: healthyFiles('project-a'), envDeny: DENY })
    const shown = world.statuses.length

    await clock.advance(30_000)

    expect(world.statuses.length).toBeGreaterThan(shown)
    expect(world.statuses.at(-1)).toMatch(new RegExp(`verversen mislukt: .*${DENY}`))
  })

  test('on the button is shown', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a'), envDeny: DENY })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const shown = world.statuses.length

    await ui.press({ key: 'ververs' })

    expect(world.statuses.length).toBeGreaterThan(shown)
    expect(world.statuses.at(-1)).toMatch(new RegExp(`verversen mislukt: .*${DENY}`))
    await ui.unmount()
  })

  test('on /voortgang is answered with the reason', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), envDeny: DENY })
    const answer = await $.command.run(command('voortgang'))

    expect(answer.text).toMatch(new RegExp(`^Voortgang: verversen mislukt \\(.*${DENY}.*\\), het paneel is niet geopend\\.$`))
  })
})
