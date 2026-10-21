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
  scenarioOf,
  projectIdFileOf,
  specOf,
  stampMinutesAgo,
  stateJson,
  stateOf,
  storeOf,
  worldOf,
} from './fixtures'
import type { RunAnswer, WorldSetup } from './fixtures'

const SURFACES = ['terminal', 'mobile'] as const

type Found = { text: string; props: Record<string, unknown>; children: unknown[] }
type Drawn = { findAll: (query: { type: string }) => Promise<Found[]>; unmount: () => Promise<void> }

/** One drawn row: its whole text, and its segments with the props that carry their tone. */
type DrawnRow = { text: string; segments: { text: string; props: Record<string, unknown> }[] }

/**
 * The rows a mounted pane shows in drawing order. A row of segments is one Text that holds one Text per segment: the outer
 * one is the row and the inner ones follow it, so they are folded into it. The header is a Text of its own without children.
 */
async function rowsOf(ui: Drawn): Promise<DrawnRow[]> {
  const found = await ui.findAll({ type: 'Text' })
  const rows: DrawnRow[] = []

  for (let at = 0; at < found.length; at += 1) {
    const one = found[at] as Found
    const parts = one.children.filter(child => typeof child !== 'string').length

    rows.push({ text: one.text, segments: found.slice(at + 1, at + 1 + parts).map(part => ({ text: part.text, props: part.props })) })
    at += parts
  }

  return rows
}

const linesOf = async (ui: Drawn): Promise<string[]> => (await rowsOf(ui)).map(one => one.text)

/** The tone a segment was drawn with: the colour or dim prop the mod gave it. */
const toneOfProps = (props: Record<string, unknown>): string => (typeof props.color === 'string' ? props.color : props.dimColor === true ? 'dim' : props.bold === true ? 'bold' : 'normal')

/** Starts a session over `setup` and lets its first refresh finish. */
async function started($: Engine, on: On, setup: WorldSetup, cwd: string = CWD) {
  const world = worldOf(on, setup)
  const clock = mock.clock(on, { now: NOW })
  await $.session.start({ ...SESSION, cwd })
  await clock.settle()

  return { world, clock }
}

/** The deliverable list a scenario's `vnx` answers with; a denied run has none. */
function deliverablesOf(run: (argv: readonly string[]) => RunAnswer): { deliverable_ref: string; derived_status: string; title: string; last_activity: string }[] {
  const answer = run(['vnx', 'deliverable'])

  if ('deny' in answer) {
    throw new Error(answer.deny)
  }

  return JSON.parse(answer.stdout)
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
      "  1 open PR's zonder bouwer · 1 wacht op stoel",
      'Wacht op een besluit',
      '  2 te promoveren (now) · 2 open uitkomsten',
      '  backlog: 1 voorgesteld op next/later',
      'Tracks',
      '  P2 bld  geen leveringen gepland',
      '     bezig:  Invented task',
      '  P1 kosten  geen leveringen gepland',
      '  P2 viaref  geen leveringen gepland',
      '  P3 fresh  ▓▓▓▓▓▓▓▓  1/1',
      '  P1 ci-profile  ▓▓▓▓▓░░░  5/8',
      '     volgt:  A6 · A7',
      '  P2 docs  ░░░░░░░░  0/1',
      '     bezig:  D1',
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
    expect(text.split('\n')).not.toContain("PR's")

    for (const gone of ['werk:', 'live:', 'besluit nodig', 'zonder horizon', 'ongemeten', 'uitkomsten:', 'dicht', 'van 8 klaar']) {
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

      expect(world.statuses.at(-1)).toBe('project-a · bld: geen plan · kosten: geen plan · 1 bouwer')
    })
  }

  test('says Nu bezig: niets and Wacht op een besluit: niets when there is none, and no opruimen row at zero', async ($, on) => {
    const files = {
      ...healthyFiles('project-a'),
      [indexOf('project-a')]: indexJson({ liveWork: producerLiveWork(0, 0), outcomes: { available: true, total: 0, items: [] } }),
      [stateOf('project-a')]: stateJson({ prQueue: { available: true, reason: null, open_prs: [] } }),
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

  test('puts the track a builder works on above a P1 track with only a recent deliverable', async ($, on) => {
    const tracks = [
      { track_id: 'recent-p1', title: 'T', phase: 'queued', derived_status: 'queued', horizon: 'next', priority: 'P1', pr_ref: null },
      { track_id: 'bld', title: 'T', phase: 'queued', derived_status: 'queued', horizon: 'next', priority: 'P2', pr_ref: null },
    ]
    const deliverables = [{ deliverable_ref: 'r-0', track: 'recent-p1', derived_status: 'done', last_activity: stampMinutesAgo(2 * 24 * 60), title: 'T' }]
    const run = (argv: readonly string[]) => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : deliverables) })
    const { world } = await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines.slice(lines.indexOf('Tracks') + 1, lines.indexOf('Tracks') + 4)).toEqual([
      '  P2 bld  geen leveringen gepland',
      '     bezig:  Invented task',
      '  P1 recent-p1  ▓▓▓▓▓▓▓▓  1/1',
    ])
    expect(world.statuses.at(-1)).toBe('project-a · bld: geen plan · recent-p1 ▓▓▓▓▓▓▓▓ 1/1 · 1 bouwer')
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

    expect(lines).not.toContain("PR's")
    expect(lines).toContain("  1 open PR's zonder bouwer · onbeschikbaar: FileNotFoundError: 'gh'")
    expect(toneOfProps((await rowsOf(ui)).find(one => one.text.includes('onbeschikbaar: FileNotFoundError'))?.segments.at(-1)?.props ?? {})).toBe('error')
    await ui.unmount()
  })

  test('shows no PR number and no PR status in a track row, whether or not the PR queue can be read', async ($, on) => {
    const files = {
      ...healthyFiles('project-a'),
      [stateOf('project-a')]: stateJson({ prQueue: { available: false, reason: "FileNotFoundError: 'gh'" } }),
      [indexOf('project-a')]: indexJson({ liveWork: { available: false, reason: 'index stuk' } }),
    }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)
    const tracks = lines.slice(lines.indexOf('Tracks')).join('\n')

    expect(tracks).not.toMatch(/PR|dicht|onbekend|#\d/)
    expect(lines.some(line => line.includes('  P2 viaref'))).toBe(true)
    await ui.unmount()
  })

  test('shows a PR the index lists in Nu bezig when the state is unreadable, as onbeschikbaar with the reason', async ($, on) => {
    const files = { ...healthyFiles('project-a'), [stateOf('project-a')]: stateJson({ prQueue: { available: false, reason: 'gh weg' } }) }
    await started($, on, { files })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)

    expect(lines).toContain("  1 open PR's zonder bouwer · onbeschikbaar: gh weg")
    expect(lines.join('\n')).not.toContain('dicht')
    await ui.unmount()
  })

  test('starts the bezig and volgt rows of every track at the same column, also for a track without priority', async ($, on) => {
    const noPriority = { track_id: 'zonder-prio', title: CONTENT, phase: 'queued', derived_status: 'queued', horizon: 'now', priority: null, pr_ref: null }
    const items = [
      { deliverable_ref: 'pr:z-1', track: 'zonder-prio', derived_status: 'ready', last_activity: null, title: 'Z1: ready' },
      { deliverable_ref: 'pr:z-2', track: 'zonder-prio', derived_status: 'proposed', last_activity: null, title: 'Z2: proposed' },
      { deliverable_ref: 'pr:m-1', track: 'met-prio', derived_status: 'ready', last_activity: null, title: 'M1: ready' },
      { deliverable_ref: 'pr:m-2', track: 'met-prio', derived_status: 'proposed', last_activity: null, title: 'M2: proposed' },
    ]
    const run = (argv: readonly string[]) =>
      argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify([noPriority, { ...noPriority, track_id: 'met-prio', priority: 'P1' }]) } : { exitCode: 0, stdout: JSON.stringify(items) }
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const lines = await linesOf(ui)
    const at = lines.findIndex(line => line.startsWith('  - zonder-prio'))
    const withPriority = lines.findIndex(line => line.startsWith('  P1 met-prio'))

    expect(at).toBeGreaterThanOrEqual(0)
    expect(withPriority).toBeGreaterThanOrEqual(0)
    expect(lines.slice(at + 1, at + 3)).toEqual(['     bezig:  Z1', '     volgt:  Z2'])
    expect(lines.slice(withPriority + 1, withPriority + 3)).toEqual(['     bezig:  M1', '     volgt:  M2'])
    await ui.unmount()
  })

  test('shows tracks and deliverables onbeschikbaar when vnx exits 1 or answers no JSON, the rest still shown', async ($, on) => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 1, stdout: '' } : { exitCode: 0, stdout: 'x' })
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })
    const lines = await linesOf(ui)

    expect(lines).toContain('  onbeschikbaar: vnx exit 1')
    expect(lines).toContain('  te promoveren onbeschikbaar: vnx gaf ongeldige JSON · 2 open uitkomsten')
    expect(lines).toContain("  1 open PR's zonder bouwer · 1 wacht op stoel")
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

  test('keeps the builders of a track on its bezig row when its deliverables could not be read', async ($, on) => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? healthyRun(argv) : { exitCode: 3, stdout: '' })
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 120) })
    const lines = await linesOf(ui)
    const at = lines.indexOf('  P2 bld')

    expect(lines.slice(at, at + 3)).toEqual(['  P2 bld', '     leveringen onbeschikbaar: vnx exit 3', '     bezig:  Invented task'])
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
    expect(world.statuses.at(-1)).toBe('project-a · bld: geen plan · kosten: geen plan · 1 bouwer · verouderd')
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

describe('the deliverable is the unit of a track row', () => {
  const scenario = async ($: Engine, on: On, width = 100) => {
    const { files, run } = scenarioOf('project-a')
    const { world } = await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, width) })

    return { ui, world }
  }

  test('shows a track row with its bar and done/total, a bezig row and a volgt row with (+N), and no PR number', async ($, on) => {
    const { ui } = await scenario($, on, 90)
    const lines = await linesOf(ui)
    const at = lines.indexOf('  P1 alpha  ▓▓░░░░░░  3/12')

    expect(at).toBeGreaterThanOrEqual(0)
    expect(lines.slice(at, at + 3)).toEqual(['  P1 alpha  ▓▓░░░░░░  3/12', '     bezig:  K2a-3b', '     volgt:  K2a-3c · K2b · K3 · K4 · K5 · K6 · K7 · K8'])
    expect(lines.slice(lines.indexOf('Tracks')).join('\n')).not.toMatch(/PR|#\d|dicht|open/)
    await ui.unmount()
  })

  test('cuts the volgt row to the titles that fit and counts the rest as (+N)', async ($, on) => {
    const { ui } = await scenario($, on, 44)
    const lines = await linesOf(ui)
    const volgt = lines.find(line => line.startsWith('     volgt:'))

    expect(volgt).toBe('     volgt:  K2a-3c · K2b · K3 · K4  (+4)')
    expect(cellsOf(volgt ?? '')).toBeLessThanOrEqual(44)

    const row = (await rowsOf(ui)).find(one => one.text.startsWith('     volgt:'))

    expect(row?.segments.at(-1)).toMatchObject({ text: '  (+4)', props: { dimColor: true } })
    await ui.unmount()
  })

  test('names a deliverable by the title up to the first colon, or the first 40 cells', async ($, on) => {
    const long = `${'x'.repeat(60)}: tail`
    const items = [
      { deliverable_ref: 'pr:t-1', track: 't', derived_status: 'ready', last_activity: null, title: long },
      { deliverable_ref: 'pr:t-2', track: 't', derived_status: 'proposed', last_activity: null, title: `${'y'.repeat(50)} zonder dubbele punt` },
      { deliverable_ref: 'pr:t-3', track: 't', derived_status: 'proposed', last_activity: null, title: 'Kort: rest' },
    ]
    const tracks = [{ track_id: 't', title: CONTENT, phase: 'queued', derived_status: 'queued', horizon: 'now', priority: 'P1', pr_ref: null }]
    const run = (argv: readonly string[]) => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : items) })
    await started($, on, { files: healthyFiles('project-a'), run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 140) })
    const lines = await linesOf(ui)

    expect(lines).toContain(`     bezig:  ${'x'.repeat(39)}…`)
    expect(lines).toContain(`     volgt:  ${'y'.repeat(39)}… · Kort`)
    await ui.unmount()
  })

  test('draws the filled part of the bar ok, the empty part dim, a finished track ok, and what waits warn', async ($, on) => {
    const { ui } = await scenario($, on, 90)
    const rows = await rowsOf(ui)
    const tones = (text: string) => rows.find(one => one.text.startsWith(text))?.segments.map(part => [part.text, toneOfProps(part.props)])

    expect(tones('  P1 alpha')).toEqual([['  P1 alpha', 'normal'], ['  ', 'normal'], ['▓▓', 'success'], ['░░░░░░', 'dim'], ['  3/12', 'normal']])
    expect(tones('  P2 beta')).toEqual([['  P2 beta', 'success'], ['  ', 'normal'], ['▓▓▓▓▓▓▓▓', 'success'], ['  2/2', 'success']])
    expect(tones('     volgt:')?.filter(([, tone]) => tone === 'warning').map(([text]) => text)).toContain('K2a-3c')
    await ui.unmount()
  })
})

describe('Wacht op een besluit counts the now-tracks only', () => {
  test('counts the proposed deliverables on now-tracks, shows the backlog on next and later dim, and leaves a done track out', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)
    const at = lines.indexOf('Wacht op een besluit')

    // 8 proposed on alpha (now); 3 on next and later; 2 on a done track and 1 on a track without horizon are not backlog.
    expect(lines.slice(at, at + 3)).toEqual(['Wacht op een besluit', '  8 te promoveren (now) · 0 open uitkomsten', '  backlog: 3 voorgesteld op next/later'])
    expect(lines.join('\n')).not.toMatch(/orphan|wees|zonder horizon: |111/)

    const rows = await rowsOf(ui)
    const count = rows.find(one => one.text.startsWith('  8 te promoveren'))
    const backlog = rows.find(one => one.text.startsWith('  backlog'))

    expect(count?.segments.map(part => [part.text, toneOfProps(part.props)])).toContainEqual(['8 te promoveren (now)', 'warning'])
    expect(backlog?.segments.map(part => toneOfProps(part.props))).toEqual(['dim'])
    await ui.unmount()
  })

  test('shows no backlog row when nothing is proposed on next or later, and nothing waits when all counts are zero', async ($, on) => {
    const done = [{ deliverable_ref: 'pr:d-1', track: 't', derived_status: 'done', last_activity: null, title: 'D' }]
    const tracks = [{ track_id: 't', title: CONTENT, phase: 'queued', derived_status: 'queued', horizon: 'now', priority: 'P1', pr_ref: null }]
    const run = (argv: readonly string[]) => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : done) })
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ outcomes: { available: true, total: 0, items: [] } }) }
    await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)

    expect(lines).toContain('Wacht op een besluit: niets')
    expect(lines.some(line => line.includes('backlog'))).toBe(false)
    await ui.unmount()
  })

  test('says te promoveren onbeschikbaar when the tracks cannot be read, since the deliverables cannot be placed on a horizon', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    await started($, on, { files, run: argv => (argv[1] === 'objective' ? { exitCode: 2, stdout: '' } : run(argv)) })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })

    expect(await linesOf(ui)).toContain('  te promoveren onbeschikbaar: vnx exit 2 · 0 open uitkomsten')
    await ui.unmount()
  })
})

describe('the open PRs sit in Nu bezig', () => {
  test('shows an open PR on the row of its builder with CI and the seat that is still owed, and no PR section', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)
    const at = lines.indexOf('Nu bezig')

    expect(lines[at + 1]).toBe('  sonnet · 6 min · K2a-3b: replay van de spool · alpha · PR #3001')
    expect(lines[at + 2]).toBe('    PR #3001 · ci loopt · stoel open: kimi_gate')
    expect(lines).not.toContain("PR's")

    const row = (await rowsOf(ui)).find(one => one.text.startsWith('    PR #3001'))

    expect(row?.segments.at(-1)).toMatchObject({ text: 'stoel open: kimi_gate', props: { color: 'warning' } })
    await ui.unmount()
  })

  test('folds a PR that no builder owns and no bezig deliverable carries into one row, with klaar voor merge in ok', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)

    expect(lines).toContain("  1 open PR's zonder bouwer · 1 klaar voor merge")
    expect(lines.filter(one => one.includes('PR #3002'))).toEqual([])

    const row = (await rowsOf(ui)).find(one => one.text === "  1 open PR's zonder bouwer · 1 klaar voor merge")

    expect(row?.segments.at(-1)).toMatchObject({ text: '1 klaar voor merge', props: { color: 'success' } })
    await ui.unmount()
  })

  test('keeps the row of a PR under the bezig deliverable it belongs to', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const items = deliverablesOf(run)
    items.find((one: { deliverable_ref: string }) => one.deliverable_ref === 'pr:dlv-b-2').derived_status = 'in_progress'
    await started($, on, { files, run: argv => (argv[1] === 'deliverable' ? { exitCode: 0, stdout: JSON.stringify(items) } : run(argv)) })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)
    const at = lines.indexOf('  B2: tweede, klaar voor merge')

    expect(at).toBeGreaterThan(0)
    expect(lines[at + 1]).toBe('    PR #3002 · klaar voor merge')
    expect(lines.join('\n')).not.toContain("open PR's zonder bouwer")
    await ui.unmount()
  })

  test('draws ci rood in error on the row of a builder and wacht op stoel in warn in the folded row', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const queue = JSON.parse(files[stateOf('project-a')] ?? '{}')
    queue.pr_queue.open_prs[0].ci_status = 'fail'
    queue.pr_queue.open_prs[1].blocked_on = ['codex_gate']
    await started($, on, { files: { ...files, [stateOf('project-a')]: JSON.stringify(queue) }, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const rows = await rowsOf(ui)
    const toneOfPart = (row: string, part: string) => rows.find(one => one.text.startsWith(row))?.segments.find(one => one.text === part)?.props.color

    expect(toneOfPart('    PR #3001', 'ci rood')).toBe('error')
    expect(toneOfPart("  1 open PR's", '1 wacht op stoel')).toBe('warning')
    await ui.unmount()
  })

  test('counts an index PR that the readable PR queue misses in the folded row, without a status', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const queue = JSON.parse(files[stateOf('project-a')] ?? '{}')
    queue.pr_queue.open_prs = queue.pr_queue.open_prs.slice(0, 1)
    await started($, on, { files: { ...files, [stateOf('project-a')]: JSON.stringify(queue) }, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })

    expect(await linesOf(ui)).toContain("  1 open PR's zonder bouwer")
    await ui.unmount()
  })

  test('folds 20 open PRs, one owned by a builder, into one row after the builder, and leaves out the counts that are 0', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const queue = JSON.parse(files[stateOf('project-a')] ?? '{}')
    const index = JSON.parse(files[indexOf('project-a')] ?? '{}')
    const extra = Array.from({ length: 18 }, (_, at) => 4000 + at)
    const shape = (at: number) => (at < 5 ? { ci_status: 'pass', blocked_on: [] } : at < 12 ? { ci_status: 'pending', blocked_on: ['codex_gate'] } : at < 14 ? { ci_status: 'fail', blocked_on: [] } : { ci_status: 'pending', blocked_on: [] })
    queue.pr_queue.open_prs.push(...extra.map((number, at) => ({ number, title: `t${number}`, branch: `dispatch/dlv-x-${at}`, state: 'OPEN', merge_state: 'clean', gates_passed: [], ...shape(at) })))
    const liveWork = index.live_work ?? index.liveWork
    liveWork.open_prs.push(...extra.map((number, at) => ({ number, dispatch_id: `dlv-x-${at}` })))
    await started($, on, { files: { ...files, [stateOf('project-a')]: JSON.stringify(queue), [indexOf('project-a')]: JSON.stringify(index) }, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(60, 100) })
    const lines = await linesOf(ui)
    const at = lines.indexOf('Nu bezig')

    expect(lines.slice(at, at + 4)).toEqual([
      'Nu bezig',
      '  sonnet · 6 min · K2a-3b: replay van de spool · alpha · PR #3001',
      '    PR #3001 · ci loopt · stoel open: kimi_gate',
      "  19 open PR's zonder bouwer · 6 klaar voor merge · 7 wacht op stoel · 2 ci rood",
    ])
    expect(lines[at + 4]).toBe('Wacht op een besluit')
    expect(lines.filter(one => /PR #\d+/.test(one))).toEqual(['  sonnet · 6 min · K2a-3b: replay van de spool · alpha · PR #3001', '    PR #3001 · ci loopt · stoel open: kimi_gate'])

    const row = (await rowsOf(ui)).find(one => one.text.startsWith("  19 open PR's"))

    expect(row?.segments.map(one => [one.text, toneOfProps(one.props)])).toEqual([
      ["  19 open PR's zonder bouwer", 'dim'],
      [' · ', 'dim'],
      ['6 klaar voor merge', 'success'],
      [' · ', 'dim'],
      ['7 wacht op stoel', 'warning'],
      [' · ', 'dim'],
      ['2 ci rood', 'error'],
    ])
    await ui.unmount()
  })
})

describe('/voortgang plan', () => {
  test('draws the tracks on next and later with a bar and a volgt row, the tracks without horizon as one count, and no now-track', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const { world } = await started($, on, { files, run })
    const out = await $.command.run(command('voortgang', 'plan'))
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)

    expect(out.text).toContain('project-a')
    expect(world.opened).toEqual([{ id: 'voortgang', title: 'Voortgang' }])
    expect(lines).toEqual([
      'project-a · index 3 min · plan',
      'next',
      '  P2 next-t  ░░░░░░░░  0/2',
      '     volgt:  N1 · N2',
      'later',
      '  P3 later-t  ░░░░░░░░  0/1',
      '     volgt:  L1',
      '1 track zonder horizon',
    ])
    expect(lines.join('\n')).not.toMatch(/alpha|beta|Nu bezig|Wacht op/)
    expect((await rowsOf(ui)).at(-1)?.segments.map(part => toneOfProps(part.props))).toEqual(['dim'])
    await ui.unmount()
  })

  test('goes back to the now view with /voortgang and without argument', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    await started($, on, { files, run })
    await $.command.run(command('voortgang', 'plan'))
    await $.command.run(command('voortgang'))
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)

    expect(lines).toContain('Nu bezig')
    expect(lines).not.toContain('later')
    await ui.unmount()
  })

  test('answers an unknown argument with the two forms and opens nothing', async ($, on) => {
    const { world } = await started($, on, { files: healthyFiles('project-a') })
    const out = await $.command.run(command('voortgang', 'wat'))

    expect(out.text).toBe("Voortgang: onbekend argument 'wat'. Gebruik /voortgang of /voortgang plan.")
    expect(world.opened).toEqual([])
  })

  test('says tracks onbeschikbaar in the plan when the tracks cannot be read', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), run: argv => (argv[1] === 'objective' ? { exitCode: 2, stdout: '' } : healthyRun(argv)) })
    await $.command.run(command('voortgang', 'plan'))
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })

    expect(await linesOf(ui)).toContain('  onbeschikbaar: vnx exit 2')
    await ui.unmount()
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
      "  1 open PR's zonder bouwer · 1 wacht op stoel",
      'Wacht op een besluit',
      '  2 te promoveren (now) · 2 open uitkomsten',
      '  backlog: 1 voorgesteld op next/later',
      'Tracks',
      '  P2 bld  geen leveringen gepland',
      '     bezig:  Invented task',
      '  P1 kosten  geen leveringen gepland',
      '  +4 niet getoond',
    ])
    await ui.unmount()
  })

  test('never draws more rows than the pane has, and never a +N niet getoond row without an item above it', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a') })
    const titles = ['Nu bezig', 'Wacht op een besluit', 'Tracks']

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

  test('gives the Nu bezig section its items first, and a +N row for the rest', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const items = deliverablesOf(run)
    items.find((one: { deliverable_ref: string }) => one.deliverable_ref === 'pr:dlv-b-2').derived_status = 'in_progress'
    await started($, on, { files, run: argv => (argv[1] === 'deliverable' ? { exitCode: 0, stdout: JSON.stringify(items) } : run(argv)) })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(5, 100) })

    // The builder with its PR is the first item of Nu bezig; the deliverable with its PR (2 rows) does not fit in the room left: it is counted.
    expect(await linesOf(ui)).toEqual([
      'project-a · index 3 min',
      'Nu bezig',
      '  sonnet · 6 min · K2a-3b: replay van de spool · alpha · PR #3001',
      '    PR #3001 · ci loopt · stoel open: kimi_gate',
      '  +1 niet getoond',
    ])
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
    expect(world.statuses.at(-1)).toBe('project-a · bld: geen plan · kosten: geen plan · 1 bouwer')
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
    expect(answer.text).toBe('Voortgang geopend: project-a · bld: geen plan · kosten: geen plan · 1 bouwer')
  })

  test('/voortgang answers the status line and one line per shown track where no surface places the pane', async ($, on) => {
    await started($, on, { files: healthyFiles('project-a'), unplaced: 'no surface attached' })
    const answer = await $.command.run(command('voortgang'))

    expect(answer.text).toBe(
      [
        'project-a · bld: geen plan · kosten: geen plan · 1 bouwer',
        'P2 bld · geen leveringen gepland',
        'P1 kosten · geen leveringen gepland',
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

describe('round 3: absence stays loud and the pane agrees with itself', () => {
  /** The scenario with no PR in the index and an unreadable PR queue: the shape `gh` failing gives. */
  const unreadableQueue = (reason: string) => {
    const { files, run } = scenarioOf('project-a')
    const index = JSON.parse(files[indexOf('project-a')] ?? '{}')
    const liveWork = index.live_work ?? index.liveWork
    liveWork.open_prs = []

    return { files: { ...files, [indexOf('project-a')]: JSON.stringify(index), [stateOf('project-a')]: stateJson({ prQueue: { available: false, reason } }) }, run }
  }
  const nuBezigOf = (lines: string[]) => lines.slice(lines.indexOf('Nu bezig'), lines.indexOf('Wacht op een besluit'))

  test('draws onbeschikbaar in Nu bezig when the PR queue cannot be read and no PR is known, once, in error', async ($, on) => {
    const { files, run } = unreadableQueue("FileNotFoundError: 'gh'")
    await started($, on, { files, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })
    const lines = await linesOf(ui)

    expect(nuBezigOf(lines)).toEqual(['Nu bezig', '  sonnet · 6 min · K2a-3b: replay van de spool · alpha · PR #3001', "  onbeschikbaar: FileNotFoundError: 'gh'"])
    expect(lines.filter(one => one.includes("FileNotFoundError: 'gh'"))).toHaveLength(1)
    expect(toneOfProps((await rowsOf(ui)).find(one => one.text.includes('FileNotFoundError'))?.segments[0]?.props ?? {})).toBe('error')
    await ui.unmount()
  })

  test('draws the reason of an unreadable PR queue without a builder too, and never twice when PRs are folded', async ($, on) => {
    const { files, run } = unreadableQueue('gh weg')
    const noBuilder = { ...files, [indexOf('project-a')]: indexJson({ liveWork: { available: true, counts: { live: 0, starting: 0, stale: 0, unmeasured: 0 }, live: [], stale: [], unmeasured: [], open_prs: [] } }) }
    await started($, on, { files: noBuilder, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })

    expect(nuBezigOf(await linesOf(ui))).toEqual(['Nu bezig', '  onbeschikbaar: gh weg'])
    await ui.unmount()
  })

  test('draws a builder failure as its own onbeschikbaar row and still draws the PRs of a readable queue, folded', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const unreadable = { ...files, [indexOf('project-a')]: indexJson({ liveWork: { available: false, reason: 'index stuk' } }) }
    await started($, on, { files: unreadable, run })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, 100) })

    expect(nuBezigOf(await linesOf(ui))).toEqual(['Nu bezig', '  onbeschikbaar: index stuk', "  2 open PR's zonder bouwer · 1 klaar voor merge · 1 wacht op stoel"])
    await ui.unmount()
  })

  /** The plan view of a scenario whose next-t track has a first title of 40 characters and one more proposed deliverable. */
  const planLinesAt = async ($: Engine, on: On, columns: number) => {
    const { files, run } = scenarioOf('project-a')
    const items = deliverablesOf(run)
    const first = items.find(one => one.deliverable_ref === 'pr:dlv-n-1')

    if (first !== undefined) {
      first.title = 'x'.repeat(40)
    }

    await started($, on, { files, run: argv => (argv[1] === 'deliverable' ? { exitCode: 0, stdout: JSON.stringify(items) } : run(argv)) })
    await $.command.run(command('voortgang', 'plan'))
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(44, columns) })
    const lines = await linesOf(ui)
    await ui.unmount()

    return lines
  }

  test('keeps the (+N) count on the volgt row at 44 columns and shortens the first title to what fits', async ($, on) => {
    const row = (await planLinesAt($, on, 44)).find(one => one.includes('volgt:') && one.includes('x'))

    expect(row).toBe(`     volgt:  ${'x'.repeat(24)}…  (+1)`)
    expect(cellsOf(row ?? '')).toBeLessThanOrEqual(44)
  })

  test('draws only the count on the volgt row where not even one title fits', async ($, on) => {
    const lines = await planLinesAt($, on, 18)

    expect(lines).toContain('     volgt:  (+1)')
    expect(lines.every(one => cellsOf(one) <= 18)).toBe(true)
  })

  test('keeps the row of a PR under a ready deliverable on a later track, which bezig: names', async ($, on) => {
    const { files, run } = scenarioOf('project-a')
    const items = deliverablesOf(run)
    const later = items.find(one => one.deliverable_ref === 'pr:dlv-l-1')

    if (later !== undefined) {
      later.derived_status = 'ready'
      later.last_activity = stampMinutesAgo(60)
    }

    const queue = JSON.parse(files[stateOf('project-a')] ?? '{}')
    const index = JSON.parse(files[indexOf('project-a')] ?? '{}')
    queue.pr_queue.open_prs.push({ number: 3003, title: 'later', branch: 'dispatch/dlv-l-1', state: 'OPEN', ci_status: 'pending', merge_state: 'blocked', gates_passed: [], blocked_on: ['kimi_gate'] })
    ;(index.live_work ?? index.liveWork).open_prs.push({ number: 3003, dispatch_id: 'dlv-l-1' })
    await started($, on, {
      files: { ...files, [stateOf('project-a')]: JSON.stringify(queue), [indexOf('project-a')]: JSON.stringify(index) },
      run: argv => (argv[1] === 'deliverable' ? { exitCode: 0, stdout: JSON.stringify(items) } : run(argv)),
    })
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: sizeOf(60, 100) })
    const lines = await linesOf(ui)
    const at = lines.indexOf('  L1: voorgesteld op later')

    expect(lines.some(one => one.includes('bezig:  L1'))).toBe(true)
    expect(at).toBeGreaterThan(0)
    expect(lines[at + 1]).toBe('    PR #3003 · ci loopt · stoel open: kimi_gate')
    expect(lines.join('\n')).toContain("1 open PR's zonder bouwer")
    await ui.unmount()
  })
})
