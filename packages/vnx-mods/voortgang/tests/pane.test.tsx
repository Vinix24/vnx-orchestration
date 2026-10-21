import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { cellsOf } from '../hooks/view'

import {
  BIN,
  CONTENT,
  CWD,
  NOW,
  PANE,
  PANE_PROPS,
  PROJECT_DIR,
  ROOT,
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
  projectIdFileOf,
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
async function started($: Engine, on: On, setup: WorldSetup, cwd: string = CWD) {
  const world = worldOf(on, setup)
  const clock = mock.clock(on, { now: NOW })
  await $.session.start({ ...SESSION, cwd })
  await clock.settle()

  return { world, clock }
}

const sizeOf = (rows: number, columns = 72) => ({ ...PANE_PROPS, bodyColumns: columns, scroll: { offset: 0, bodyRows: rows } })

describe('the pane', () => {
  test('shows the one project of the session, section by section, in order', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a') })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toEqual([
      'project-a · index 3 min',
      'Nu bezig',
      '  sonnet · 12 min · Invented task: add the block · bld · PR #2150',
      'Wacht op een besluit',
      '  3 leveringen te promoveren · 2 open uitkomsten',
      'Tracks',
      '  P1 kosten',
      "     geen leveringen gepland · PR's: #2063 open",
      '  P2 bld · bezig',
      '     geen leveringen gepland · 1 bouwer',
      '  P2 viaref',
      "     geen leveringen gepland · PR's: #2063 open",
      '  P3 fresh · 1 van 1 klaar ▓▓▓▓▓▓▓▓',
      '  P1 ci-profile · 5 van 8 klaar ▓▓▓▓▓░░░',
      "     PR's: #2001 #2002 dicht",
      '  P2 docs · 0 van 1 klaar ░░░░░░░░',
      "PR's",
      '  #2063 · ci groen · geslaagd: codex_gate · open: kimi_gate',
      'opruimen: 4 stale rijen',
    ])
    expect(lines.join('\n')).not.toContain(CONTENT)
    expect(lines.join('\n')).not.toContain('verouderd')
    await ui.unmount()
  })

  test('shows no other project, and none of the counters the first version printed', async ($, on) => {
    const files = { ...healthyFiles('project-a'), ...Object.fromEntries(Object.entries(healthyFiles('project-b')).filter(([path]) => path.startsWith(ROOT))) }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const text = (await linesOf(ui)).join('\n')

    expect(text).not.toContain('project-b')

    for (const gone of ['werk:', 'live:', 'besluit nodig', 'zonder horizon', 'next', 'later', 'ongemeten', 'uitkomsten:']) {
      expect(text, gone).not.toContain(gone)
    }

    await ui.unmount()
  })

  test('says there is no project, and names the cwd, when no directory above it has a .vnx-project-id', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a') }, '/elsewhere/deep')
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })

    expect(await linesOf(ui)).toEqual(['Voortgang', 'geen VNX-project in deze map (geen .vnx-project-id boven /elsewhere/deep)'])
    expect(world.statuses.at(-1)).toBe('')
    expect(world.reads).toEqual([])
    expect(world.runs).toEqual([])
    await ui.unmount()
  })

  for (const cwd of [PROJECT_DIR, `${PROJECT_DIR}/src`]) {
    test(`finds the project from ${cwd}`, async ($, on) => {
      const { world } = await started($, on, { files: healthyFiles('project-a') }, cwd)

      expect(world.statuses.at(-1)).toBe('project-a · kosten: geen plan · bld: geen plan · 1 bouwer')
    })
  }

  test('says Nu bezig: niets and Wacht op een besluit: niets when there is none, and no opruimen row at zero', async ($, on) => {
    const files = {
      ...healthyFiles('project-a'),
      [indexOf('project-a')]: indexJson({ liveWork: producerLiveWork(0, 0), outcomes: { available: true, total: 0, items: [] } }),
    }
    const run = (argv: readonly string[]) => (argv[1] === 'deliverable' ? { exitCode: 0, stdout: '[]' } : healthyRun(argv))
    await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('Nu bezig: niets')
    expect(lines).toContain('Wacht op een besluit: niets')
    expect(lines.some(line => line.startsWith('opruimen'))).toBe(false)
    await ui.unmount()
  })

  test('puts the stale and the unmeasured entries on one dim row at the bottom', async ($, on) => {
    const live = { ...producerLiveWork(0, 0), counts: { live: 0, starting: 0, stale: 1, unmeasured: 2 } }
    await started($, on, { files: { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: live }) } })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

    expect((await linesOf(ui)).at(-1)).toBe('opruimen: 1 stale rij · 2 ongemeten')
    await ui.unmount()
  })

  test('shows live work that is unavailable as onbeschikbaar with its reason, never as no builders', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: { available: false, reason: 'lease store unreadable' } }) }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines.slice(0, 3)).toEqual(['project-a · index 3 min', 'Nu bezig', '  onbeschikbaar: lease store unreadable'])
    expect(lines).not.toContain('Nu bezig: niets')
    expect(world.statuses.at(-1)).toBe('project-a · viaref: geen plan · fresh ▓▓▓▓▓▓▓▓ 1/1 · onleesbaar')
    await ui.unmount()
  })

  test('shows an unreadable PR queue with its reason on one row', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [stateOf('project-a')]: stateJson({ prQueue: { available: false, reason: "FileNotFoundError: 'gh'" } }) }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain("PR's")
    expect(lines).toContain("  onbeschikbaar: FileNotFoundError: 'gh'")
    await ui.unmount()
  })

  test('shows the PRs of a track as #N ? and says why once, when neither the state nor the index can be read', async ($, on) => {
    const files = {
      ...healthyFiles('project-a'),
      [stateOf('project-a')]: stateJson({ prQueue: { available: false, reason: "FileNotFoundError: 'gh'" } }),
      [indexOf('project-a')]: indexJson({ liveWork: { available: false, reason: 'index stuk' } }),
    }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)
    const text = lines.join('\n')

    expect(text).not.toContain('dicht')
    expect(lines.some(line => line.startsWith("     PR's: #2001 ? #2002 ? · PR-status onbekend: "))).toBe(true)
    expect(lines.some(line => line.includes('  P2 viaref'))).toBe(true)
    expect(lines.filter(line => line.includes('PR-status onbekend')).length).toBe(2)
    await ui.unmount()
  })

  test('shows a PR as open when the state is unreadable but the index lists it', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [stateOf('project-a')]: stateJson({ prQueue: { available: false, reason: 'gh weg' } }) }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain("     geen leveringen gepland · PR's: #2063 open")
    // The index list only proves a PR open: the ones it does not list are unknown while the PR queue cannot be read, never closed.
    expect(lines).toContain("     PR's: #2001 ? #2002 ? · PR-status onbekend: gh weg")
    expect(lines.filter(line => line.includes('PR-status onbekend'))).toHaveLength(1)
    expect(lines.join('\n')).not.toContain('dicht')
    await ui.unmount()
  })

  test('starts the second row of every track at the same column, also for a track without priority', async ($, on) => {
    const noPriority = { track_id: 'zonder-prio', title: CONTENT, phase: 'queued', derived_status: 'queued', horizon: 'now', priority: null, pr_ref: null }
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify([noPriority, { ...noPriority, track_id: 'met-prio', priority: 'P1' }]) } : healthyRun(argv))
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)
    const at = lines.findIndex(line => line === '  - zonder-prio')
    const withPriority = lines.findIndex(line => line === '  P1 met-prio')

    expect(at).toBeGreaterThanOrEqual(0)
    expect(withPriority).toBeGreaterThanOrEqual(0)
    expect(lines[at + 1]).toBe('     geen leveringen gepland')
    expect(lines[withPriority + 1]).toBe('     geen leveringen gepland')
    await ui.unmount()
  })

  test('shows tracks and deliverables onbeschikbaar when vnx exits 1 or answers no JSON, the rest still shown', async ($, on) => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 1, stdout: '' } : { exitCode: 0, stdout: 'x' })
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })
    const lines = await linesOf(ui)

    expect(lines).toContain('  onbeschikbaar: vnx exit 1')
    expect(lines).toContain('  te promoveren onbeschikbaar: vnx gaf ongeldige JSON · 2 open uitkomsten')
    expect(lines).toContain('  #2063 · ci groen · geslaagd: codex_gate · open: kimi_gate')
    await ui.unmount()
  })

  test('shows a track whose deliverables could not be read as such, not as a track without leveringen', async ($, on) => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? healthyRun(argv) : { exitCode: 3, stdout: '' })
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('     leveringen onbeschikbaar: vnx exit 3')
    expect(lines.some(line => line.includes('geen leveringen gepland'))).toBe(false)
    await ui.unmount()
  })

  test('keeps the open PRs and the builders of a track on its second row when its deliverables could not be read', async ($, on) => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? healthyRun(argv) : { exitCode: 3, stdout: '' })
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })
    const lines = await linesOf(ui)

    expect(lines).toContain("     leveringen onbeschikbaar: vnx exit 3 · PR's: #2063 open")
    expect(lines).toContain('     leveringen onbeschikbaar: vnx exit 3 · 1 bouwer')
    expect(lines).toContain('  leveringen onleesbaar: vnx exit 3, tracks met alleen recente leveringen ontbreken')
    await ui.unmount()
  })

  test('shows a builder whose spec has no model as model ? with its track, and not as unreadable', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [specOf('project-a', 'd-live-1')]: JSON.stringify({ track_id: 'bld', pr_id: '#2150' }) }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain('  model ? · 12 min · Invented task: add the block · bld · PR #2150')
    expect(world.statuses.at(-1)).not.toContain('onleesbaar')
    await ui.unmount()
  })

  test('shows verouderd once, on the header, for an index or state older than 60 minutes', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ ageMinutes: 120 }), [stateOf('project-a')]: stateJson({ ageMinutes: 120 }) }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines[0]).toBe('project-a · index 2 u · verouderd')
    expect(lines.filter(line => line.includes('verouderd'))).toHaveLength(1)
    expect(world.statuses.at(-1)).toBe('project-a · kosten: geen plan · bld: geen plan · 1 bouwer · verouderd')
    await ui.unmount()
  })

  test('shows geen store in the header, and nothing else, when the id names a store that is not there', async ($, on) => {
    const { world } = await started($, on, { files: { [projectIdFileOf(PROJECT_DIR)]: 'project-z\n' } })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

    expect(await linesOf(ui)).toEqual(['project-z · geen store'])
    expect(world.statuses.at(-1)).toBe('project-z · tracks onbekend · onleesbaar')
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
      expect(world.reads.length - reads, surface).toBe(6)
      await ui.unmount()
    }
  })
})

describe('the room', () => {
  test('gives every section, in order, what is left: a cut section shows what fits and one +N niet getoond row', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a') })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(12) })
    const lines = await linesOf(ui)

    expect(lines).toEqual([
      'project-a · index 3 min',
      'Nu bezig',
      '  sonnet · 12 min · Invented task: add the block · bld · PR #2150',
      'Wacht op een besluit',
      '  3 leveringen te promoveren · 2 open uitkomsten',
      'Tracks',
      '  P1 kosten',
      "     geen leveringen gepland · PR's: #2063 open",
      '  P2 bld · bezig',
      '     geen leveringen gepland · 1 bouwer',
      '  +4 niet getoond',
      'opruimen: 4 stale rijen',
    ])
    await ui.unmount()
  })

  test('never draws more rows than the pane has, and never a +N niet getoond row without an item above it', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a') })
    const titles = ['Nu bezig', 'Wacht op een besluit', 'Tracks', "PR's"]

    for (let rows = 1; rows <= 24; rows += 1) {
      const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(rows) })
      const lines = await linesOf(ui)

      expect(lines.length, `rows ${rows}`).toBeLessThanOrEqual(Math.max(1, rows - 1) + 1)

      lines.forEach((line, at) => {
        if (/^ +\+\d+ niet getoond/.test(line)) {
          expect(titles.includes(lines[at - 1] ?? ''), `rows ${rows} line ${at}`).toBe(false)
          expect(at, `rows ${rows}`).toBeGreaterThan(1)
        }
      })
      await ui.unmount()
    }
  })

  test('shows the Nu bezig section whole before Tracks gets any room', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a') })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(4) })

    // Wacht op een besluit (2 rows) does not fit in the one row that is left, so it is not drawn at all; the cleanup row does.
    expect(await linesOf(ui)).toEqual(['project-a · index 3 min', 'Nu bezig', '  sonnet · 12 min · Invented task: add the block · bld · PR #2150', 'opruimen: 4 stale rijen'])
    await ui.unmount()
  })

  test('cuts every row to the width of the pane in display cells, and keeps the track and the PR of a builder', async ($, on) => {
    const long = `${'日本語'.repeat(20)} een heel lange titel`
    const files = { ...healthyFiles('project-a'), [instructionOf('project-a', 'd-live-1')]: instructionText(long) }
    await started($, on, { files })

    for (const columns of [30, 50, 72]) {
      const ui = await $.ui.mount({ ...PANE, surface: 'mobile', props: sizeOf(44, columns) })
      const lines = await linesOf(ui)

      expect(lines.every(line => cellsOf(line) <= columns), `columns ${columns}`).toBe(true)

      if (columns >= 50) {
        expect(lines[2], `columns ${columns}`).toMatch(/….*· bld · PR #2150$/)
      }

      await ui.unmount()
    }
  })

  test('shows the total from the counts and one row for the builders the capped index does not list', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: producerLiveWork(9, 0) }) }
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines.filter(line => line.startsWith('  sonnet · 12 min'))).toHaveLength(8)
    expect(lines).toContain('  +1 niet getoond (index toont er max. 8)')
    expect(world.statuses.at(-1)).toContain('9 bouwers')
    await ui.unmount()
  })

  test('shows the field that cannot be read, for that builder only, and marks the project onleesbaar', async ($, on) => {
    const files = healthyFiles('project-a')
    files[specOf('project-a', 'd-live-1')] = JSON.stringify({ role: 'r' })
    delete files[instructionOf('project-a', 'd-live-1')]
    const { world } = await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })

    expect((await linesOf(ui))[2]).toBe('  model ? · 12 min · onbeschikbaar: instruction.md ontbreekt')
    expect(world.statuses.at(-1)).toContain('onleesbaar')
    await ui.unmount()
  })

  test('shows a builder without track and PR as model, minutes and title only', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [specOf('project-a', 'd-live-1')]: JSON.stringify({ model: 'opus' }) }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

    expect((await linesOf(ui))[2]).toBe('  opus · 12 min · Invented task: add the block')
    await ui.unmount()
  })

  test('shows a dispatch id that names a path as an invalid id and reads nothing for it', async ($, on) => {
    const live = { ...producerLiveWork(1, 0), live: [{ dispatch_id: '../../state', age_seconds: 60 }], open_prs: [] }
    const { world } = await started($, on, { files: { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: live }) } })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })

    expect((await linesOf(ui))[2]).toBe('  onbeschikbaar: ongeldige dispatch-id · 1 min · onbeschikbaar: ongeldige dispatch-id')
    expect(world.reads.some(path => path.includes('..'))).toBe(false)
    await ui.unmount()
  })
})

describe('the session', () => {
  test('registers /voortgang, refreshes once and sets the status line, without opening the pane', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a') })

    expect(world.commands).toEqual(['voortgang'])
    expect(world.opened).toEqual([])
    expect(world.runs).toHaveLength(2)
    expect(world.statuses.at(-1)).toBe('project-a · kosten: geen plan · bld: geen plan · 1 bouwer')
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
    expect(answer.text).toBe('Voortgang geopend: project-a · kosten: geen plan · bld: geen plan · 1 bouwer')
  })

  test('/voortgang answers the status line and one line per shown track where no surface places the pane', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), unplaced: 'no surface attached' })
    const answer = await $.command.run(command('voortgang'))

    expect(answer.text).toBe(
      [
        'project-a · kosten: geen plan · bld: geen plan · 1 bouwer',
        'P1 kosten · geen leveringen gepland',
        'P2 bld · geen leveringen gepland',
        'P2 viaref · geen leveringen gepland',
        'P3 fresh · 1 van 1 klaar',
        'P1 ci-profile · 5 van 8 klaar',
        'P2 docs · 0 van 1 klaar',
      ].join('\n'),
    )
  })

  test('/voortgang says there is no project, in the same words as the pane', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), unplaced: 'no surface attached' }, '/elsewhere')
    const answer = await $.command.run(command('voortgang'))

    expect(answer.text).toBe('geen VNX-project in deze map (geen .vnx-project-id boven /elsewhere)')
  })

  test('shows onbeschikbaar and reads no store when HOME is unknown', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a'), env: {} })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })

    expect(await linesOf(ui)).toContain('HOME onbekend, ~ niet op te lossen')
    expect(world.reads).toEqual([projectIdFileOf(PROJECT_DIR)])
    expect(world.runs).toEqual([])
    expect(world.statuses.at(-1)).toBe('')
    await ui.unmount()
  })
})

describe('the content boundary', () => {
  test('a full refresh reads the id file, the two state files, the specs and one title line, and runs only the two vnx shapes', async ($, on) => {
    const files = { ...healthyFiles('project-a'), ...Object.fromEntries(Object.entries(healthyFiles('project-b')).filter(([path]) => path.startsWith(ROOT))) }
    const { world } = await started($, on, { files, dirs: [`${ROOT}/project-c`] })
    const sorted = (list: string[]) => [...list].sort()
    const argvOf = (verb: string) => [BIN, verb, 'list', '--json', '--project-id', 'project-a', '--state-dir', storeOf('project-a')]

    expect(sorted(world.reads)).toEqual(
      sorted([
        projectIdFileOf(PROJECT_DIR),
        indexOf('project-a'),
        stateOf('project-a'),
        specOf('project-a', 'd-live-1'),
        instructionOf('project-a', 'd-live-1'),
        specOf('project-a', 'd-pr-1'),
      ]),
    )
    expect(sorted(world.exists)).toEqual(
      sorted([
        projectIdFileOf(CWD),
        projectIdFileOf('/work/project-a/src'),
        // The file is checked for, then read (which checks again).
        projectIdFileOf(PROJECT_DIR),
        projectIdFileOf(PROJECT_DIR),
        storeOf('project-a'),
        indexOf('project-a'),
        stateOf('project-a'),
        dbOf('project-a'),
        specOf('project-a', 'd-live-1'),
        instructionOf('project-a', 'd-live-1'),
        specOf('project-a', 'd-pr-1'),
      ]),
    )
    expect(sorted(world.runs.map(one => JSON.stringify(one.argv)))).toEqual(sorted([JSON.stringify(argvOf('objective')), JSON.stringify(argvOf('deliverable'))]))
    expect(world.runs.every(one => one.cwd === storeOf('project-a') && one.timeoutMs === 10_000)).toBe(true)
    expect(world.forbidden).toEqual([])
  })
})

describe('a store without runtime_coordination.db', () => {
  test('runs no vnx command for it, creates no file in it and shows tracks and deliverables onbeschikbaar', async ($, on) => {
    const files = healthyFiles('project-a')
    delete files[dbOf('project-a')]
    const { world } = await started($, on, { files, dirs: [storeOf('project-a')] })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })
    const lines = await linesOf(ui)

    expect(world.runs).toEqual([])
    expect(world.forbidden).toEqual([])
    expect(lines).toContain('  onbeschikbaar: runtime_coordination.db ontbreekt')
    expect(lines).toContain('  te promoveren onbeschikbaar: runtime_coordination.db ontbreekt · 2 open uitkomsten')
    expect(lines).toContain('  sonnet · 12 min · Invented task: add the block · bld · PR #2150')
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
