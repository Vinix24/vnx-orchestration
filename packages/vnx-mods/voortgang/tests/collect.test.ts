import { describe, expect, test } from 'claude-code/testing'

import {
  ageMinutesOf,
  ancestorsOf,
  barCellsOf,
  barOf,
  busyDeliverableIdsOf,
  builderCountOf,
  collectModel,
  collectProject,
  commandText,
  configOf,
  deliverablesNotice,
  deliverableArgv,
  deliverableIdOf,
  deliverableViewOf,
  expandHome,
  formatAge,
  indexViewOf,
  isOutdated,
  isDispatchId,
  isProjectId,
  isStale,
  isUnreadable,
  objectiveArgv,
  parseJson,
  planOf,
  promotionOf,
  prRefsOf,
  resolveProject,
  shortTitleOf,
  shownTracksOf,
  stateViewOf,
  statusLine,
  summaryLines,
  trackViewOf,
} from '../hooks/collect'
import type { Io } from '../hooks/collect'
import { cellsOf, fit, fitRow, textOf } from '../hooks/view'
import type { Model, ProjectModel } from '../types'

import {
  BIN,
  CWD,
  DELIVERABLES,
  HOME,
  LIVE_WORK,
  NOW,
  OUTCOMES,
  PROJECT_DIR,
  ROOT,
  TRACKS,
  healthyFiles,
  indexJson,
  indexOf,
  instructionOf,
  instructionText,
  producerLiveWork,
  projectIdFileOf,
  specOf,
  stampMinutesAgo,
  stateJson,
  stateOf,
  storeOf,
} from './fixtures'

const json = (text: string): unknown => JSON.parse(text)

/** An in-memory `Io`: the files, the directories that exist without a file in them, and what `vnx` answers or throws. */
const ioOf = (
  files: Record<string, string>,
  dirs: string[],
  run: (argv: readonly string[]) => { exitCode: number; stdout: string } | Error,
): Io => ({
  list: async () => {
    throw new Error('the mod lists nothing')
  },
  exists: async path => path in files || dirs.includes(path) || Object.keys(files).some(file => file.startsWith(`${path}/`)),
  read: async path => {
    const text = files[path]

    if (text === undefined) {
      throw new Error(`ENOENT: ${path}`)
    }

    return text
  },
  run: async argv => {
    const answer = run(argv)

    if (answer instanceof Error) {
      throw answer
    }

    return { ...answer, stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
  },
})

const healthyAnswer = (argv: readonly string[]) =>
  argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(TRACKS) } : { exitCode: 0, stdout: JSON.stringify(DELIVERABLES) }

const healthy = async (files: Record<string, string> = healthyFiles('project-a')): Promise<ProjectModel> =>
  collectProject(ioOf(files, [], healthyAnswer), 'project-a', ROOT, BIN, NOW)

const modelOf = (project: ProjectModel): Model => ({ collectedAt: NOW, noProject: null, project })

describe('the options', () => {
  test('defaults to the data root and the vnx binary, and has no exclude option', () => {
    expect(configOf({})).toEqual({ dataRoot: '~/.vnx-data', vnxBin: '~/.vnx-system/current/bin/vnx', refreshSeconds: 120 })
    expect(configOf({ exclude: 'x, y' })).toEqual(configOf({}))
  })

  test('takes an interval no shorter than 15 s', () => {
    expect(configOf({ refresh_seconds: 2 }).refreshSeconds).toBe(15)
    expect(configOf({ refresh_seconds: 300 }).refreshSeconds).toBe(300)
  })

  test('resolves ~ from HOME, and refuses to guess without it', () => {
    expect(expandHome('~/.vnx-data', HOME)).toBe(ROOT)
    expect(expandHome('~', HOME)).toBe(HOME)
    expect(expandHome('/srv/vnx', undefined)).toBe('/srv/vnx')
    expect(expandHome('~/.vnx-data', undefined)).toBeNull()
  })

  test('accepts a dispatch id that stays one directory, with the longer alphabet a dispatch id has', () => {
    expect(isDispatchId('20261021-vg-2-r2')).toBe(true)
    expect(isDispatchId('Dispatch_1.b')).toBe(true)
    expect(isDispatchId('..')).toBe(false)
    expect(isDispatchId('../../state')).toBe(false)
  })

  test('accepts a project id only when it stays one directory under the data root', () => {
    expect(isProjectId('project-a')).toBe(true)
    expect(isProjectId('project-b.v2')).toBe(false)
    expect(isProjectId('Project-a')).toBe(false)
    expect(isProjectId('a')).toBe(false)
    expect(isProjectId('..')).toBe(false)
    expect(isProjectId('../etc')).toBe(false)
    expect(isProjectId('a/b')).toBe(false)
    expect(isProjectId('')).toBe(false)
  })

  test('builds the two vnx argv shapes with --state-dir', () => {
    const store = storeOf('project-a')

    expect(objectiveArgv(BIN, 'project-a', store)).toEqual([BIN, 'objective', 'list', '--json', '--project-id', 'project-a', '--state-dir', store])
    expect(deliverableArgv(BIN, 'project-a', store)).toEqual([BIN, 'deliverable', 'list', '--json', '--project-id', 'project-a', '--state-dir', store])
  })

  test('carry no project id of the operator in the defaults', () => {
    const spelled = JSON.stringify(configOf({})).toLowerCase()

    for (const parts of [['vnx', 'dev'], ['mission', 'control'], ['seocrawler', 'v2'], ['sales', 'copilot'], ['pacompany', 'engine']]) {
      expect(spelled.includes(parts.join('-'))).toBe(false)
    }
  })
})

describe('ages', () => {
  test('reads a timestamp with microseconds and an offset', () => {
    expect(ageMinutesOf(stampMinutesAgo(3), NOW)).toBe(3)
    expect(ageMinutesOf(stampMinutesAgo(120), NOW)).toBe(120)
    expect(ageMinutesOf('2026-10-20T15:57:00.123456Z', NOW)).toBe(2)
    expect(ageMinutesOf('not a time', NOW)).toBeNull()
    expect(ageMinutesOf(undefined, NOW)).toBeNull()
  })

  test('calls an index older than 60 minutes, or of unknown age, stale', () => {
    expect(isStale(3)).toBe(false)
    expect(isStale(60)).toBe(false)
    expect(isStale(61)).toBe(true)
    expect(isStale(null)).toBe(true)
  })

  test('spells an age in minutes, hours or days', () => {
    expect(formatAge(0)).toBe('0 min')
    expect(formatAge(59)).toBe('59 min')
    expect(formatAge(120)).toBe('2 u')
    expect(formatAge(3 * 24 * 60)).toBe('3 d')
    expect(formatAge(null)).toBe('leeftijd onbekend')
  })
})

describe('t0_index.json', () => {
  test('reads the live work, its open PRs and the open outcomes of a healthy index', () => {
    expect(indexViewOf(json(indexJson()), NOW)).toEqual({
      isOk: true,
      value: {
        ageMinutes: 3,
        liveWork: {
          isOk: true,
          value: {
            live: 1,
            starting: 0,
            stale: 4,
            unmeasured: 0,
            openPrs: [{ number: 2063, dispatchId: 'd-pr-1' }],
            running: { isOk: true, value: [{ dispatchId: 'd-live-1', ageSeconds: 725 }] },
          },
        },
        outcomes: { isOk: true, value: { total: 2 } },
      },
    })
  })

  test('keeps an open_prs entry that names no dispatch as evidence of an open PR, leaves out one without a number, and reads a missing list as none', () => {
    const live = { ...producerLiveWork(0, 0), open_prs: [{ number: 1 }, 'x', { number: 2, dispatch_id: 'd-2' }] }
    const view = indexViewOf(json(indexJson({ liveWork: live })), NOW)
    const none = indexViewOf(json(indexJson({ liveWork: { ...producerLiveWork(0, 0), open_prs: undefined } })), NOW)

    expect(view.isOk && view.value.liveWork.isOk && view.value.liveWork.value.openPrs).toEqual([{ number: 1, dispatchId: null }, { number: 2, dispatchId: 'd-2' }])
    expect(none.isOk && none.value.liveWork.isOk && none.value.liveWork.value.openPrs).toEqual([])
  })

  test('gives a block that says available: false its own reason, or says so', () => {
    const view = indexViewOf(json(indexJson({ liveWork: { available: false, reason: 'lease store unreadable' }, outcomes: { available: false } })), NOW)

    expect(view.isOk && view.value.liveWork).toEqual({ isOk: false, reason: 'lease store unreadable' })
    expect(view.isOk && view.value.outcomes).toEqual({ isOk: false, reason: 'available: false' })
  })

  test('never reads a missing block or missing counts as zero', () => {
    const missing = indexViewOf({ timestamp: stampMinutesAgo(1) }, NOW)
    const partial = indexViewOf(json(indexJson({ liveWork: { available: true, counts: { live: 1 } }, outcomes: { ...OUTCOMES, total: 'x' } })), NOW)

    expect(missing.isOk && missing.value.liveWork).toEqual({ isOk: false, reason: 'blok live_work ontbreekt' })
    expect(missing.isOk && missing.value.outcomes).toEqual({ isOk: false, reason: 'blok open_outcomes ontbreekt' })
    expect(partial.isOk && partial.value.liveWork).toEqual({ isOk: false, reason: 'counts onvolledig' })
    expect(partial.isOk && partial.value.outcomes).toEqual({ isOk: false, reason: 'total ontbreekt' })
    expect(indexViewOf([], NOW)).toEqual({ isOk: false, reason: 'geen JSON-object' })
  })

  test('parses JSON or says it is invalid', () => {
    expect(parseJson('{"a":1}')).toEqual({ isOk: true, value: { a: 1 } })
    expect(parseJson('{"a":')).toEqual({ isOk: false, reason: 'ongeldige JSON' })
  })
})

describe('t0_state.json', () => {
  test('reads the open PRs with the seats that passed and the seats still owed', () => {
    expect(stateViewOf(json(stateJson()), NOW)).toEqual({
      isOk: true,
      value: {
        ageMinutes: 3,
        prs: { isOk: true, value: [{ number: 2063, title: 'feat: voortgang', ci: 'pass', merge: 'blocked', passed: ['codex_gate'], owed: ['kimi_gate'] }] },
      },
    })
  })

  test('says why the PR queue is unavailable, and keeps a missing seat list unknown', () => {
    const offline = stateViewOf(json(stateJson({ prQueue: { available: false, reason: "FileNotFoundError: 'gh'" } })), NOW)
    const noSeats = stateViewOf(json(stateJson({ prQueue: { available: true, open_prs: [{ number: 7, title: 't' }] } })), NOW)

    expect(offline.isOk && offline.value.prs).toEqual({ isOk: false, reason: "FileNotFoundError: 'gh'" })
    expect(noSeats.isOk && noSeats.value.prs).toEqual({ isOk: true, value: [{ number: 7, title: 't', ci: '?', merge: '?', passed: null, owed: null }] })
  })
})

describe('vnx objective list and deliverable list', () => {
  test('reads every track on every horizon with its priority and the PR numbers of pr_ref', () => {
    const view = trackViewOf(TRACKS)

    expect(view.isOk && view.value.tracks.length).toBe(TRACKS.length)
    expect(view.isOk && view.value.tracks[0]).toEqual({ id: 'ci-profile', priority: 'P1', horizon: 'now', phase: 'queued', prRefs: [2001, 2002] })
    expect(view.isOk && view.value.tracks.find(one => one.id === 'u1')).toEqual({ id: 'u1', priority: '-', horizon: '-', phase: 'queued', prRefs: [] })
    expect(trackViewOf({ tracks: [] })).toEqual({ isOk: false, reason: 'vnx gaf geen lijst' })
  })

  test('reads pr_ref as the numbers in the string, in order, and a missing one as none', () => {
    expect(prRefsOf('#2139,#2142')).toEqual([2139, 2142])
    expect(prRefsOf('#2141 #2148')).toEqual([2141, 2148])
    expect(prRefsOf(null)).toEqual([])
    expect(prRefsOf('')).toEqual([])
  })

  test('lists the deliverables per track in the order given, with the id behind the ref, the title and the status, and counts them', () => {
    const view = deliverableViewOf(DELIVERABLES, NOW)
    const counts = view.isOk ? view.value.byTrack : {}

    expect(view.isOk).toBe(true)
    expect(Object.keys(counts).sort()).toEqual(['ci-profile', 'docs', 'fresh', 'l1', 'old-d'])
    expect(counts['ci-profile']).toMatchObject({ total: 8, done: 5, isRecent: false })
    expect(counts['ci-profile']?.items.map(item => item.id)).toEqual(['dlv-cip-6', 'dlv-cip-7', 'dlv-cip-1', 'dlv-cip-2', 'dlv-cip-3', 'dlv-cip-4', 'dlv-cip-5', 'dlv-cip-8'])
    expect(counts['ci-profile']?.items[0]).toEqual({ id: 'dlv-cip-6', title: 'A6: the sixth step, proposed', status: 'proposed' })
    expect(counts['docs']).toMatchObject({ total: 1, done: 0, isRecent: false })
    expect(counts['fresh']).toMatchObject({ total: 1, done: 1, isRecent: true })
    expect(counts['old-d']).toMatchObject({ total: 1, done: 1, isRecent: false })
    expect(deliverableViewOf('x', NOW)).toEqual({ isOk: false, reason: 'vnx gaf geen lijst' })
  })

  test('takes the id of a deliverable_ref after the colon, and a title on one line', () => {
    expect(deliverableIdOf('pr:dlv-1')).toBe('dlv-1')
    expect(deliverableIdOf('plain')).toBe('plain')
    expect(deliverableIdOf(undefined)).toBe('')

    const view = deliverableViewOf([{ track: 't', derived_status: 'ready', deliverable_ref: 'pr:x', title: 'one\ntwo' }, { track: 't', derived_status: 'ready' }], NOW)

    expect(view.isOk && view.value.byTrack.t?.items).toEqual([{ id: 'x', title: 'one two', status: 'ready' }, { id: '', title: '', status: 'ready' }])
  })

  test('calls a deliverable recent up to seven days, and not recent when last_activity is missing or unreadable', () => {
    const at = (days: number) => ({ track: 't', derived_status: 'ready', last_activity: stampMinutesAgo(days * 24 * 60) })
    const isRecent = (list: unknown[]) => {
      const result = deliverableViewOf(list, NOW)

      return result.isOk && result.value.byTrack.t?.isRecent
    }

    expect(isRecent([at(7)])).toBe(true)
    expect(isRecent([at(8)])).toBe(false)
    expect(isRecent([{ track: 't', derived_status: 'ready' }])).toBe(false)
    expect(isRecent([{ track: 't', derived_status: 'ready', last_activity: 'gisteren' }])).toBe(false)
    expect(isRecent([at(8), at(1)])).toBe(true)
  })

  test('counts a track named like an Object.prototype member as any other', () => {
    const result = deliverableViewOf([{ track: '__proto__', derived_status: 'proposed' }, { track: '__proto__', derived_status: 'done' }, { track: 'constructor', derived_status: 'done' }], NOW)

    expect(result.isOk).toBe(true)

    if (result.isOk) {
      expect(result.value.byTrack['__proto__']).toMatchObject({ total: 2, done: 1, isRecent: false })
      expect(result.value.byTrack['constructor']).toMatchObject({ total: 1, done: 1, isRecent: false })
      expect(Object.keys(result.value.byTrack).sort()).toEqual(['__proto__', 'constructor'])
    }
  })

  test('make a list with a malformed entry onbeschikbaar instead of dropping the entry', () => {
    expect(trackViewOf([TRACKS[0], null])).toEqual({ isOk: false, reason: 'vnx gaf geen object op plaats 2' })
    expect(deliverableViewOf([null], NOW)).toEqual({ isOk: false, reason: 'vnx gaf geen object op plaats 1' })
  })
})

describe('the progress bar', () => {
  test('has eight cells, filled for done and rounded down', () => {
    expect(barOf({ items: [], total: 1, done: 0, isRecent: false })).toBe('░░░░░░░░')
    expect(barOf({ items: [], total: 8, done: 5, isRecent: false })).toBe('▓▓▓▓▓░░░')
    expect(barOf({ items: [], total: 8, done: 8, isRecent: false })).toBe('▓▓▓▓▓▓▓▓')
    expect(barOf({ items: [], total: 3, done: 1, isRecent: false })).toBe('▓▓░░░░░░')
    expect(barOf({ items: [], total: 9, done: 8, isRecent: false })).toBe('▓▓▓▓▓▓▓░')
  })
})

describe('the project of a session', () => {
  const idFiles = (files: Record<string, string>) => ioOf(files, [], healthyAnswer)

  test('walks up from the cwd: the directory itself, and two levels up', async () => {
    expect(await resolveProject(idFiles({ [projectIdFileOf(CWD)]: 'here\n' }), CWD)).toEqual({ isOk: true, value: 'here' })
    expect(await resolveProject(idFiles({ [projectIdFileOf(PROJECT_DIR)]: 'project-a\n' }), CWD)).toEqual({ isOk: true, value: 'project-a' })
    expect(await resolveProject(idFiles({ [projectIdFileOf('')]: 'at-root' }), CWD)).toEqual({ isOk: true, value: 'at-root' })
  })

  test('takes the first line with all whitespace removed, as bin/vnx does, and the nearest file', async () => {
    const io = idFiles({ [projectIdFileOf(PROJECT_DIR)]: '  project-a  \nignored\n', [projectIdFileOf('/work')]: 'outer' })

    expect(await resolveProject(io, CWD)).toEqual({ isOk: true, value: 'project-a' })
  })

  test('strips inner whitespace before it checks the id, and rejects an id that stays invalid', async () => {
    const stripped = idFiles({ [projectIdFileOf(PROJECT_DIR)]: ' proj\t- x \n' })
    const invalid = idFiles({ [projectIdFileOf(PROJECT_DIR)]: 'Project_A\n', [projectIdFileOf('/work')]: 'outer' })

    expect(await resolveProject(idFiles({ [projectIdFileOf(PROJECT_DIR)]: 'proj-x\n' }), CWD)).toEqual({ isOk: true, value: 'proj-x' })
    expect(await resolveProject(stripped, CWD)).toEqual({ isOk: true, value: 'proj-x' })
    expect(await resolveProject(invalid, CWD)).toEqual({ isOk: false, reason: `geen geldig project-id in ${projectIdFileOf(PROJECT_DIR)}` })
  })

  test('says there is no project, and names the cwd, when no directory above it has the file', async () => {
    expect(await resolveProject(idFiles({}), CWD)).toEqual({ isOk: false, reason: `geen VNX-project in deze map (geen .vnx-project-id boven ${CWD})` })
  })

  test('does not fall back to another project: an empty or unfit id ends the search', async () => {
    const files = { [projectIdFileOf(PROJECT_DIR)]: '\n', [projectIdFileOf('/work')]: 'outer' }
    const empty = await resolveProject(idFiles(files), CWD)
    const unfit = await resolveProject(idFiles({ [projectIdFileOf(PROJECT_DIR)]: '../etc\n' }), CWD)

    expect(empty).toEqual({ isOk: false, reason: `geen geldig project-id in ${projectIdFileOf(PROJECT_DIR)}` })
    expect(unfit.isOk).toBe(false)
  })

  test('says why when the check or the read of the file rejects', async () => {
    const base = idFiles({ [projectIdFileOf(PROJECT_DIR)]: 'project-a' })
    const noCheck: Io = { ...base, exists: async () => Promise.reject(new Error('EIO: x')) }
    const noRead: Io = { ...base, read: async () => Promise.reject(new Error('EACCES: y')) }

    expect(await resolveProject(noCheck, CWD)).toEqual({ isOk: false, reason: 'onbeschikbaar: EIO: x' })
    expect(await resolveProject(noRead, CWD)).toEqual({ isOk: false, reason: '.vnx-project-id onbeschikbaar: EACCES: y' })
  })

  test('lists the directories from the cwd to the root, nearest first', () => {
    expect(ancestorsOf('/a/b/c')).toEqual(['/a/b/c', '/a/b', '/a', '/'])
    expect(ancestorsOf('/')).toEqual(['/'])
    expect(ancestorsOf('/a//b/')).toEqual(['/a/b', '/a', '/'])
  })

  test('a model without a project reads no store, and has an empty status line', async () => {
    const asked: string[] = []
    const base = idFiles({})
    const io: Io = { ...base, read: async path => (asked.push(path), base.read(path)), run: async (argv, init) => (asked.push(argv.join(' ')), base.run(argv, init)) }
    const model = await collectModel(io, configOf({}), HOME, CWD, NOW)

    expect(model).toEqual({ collectedAt: NOW, noProject: `geen VNX-project in deze map (geen .vnx-project-id boven ${CWD})`, project: null })
    expect(asked).toEqual([])
    expect(statusLine(model)).toBe('')
  })

  test('a model without a cwd says so, and a model without HOME reads nothing of the store', async () => {
    const files = healthyFiles('project-a')

    expect((await collectModel(idFiles(files), configOf({}), HOME, null, NOW)).noProject).toBe('geen VNX-project in deze map (werkmap onbekend)')
    expect((await collectModel(idFiles(files), configOf({}), undefined, CWD, NOW)).noProject).toBe('onbeschikbaar: HOME onbekend, ~ niet op te lossen'.replace('onbeschikbaar: ', ''))
  })

  test('reads the store of the id the file names, and no other', async () => {
    const files = { ...healthyFiles('project-a'), ...Object.fromEntries(Object.entries(healthyFiles('project-b')).filter(([path]) => path.startsWith(ROOT))) }
    const model = await collectModel(idFiles(files), configOf({}), HOME, CWD, NOW)

    expect(model.project?.id).toBe('project-a')
  })
})

describe('one project', () => {
  test('reads both files and runs both commands for a healthy store', async () => {
    const project = await healthy()

    expect(project.hasStore).toBe(true)
    expect(project.hasStore && project.index.isOk && project.index.value.liveWork.isOk).toBe(true)
    expect(project.hasStore && project.tracks.isOk && project.tracks.value.tracks.length).toBe(TRACKS.length)
    expect(project.hasStore && project.deliverables.isOk && project.deliverables.value.byTrack['ci-profile']?.items.length).toBe(8)
    expect(project.hasStore && project.prTracks).toEqual({ 2063: 'kosten' })
    expect(isUnreadable(project)).toBe(false)
    expect(isOutdated(project)).toBe(false)
  })

  test('says geen store when the store directory is missing, and runs nothing for it', async () => {
    const asked: string[] = []
    const project = await collectProject(ioOf({}, [], argv => (asked.push(argv.join(' ')), healthyAnswer(argv))), 'project-a', ROOT, BIN, NOW)

    expect(project).toEqual({ id: 'project-a', hasStore: false, reason: 'geen store' })
    expect(asked).toEqual([])
    expect(isUnreadable(project)).toBe(true)
  })

  test('turns a rejecting exists or read into onbeschikbaar for that part only', async () => {
    const base = ioOf(healthyFiles('project-a'), [], healthyAnswer)
    const noState: Io = { ...base, read: async path => (path === stateOf('project-a') ? Promise.reject(new Error('EIO: state')) : base.read(path)) }
    const noStore: Io = { ...base, exists: async path => (path === storeOf('project-a') ? Promise.reject(new Error('EIO: store')) : base.exists(path)) }
    const project = await collectProject(noState, 'project-a', ROOT, BIN, NOW)

    expect(project.hasStore && project.state).toEqual({ isOk: false, reason: 'state.json onbeschikbaar: EIO: state'.replace('state.json', 't0_state.json') })
    expect(project.hasStore && project.index.isOk).toBe(true)
    expect(await collectProject(noStore, 'project-a', ROOT, BIN, NOW)).toEqual({ id: 'project-a', hasStore: false, reason: 'onbeschikbaar: EIO: store' })
  })

  test('refuses a project id that would leave the data root', async () => {
    const asked: string[] = []
    const project = await collectProject(ioOf({}, [], argv => (asked.push(argv.join(' ')), healthyAnswer(argv))), '../etc', ROOT, BIN, NOW)

    expect(project).toEqual({ id: '../etc', hasStore: false, reason: 'ongeldige project-id' })
    expect(asked).toEqual([])
  })

  test('says which file is missing or not JSON', async () => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: '{"a":' }
    delete files[stateOf('project-a')]
    const project = await healthy(files)

    expect(project.hasStore && project.index).toEqual({ isOk: false, reason: 'ongeldige JSON' })
    expect(project.hasStore && project.state).toEqual({ isOk: false, reason: 't0_state.json ontbreekt' })
  })

  test('names the exit code, a time-out, a start failure, invalid JSON and a wrong shape from vnx', async () => {
    const tracksOf = async (run: (argv: readonly string[]) => { exitCode: number; stdout: string } | Error) => {
      const project = await collectProject(ioOf(healthyFiles('project-a'), [], run), 'project-a', ROOT, BIN, NOW)

      return project.hasStore ? project.tracks : null
    }

    expect(await tracksOf(() => ({ exitCode: 1, stdout: '' }))).toEqual({ isOk: false, reason: 'vnx exit 1' })
    expect(await tracksOf(() => new Error('ETIMEDOUT'))).toEqual({ isOk: false, reason: 'vnx time-out' })
    expect(await tracksOf(() => new Error('spawn ENOENT'))).toEqual({ isOk: false, reason: 'vnx startfout: spawn ENOENT' })
    expect(await tracksOf(() => ({ exitCode: 0, stdout: 'x' }))).toEqual({ isOk: false, reason: 'vnx gaf ongeldige JSON' })
    expect(await tracksOf(() => ({ exitCode: 0, stdout: '{}' }))).toEqual({ isOk: false, reason: 'vnx gaf geen lijst' })
  })

  test('calls a project with a two-hour-old index outdated', async () => {
    const project = await healthy({ ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ ageMinutes: 120 }) })

    expect(isOutdated(project)).toBe(true)
    expect(isUnreadable(project)).toBe(false)
  })

  test('leaves a PR out of prTracks when its dispatch spec is gone, has no track, or its id is not one directory name', async () => {
    const files = healthyFiles('project-a')
    delete files[specOf('project-a', 'd-pr-1')]
    const gone = await healthy(files)
    const unnamed = await healthy({ ...files, [specOf('project-a', 'd-pr-1')]: JSON.stringify({ model: 'sonnet' }) })
    const asked: string[] = []
    const live = { ...producerLiveWork(0, 0), open_prs: [{ number: 5, dispatch_id: '../../state' }] }
    const base = ioOf({ ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: live }) }, [], healthyAnswer)
    const unsafe = await collectProject({ ...base, read: async path => (asked.push(path), base.read(path)) }, 'project-a', ROOT, BIN, NOW)

    expect(gone.hasStore && gone.prTracks).toEqual({})
    expect(unnamed.hasStore && unnamed.prTracks).toEqual({})
    expect(unsafe.hasStore && unsafe.prTracks).toEqual({})
    expect(asked.some(path => path.includes('..'))).toBe(false)
    expect(isUnreadable(gone)).toBe(false)
  })
})

describe('the tracks the pane shows', () => {
  const idsOf = (project: ProjectModel) => shownTracksOf(project)?.map(one => one.track.id)

  test('are every now track plus every track with activity, by activity tier (builder, open PR, recent deliverable, none), then priority, then id', async () => {
    const project = await healthy()

    // tier 1: bld (P2, a builder). Tier 2: kosten (P1, the spec of PR 2063's dispatch), viaref (P2, pr_ref lists 2063). Tier 3: fresh (P3, a recent deliverable). Tier 4: ci-profile, docs
    expect(idsOf(project)).toEqual(['bld', 'kosten', 'viaref', 'fresh', 'ci-profile', 'docs'])
  })

  test('leave out a track without activity that is not on the now horizon, even with an old deliverable', async () => {
    const shown = idsOf(await healthy())

    expect(shown).not.toContain('old-d')
    expect(shown).not.toContain('l1')
    expect(shown).not.toContain('n1')
    expect(shown).not.toContain('u1')
  })

  test('count a builder, an open PR from pr_ref, an open PR from a dispatch spec and a recent deliverable each as activity on their own', async () => {
    const activityOf = async (change: (files: Record<string, string>) => void, id: string) => {
      const files = healthyFiles('project-a')
      change(files)
      const project = await healthy(files)

      return shownTracksOf(project)?.find(one => one.track.id === id)?.hasActivity
    }
    const noBuilderTrack = (files: Record<string, string>) => (files[specOf('project-a', 'd-live-1')] = JSON.stringify({ model: 'sonnet' }))
    const noPrSpec = (files: Record<string, string>) => delete files[specOf('project-a', 'd-pr-1')]
    const closedPr = (files: Record<string, string>) => {
      files[stateOf('project-a')] = stateJson({ prQueue: { available: true, open_prs: [] } })
      files[indexOf('project-a')] = indexJson({ liveWork: { ...LIVE_WORK, open_prs: [] } })
    }

    expect(await activityOf(() => {}, 'bld')).toBe(true)
    expect(await activityOf(noBuilderTrack, 'bld')).toBeUndefined()
    expect(await activityOf(() => {}, 'kosten')).toBe(true)
    expect(await activityOf(noPrSpec, 'kosten')).toBeUndefined()
    expect(await activityOf(() => {}, 'viaref')).toBe(true)
    expect(await activityOf(closedPr, 'viaref')).toBeUndefined()
    expect(await activityOf(closedPr, 'kosten')).toBeUndefined()
    expect(await activityOf(() => {}, 'fresh')).toBe(true)
  })

  test('carry the progress, the builders and the PRs of their pr_ref with open or closed', async () => {
    const shown = shownTracksOf(await healthy()) ?? []
    const byId = (id: string) => shown.find(one => one.track.id === id)

    expect(byId('ci-profile')?.progress).toMatchObject({ total: 8, done: 5, isRecent: false })
    expect(byId('ci-profile')?.prs).toEqual([{ number: 2001, isOpen: false }, { number: 2002, isOpen: false }])
    expect(byId('kosten')?.progress).toBeNull()
    expect(byId('kosten')?.prs).toEqual([{ number: 2063, isOpen: true }])
    expect(byId('bld')?.builders).toBe(1)
    expect(byId('viaref')?.prs).toEqual([{ number: 2063, isOpen: true }])
  })

  test('treat a PR as open when either readable source lists it', async () => {
    const stateOnly = healthyFiles('project-a')
    stateOnly[indexOf('project-a')] = indexJson({ liveWork: { ...LIVE_WORK, open_prs: [] } })
    const indexOnly = healthyFiles('project-a')
    indexOnly[stateOf('project-a')] = stateJson({ prQueue: { available: false, reason: 'gh weg' } })
    const prsOf = async (files: Record<string, string>) => shownTracksOf(await healthy(files))?.find(one => one.track.id === 'viaref')?.prs

    expect(await prsOf(healthyFiles('project-a'))).toEqual([{ number: 2063, isOpen: true }])
    expect(await prsOf(stateOnly)).toEqual([{ number: 2063, isOpen: true }])
    expect(await prsOf(indexOnly)).toEqual([{ number: 2063, isOpen: true }])
  })

  test('mark the PRs of pr_ref unknown, not closed, when neither source can be read, and keep the track', async () => {
    const files = healthyFiles('project-a')
    files[stateOf('project-a')] = stateJson({ prQueue: { available: false, reason: 'gh weg' } })
    files[indexOf('project-a')] = indexJson({ liveWork: { available: false, reason: 'index stuk' } })
    const shown = shownTracksOf(await healthy(files)) ?? []
    const viaref = shown.find(one => one.track.id === 'viaref')

    expect(viaref?.prs).toEqual([{ number: 2063, isOpen: null }])
    expect(shown.find(one => one.track.id === 'ci-profile')?.prs.every(pr => pr.isOpen === null)).toBe(true)
  })

  test('are null when the tracks could not be read, and say so in the headless summary', async () => {
    const project = await collectProject(ioOf(healthyFiles('project-a'), [], () => ({ exitCode: 1, stdout: '' })), 'project-a', ROOT, BIN, NOW)

    expect(shownTracksOf(project)).toBeNull()
    expect(summaryLines(modelOf(project))[1]).toBe('tracks onbeschikbaar: vnx exit 1')
  })

  test('are not a track without leveringen when the deliverables could not be read', async () => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(TRACKS) } : { exitCode: 2, stdout: '' })
    const project = await collectProject(ioOf(healthyFiles('project-a'), [], run), 'project-a', ROOT, BIN, NOW)
    const shown = shownTracksOf(project) ?? []

    expect(shown.map(one => one.unreadable)).toEqual(shown.map(() => 'vnx exit 2'))
    expect(summaryLines(modelOf(project)).slice(1)).toContain('P1 ci-profile · leveringen onbeschikbaar: vnx exit 2')
  })
})

describe('the status line', () => {
  const H = 'project-a · bld: geen plan · kosten: geen plan · 1 bouwer'
  const trackOf = (track_id: string, priority = 'P1', horizon = 'now') => ({ track_id, title: 'T', phase: 'queued', derived_status: 'queued', horizon, priority, pr_ref: null })
  const deliverablesOf = (track: string, done: number, total: number) =>
    Array.from({ length: total }, (_, at) => ({ deliverable_ref: `${track}-${at}`, track, derived_status: at < done ? 'done' : 'ready', last_activity: stampMinutesAgo(30 * 24 * 60), title: 'T' }))
  /** A project whose `vnx` answers `tracks` and `deliverables`, with `live` builders in the index. */
  const lineOf = async (tracks: unknown[], deliverables: unknown[] | 'exit', live = 1) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: producerLiveWork(live, 0) }) }
    const run = (argv: readonly string[]) =>
      argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(tracks) } : deliverables === 'exit' ? { exitCode: 2, stdout: '' } : { exitCode: 0, stdout: JSON.stringify(deliverables) }

    return statusLine(modelOf(await collectProject(ioOf(files, [], run), 'project-a', ROOT, BIN, NOW)))
  }

  test('names the first two tracks of the pane with their bar and X/Y, and the builders', async () => {
    const line = await lineOf([trackOf('aaa'), trackOf('bbb'), trackOf('ccc')], [...deliverablesOf('aaa', 5, 8), ...deliverablesOf('bbb', 2, 8), ...deliverablesOf('ccc', 8, 8)])

    expect(line).toBe('project-a · aaa ▓▓▓▓▓░░░ 5/8 · bbb ▓▓░░░░░░ 2/8 · 1 bouwer')
  })

  test('follows the order of the pane: the track with the builder first', async () => {
    const tracks = (await healthy()) && shownTracksOf(await healthy())?.map(one => one.track.id)

    expect(statusLine(modelOf(await healthy()))).toBe(H)
    expect(tracks?.slice(0, 2)).toEqual(['bld', 'kosten'])
  })

  test('says geen plan for a track without deliverables', async () => {
    expect(await lineOf([trackOf('aaa'), trackOf('bbb')], deliverablesOf('bbb', 6, 8), 2)).toBe('project-a · aaa: geen plan · bbb ▓▓▓▓▓▓░░ 6/8 · 2 bouwers')
  })

  test('says ? for a track when the deliverables could not be read, and onleesbaar', async () => {
    expect(await lineOf([trackOf('aaa'), trackOf('bbb')], 'exit')).toBe('project-a · aaa: ? · bbb: ? · 1 bouwer · onleesbaar')
  })

  test('says geen tracks in beweging when no track is shown', async () => {
    expect(await lineOf([trackOf('later-1', 'P2', 'later')], [], 0)).toBe('project-a · geen tracks in beweging')
  })

  test('says tracks onbekend, not geen tracks in beweging, when the tracks could not be read', async () => {
    const project = await collectProject(ioOf(healthyFiles('project-a'), [], () => ({ exitCode: 1, stdout: '' })), 'project-a', ROOT, BIN, NOW)

    expect(statusLine(modelOf(project))).toBe('project-a · tracks onbekend · 1 bouwer · onleesbaar')
  })

  test('names bouwer for one, bouwers for more, and nothing at zero', async () => {
    const tracks = [trackOf('aaa')]
    const deliverables = deliverablesOf('aaa', 1, 2)

    expect(await lineOf(tracks, deliverables, 0)).toBe('project-a · aaa ▓▓▓▓░░░░ 1/2')
    expect(await lineOf(tracks, deliverables, 1)).toBe('project-a · aaa ▓▓▓▓░░░░ 1/2 · 1 bouwer')
    expect(await lineOf(tracks, deliverables, 2)).toBe('project-a · aaa ▓▓▓▓░░░░ 1/2 · 2 bouwers')
  })

  test('cuts a track id to 28 display cells with an ellipsis', async () => {
    const long = await lineOf([trackOf('x'.repeat(40))], [], 0)
    const wide = await lineOf([trackOf('漢'.repeat(20))], [], 0)
    const cut = (line: string) => line.split(' · ')[1]?.split(':')[0] ?? ''

    expect(cut(long)).toBe(`${'x'.repeat(27)}…`)
    expect(cellsOf(cut(wide))).toBeLessThanOrEqual(28)
    expect(cut(wide).endsWith('…')).toBe(true)
  })

  test('adds verouderd for an old index or state, and onleesbaar for a part that could not be read', async () => {
    const old = await healthy({ ...healthyFiles('project-a'), [stateOf('project-a')]: stateJson({ ageMinutes: 90 }) })
    const files = healthyFiles('project-a')
    delete files[specOf('project-a', 'd-live-1')]
    const broken = await healthy(files)
    const both = await healthy({ ...files, [indexOf('project-a')]: indexJson({ ageMinutes: 90 }) })

    expect(statusLine(modelOf(old))).toBe(`${H} · verouderd`)
    expect(statusLine(modelOf(broken))).toBe('project-a · kosten: geen plan · viaref: geen plan · 1 bouwer · onleesbaar')
    expect(statusLine(modelOf(both))).toContain(' · verouderd · onleesbaar')
  })

  test('leaves the bouwers out, not 0, when the live work could not be read', async () => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: { available: false, reason: 'x' }, outcomes: { available: false } }), [stateOf('project-a')]: '{' }
    const project = await collectProject(ioOf(files, [], () => ({ exitCode: 1, stdout: '' })), 'project-a', ROOT, BIN, NOW)

    expect(statusLine(modelOf(project))).toBe('project-a · tracks onbekend · onleesbaar')
  })

  test('counts a project without a store as unreadable with unknown tracks', () => {
    expect(statusLine(modelOf({ id: 'project-a', hasStore: false, reason: 'geen store' }))).toBe('project-a · tracks onbekend · onleesbaar')
  })

  test('no longer carries the PR count, te promoveren or uitkomsten', async () => {
    const line = statusLine(modelOf(await healthy()))

    expect(line).not.toMatch(/PR|promoveren|uitkomsten/)
  })

  test('counts builders from counts.live + counts.starting, not from the capped list', async () => {
    const project = await healthy({ ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: producerLiveWork(9, 0) }) })

    expect(project.hasStore && project.builders.isOk && project.builders.value.length).toBe(8)
    expect(project.hasStore && project.buildersOmitted).toBe(1)
    expect(builderCountOf(project)).toBe(9)
    expect(statusLine(modelOf(project))).toContain('9 bouwers')
  })
})

describe('the deliverables `bezig:` names', () => {
  const trackOf = (track_id: string, horizon: string) => ({ track_id, title: 'T', phase: 'queued', derived_status: 'queued', horizon, priority: 'P2', pr_ref: null })
  const itemOf = (track: string, id: string, derived_status: string, title: string) => ({ deliverable_ref: `pr:${id}`, track, derived_status, last_activity: stampMinutesAgo(60), title })
  const projectWith = async (tracks: unknown[], items: unknown[]) =>
    collectProject(ioOf(healthyFiles('project-a'), [], argv => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : items) })), 'project-a', ROOT, BIN, NOW)

  test('are the same set the PR rule uses: a ready deliverable on a later track counts, whatever the horizon', async () => {
    const project = await projectWith([trackOf('now-t', 'now'), trackOf('later-t', 'later')], [itemOf('now-t', 'dlv-n-1', 'ready', 'N1: ready'), itemOf('later-t', 'dlv-l-1', 'ready', 'L1: ready'), itemOf('later-t', 'dlv-l-2', 'proposed', 'L2: proposed')])
    const shown = shownTracksOf(project)?.find(one => one.track.id === 'later-t')

    expect(shown?.busy).toEqual(['L1'])
    expect([...busyDeliverableIdsOf(project)].sort()).toEqual(['dlv-l-1', 'dlv-n-1'])
  })

  test('draw the id like a title when the title is empty: control characters become spaces', async () => {
    const project = await projectWith([trackOf('now-t', 'now')], [itemOf('now-t', 'dlv\u0007x\ny', 'ready', '')])

    expect(shownTracksOf(project)?.[0]?.busy).toEqual(['dlv x y'])
  })
})

describe('the order of the tracks: what is being built comes first', () => {
  const trackOf = (track_id: string, priority: string, horizon: string, pr_ref: string | null = null) => ({ track_id, title: 'T', phase: 'queued', derived_status: 'queued', horizon, priority, pr_ref })
  const recent = (track: string) => ({ deliverable_ref: `${track}-0`, track, derived_status: 'done', last_activity: stampMinutesAgo(2 * 24 * 60), title: 'T' })
  const liveOf = (ids: string[]) => ({
    available: true,
    counts: { live: ids.length, starting: 0, stale: 0, unmeasured: 0 },
    live: ids.map(dispatch_id => ({ dispatch_id, state: 'running', age_seconds: 600, pr: null })),
    stale: [],
    unmeasured: [],
    open_prs: [{ number: 2063, dispatch_id: 'd-pr-1' }],
  })
  /** A project over `tracks` and `deliverables`, with one live dispatch per entry of `builders` (its track id). */
  const projectOf = async (tracks: unknown[], deliverables: unknown[], builders: string[]) => {
    const ids = builders.map((_, at) => `d-b-${at}`)
    const files: Record<string, string> = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: liveOf(ids) }) }
    delete files[specOf('project-a', 'd-live-1')]
    builders.forEach((track_id, at) => {
      const id = `d-b-${at}`
      files[specOf('project-a', id)] = JSON.stringify({ model: 'sonnet', track_id })
      files[instructionOf('project-a', id)] = instructionText('Task')
    })
    const run = (argv: readonly string[]) => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : deliverables) })

    return collectProject(ioOf(files, [], run), 'project-a', ROOT, BIN, NOW)
  }
  const idsOf = (project: ProjectModel) => shownTracksOf(project)?.map(one => one.track.id)

  test('a builder on a P2 track ranks above a P1 track with only a recent deliverable, in the pane order and the status line', async () => {
    const project = await projectOf([trackOf('recent-p1', 'P1', 'next'), trackOf('bld', 'P2', 'next')], [recent('recent-p1')], ['bld'])

    expect(idsOf(project)).toEqual(['bld', 'recent-p1'])
    expect(statusLine(modelOf(project))).toBe('project-a · bld: geen plan · recent-p1 ▓▓▓▓▓▓▓▓ 1/1 · 1 bouwer')
  })

  test('an open PR on a P3 track ranks above a P1 track with only a recent deliverable', async () => {
    const project = await projectOf([trackOf('recent-p1', 'P1', 'next'), trackOf('with-pr', 'P3', 'later', '#2063')], [recent('recent-p1')], [])

    expect(idsOf(project)).toEqual(['with-pr', 'recent-p1'])
    expect(statusLine(modelOf(project))).toBe('project-a · with-pr: geen plan · recent-p1 ▓▓▓▓▓▓▓▓ 1/1')
  })

  test('two tracks with builders order by builder count, whatever their priority', async () => {
    const project = await projectOf([trackOf('one', 'P1', 'next'), trackOf('two', 'P3', 'next')], [], ['one', 'two', 'two'])

    expect(idsOf(project)).toEqual(['two', 'one'])
    expect(statusLine(modelOf(project))).toBe('project-a · two: geen plan · one: geen plan · 3 bouwers')
  })

  test('a now track without activity comes last', async () => {
    const project = await projectOf([trackOf('idle', 'P1', 'now'), trackOf('recent-p3', 'P3', 'later'), trackOf('with-pr', 'P3', 'later', '#2063'), trackOf('bld', 'P3', 'later')], [recent('recent-p3')], ['bld'])

    expect(idsOf(project)).toEqual(['bld', 'with-pr', 'recent-p3', 'idle'])
  })
})

describe('the open PRs of a track', () => {
  const prsOf = async (files: Record<string, string>, id = 'ci-profile') => shownTracksOf(await healthy(files))?.find(one => one.track.id === id)?.prs
  const emptyIndex = indexJson({ liveWork: { ...LIVE_WORK, open_prs: [] } })

  test('are ? and never dicht when the index lists none but the PR queue cannot be read', async () => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: emptyIndex, [stateOf('project-a')]: '{' }

    expect(await prsOf(files)).toEqual([{ number: 2001, isOpen: null }, { number: 2002, isOpen: null }])
  })

  test('are ? and never dicht when the PR queue says available: false', async () => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: emptyIndex, [stateOf('project-a')]: stateJson({ prQueue: { available: false, reason: 'gh weg' } }) }

    expect(await prsOf(files)).toEqual([{ number: 2001, isOpen: null }, { number: 2002, isOpen: null }])
  })

  test('are dicht only when the PR queue is readable and neither it nor the index lists them: an index PR the queue misses stays open', async () => {
    const live = { ...LIVE_WORK, open_prs: [{ number: 2001, dispatch_id: 'd-x' }] }
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: live }), [stateOf('project-a')]: stateJson({ prQueue: { available: true, open_prs: [] } }) }

    expect(await prsOf(files)).toEqual([{ number: 2001, isOpen: true }, { number: 2002, isOpen: false }])
  })

  test('are open when an index entry without a dispatch_id lists them, and link no track', async () => {
    const live = { ...LIVE_WORK, open_prs: [{ number: 2001, dispatch_id: null }, { number: 2002 }] }
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork: live }), [stateOf('project-a')]: '{' }
    const project = await healthy(files)
    const shown = shownTracksOf(project) ?? []

    expect(await prsOf(files)).toEqual([{ number: 2001, isOpen: true }, { number: 2002, isOpen: true }])
    expect(shown.find(one => one.track.id === 'ci-profile')?.hasActivity).toBe(true)
    expect(project.hasStore && project.prTracks).toEqual({})
  })

  test('link their track from a spec that has a track_id and no model', async () => {
    const files = { ...healthyFiles('project-a'), [specOf('project-a', 'd-pr-1')]: JSON.stringify({ track_id: 'kosten' }) }
    const project = await healthy(files)

    expect(project.hasStore && project.prTracks).toEqual({ 2063: 'kosten' })
    expect(shownTracksOf(project)?.find(one => one.track.id === 'kosten')?.prs).toEqual([{ number: 2063, isOpen: true }])
  })

  test('give a builder whose spec has a track_id and no model that track', async () => {
    const files = { ...healthyFiles('project-a'), [specOf('project-a', 'd-live-1')]: JSON.stringify({ track_id: 'bld', pr_id: 7 }) }
    const project = await healthy(files)

    expect(project.hasStore && project.builders.isOk && project.builders.value[0]?.who).toEqual({ isOk: true, value: { model: null, trackId: 'bld', prId: '7' } })
    expect(shownTracksOf(project)?.find(one => one.track.id === 'bld')?.builders).toBe(1)
    expect(isUnreadable(project)).toBe(false)
  })

  test('note under Tracks that recent deliverables are not seen when the deliverables could not be read', async () => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(TRACKS) } : { exitCode: 2, stdout: '' })
    const failed = await collectProject(ioOf(healthyFiles('project-a'), [], run), 'project-a', ROOT, BIN, NOW)

    expect(deliverablesNotice(failed)).toBe('leveringen onleesbaar: vnx exit 2, tracks met alleen recente leveringen ontbreken')
    expect(deliverablesNotice(await healthy())).toBeNull()
    expect(summaryLines(modelOf(failed))[1]).toBe('leveringen onleesbaar: vnx exit 2, tracks met alleen recente leveringen ontbreken')
  })
})

describe('the headless summary', () => {
  test('is the status line, then one line per shown track', async () => {
    expect(summaryLines(modelOf(await healthy()))).toEqual([
      'project-a · bld: geen plan · kosten: geen plan · 1 bouwer',
      'P2 bld · geen leveringen gepland',
      'P1 kosten · geen leveringen gepland',
      'P2 viaref · geen leveringen gepland',
      'P3 fresh · 1 van 1 klaar',
      'P1 ci-profile · 5 van 8 klaar',
      'P2 docs · 0 van 1 klaar',
    ])
  })

  test('is the message when there is no project, and short when the pane is placed', async () => {
    const none: Model = { collectedAt: NOW, noProject: 'geen VNX-project in deze map (geen .vnx-project-id boven /x)', project: null }

    expect(commandText(none, 'geen scherm')).toBe(none.noProject ?? '')
    expect(commandText(none, null)).toBe(`Voortgang: ${none.noProject}`)
    expect(commandText(modelOf(await healthy()), null)).toBe('Voortgang geopend: project-a · bld: geen plan · kosten: geen plan · 1 bouwer')
  })
})

describe('the builders of a project', () => {
  const builders = async (liveWork: unknown, extra: Record<string, string> = {}, asked: string[] = []) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork }), ...extra }
    const base = ioOf(files, [], healthyAnswer)
    const io: Io = { ...base, read: async path => (asked.push(path), base.read(path)) }
    const project = await collectProject(io, 'project-a', ROOT, BIN, NOW)

    return project.hasStore ? project.builders : null
  }
  const running = (counts: Record<string, number>, lists: Record<string, unknown[]>) => ({ available: true, counts: { live: 0, starting: 0, stale: 0, unmeasured: 0, ...counts }, ...lists })

  test('reads model, track and PR from the spec, the first # line and the minutes of a live dispatch', async () => {
    const result = await builders(running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 725 }] }))

    expect(result).toEqual({
      isOk: true,
      value: [
        {
          dispatchId: 'd-live-1',
          who: { isOk: true, value: { model: 'sonnet', trackId: 'bld', prId: '#2150' } },
          task: { isOk: true, value: 'Invented task: add the block' },
          minutes: { isOk: true, value: 12 },
        },
      ],
    })
  })

  test('reads a spec without track_id and pr_id as no track and no PR, and a numeric pr_id as text', async () => {
    const live = running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 60 }] })
    const bare = await builders(live, { [specOf('project-a', 'd-live-1')]: JSON.stringify({ model: 'opus', track_id: null, pr_id: '  ' }) })
    const numeric = await builders(live, { [specOf('project-a', 'd-live-1')]: JSON.stringify({ model: 'opus', pr_id: 2150 }) })

    expect(bare?.isOk && bare.value[0]?.who).toEqual({ isOk: true, value: { model: 'opus', trackId: null, prId: null } })
    expect(numeric?.isOk && numeric.value[0]?.who).toEqual({ isOk: true, value: { model: 'opus', trackId: null, prId: '2150' } })
  })

  test('takes live then starting, and no stale or unmeasured entry, and reads no file of those', async () => {
    const asked: string[] = []
    const result = await builders(
      running(
        { live: 1, starting: 1, stale: 1, unmeasured: 1 },
        {
          live: [{ dispatch_id: 'd-live-1', age_seconds: 119 }],
          starting: [{ dispatch_id: 'd-start-1', age_seconds: 5 }],
          stale: [{ dispatch_id: 'd-stale-1', age_seconds: 9000 }],
          unmeasured: [{ dispatch_id: 'd-unmeasured-1' }],
        },
      ),
      {},
      asked,
    )

    expect(result?.isOk && result.value.map(one => one.minutes)).toEqual([{ isOk: true, value: 1 }, { isOk: true, value: 0 }])
    expect(asked.some(path => path.includes('d-stale-1') || path.includes('d-unmeasured-1'))).toBe(false)
  })

  test('names the field that cannot be read, for that dispatch only', async () => {
    const files = { [instructionOf('project-a', 'd-live-1')]: 'Only a body', [specOf('project-a', 'd-live-1')]: JSON.stringify({ role: 'r' }) }
    const result = await builders(running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1' }] }), files)

    expect(result).toEqual({
      isOk: true,
      value: [{ dispatchId: 'd-live-1', who: { isOk: true, value: { model: null, trackId: null, prId: null } }, task: { isOk: false, reason: 'geen titelregel' }, minutes: { isOk: false, reason: 'age_seconds ontbreekt' } }],
    })
  })

  test('stops at the first # line and at the byte budget, and says which', async () => {
    const live = running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 0 }] })
    const early = await builders(live, { [instructionOf('project-a', 'd-live-1')]: `intro\n# First\n# Second\n${'x'.repeat(9000)}` })
    const late = await builders(live, { [instructionOf('project-a', 'd-live-1')]: `${'x'.repeat(5000)}\n# Too late` })
    const cut = await builders(live, { [instructionOf('project-a', 'd-live-1')]: `${'x'.repeat(4090)}\n# Cut in half ${'y'.repeat(100)}` })

    expect(early?.isOk && early.value[0]?.task).toEqual({ isOk: true, value: 'First' })
    expect(late?.isOk && late.value[0]?.task).toEqual({ isOk: false, reason: 'geen titelregel in de eerste 4096 tekens' })
    expect(cut?.isOk && cut.value[0]?.task.isOk).toBe(false)
  })

  test('is onbeschikbaar when the live work is, and when an entry has no dispatch_id or a list is missing', async () => {
    expect(await builders({ available: false, reason: 'lease store unreadable' })).toEqual({ isOk: false, reason: 'lease store unreadable' })
    expect(await builders(running({ live: 1 }, { live: [null] }))).toEqual({ isOk: false, reason: 'live: bouwer zonder dispatch_id' })
    expect(await builders(running({ live: 1 }, {}))).toEqual({ isOk: false, reason: 'live ontbreekt' })
    expect(await builders(running({ starting: 1 }, { live: [], starting: 'x' as unknown as unknown[] }))).toEqual({ isOk: false, reason: 'starting ontbreekt' })
    expect(await builders(running({}, {}))).toEqual({ isOk: true, value: [] })
  })

  test('takes the producer shape: one starting dispatch inside `live`, no `starting` list', async () => {
    const result = await builders(producerLiveWork(0, 1))

    expect(result?.isOk && result.value.length).toBe(1)
    expect(result?.isOk && result.value[0]?.who.isOk).toBe(true)
  })

  test('is unreadable when a field of one builder failed', async () => {
    const files = healthyFiles('project-a')
    delete files[specOf('project-a', 'd-live-1')]
    const project = await healthy(files)

    expect(project.hasStore && project.builders.isOk).toBe(true)
    expect(isUnreadable(project)).toBe(true)
  })

  test('refuses a dispatch id that is not one directory name, and reads nothing for it', async () => {
    const asked: string[] = []
    const result = await builders(running({ live: 1 }, { live: [{ dispatch_id: '../../state', age_seconds: 60 }] }), {}, asked)

    expect(result?.isOk && result.value[0]).toEqual({
      dispatchId: '../../state',
      who: { isOk: false, reason: 'ongeldige dispatch-id' },
      task: { isOk: false, reason: 'ongeldige dispatch-id' },
      minutes: { isOk: true, value: 1 },
    })
    expect(asked.some(path => path.includes('..'))).toBe(false)
  })

  test('turns the instruction text of a title into one line without control characters', async () => {
    const result = await builders(running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 0 }] }), {
      [instructionOf('project-a', 'd-live-1')]: instructionText('Title\u001b[31m red'),
    })

    expect(result?.isOk && result.value[0]?.task).toEqual({ isOk: true, value: 'Title [31m red' })
  })
})

describe('a store without runtime_coordination.db', () => {
  test('runs no vnx command and creates no file, with tracks and deliverables onbeschikbaar', async () => {
    const files = healthyFiles('project-a')
    delete files[`${storeOf('project-a')}/runtime_coordination.db`]
    const before = Object.keys(files).sort()
    const asked: string[] = []
    const io = ioOf(files, [], argv => {
      asked.push(argv.join(' '))
      files[`${storeOf('project-a')}/runtime_coordination.db`] = ''

      return healthyAnswer(argv)
    })
    const project = await collectProject(io, 'project-a', ROOT, BIN, NOW)

    expect(asked).toEqual([])
    expect(Object.keys(files).sort()).toEqual(before)
    expect(project.hasStore && project.tracks).toEqual({ isOk: false, reason: 'runtime_coordination.db ontbreekt' })
    expect(project.hasStore && project.deliverables).toEqual({ isOk: false, reason: 'runtime_coordination.db ontbreekt' })
    expect(project.hasStore && project.index.isOk).toBe(true)
  })

  test('says why when the check for the database rejects', async () => {
    const base = ioOf(healthyFiles('project-a'), [], healthyAnswer)
    const io: Io = { ...base, exists: async path => (path.endsWith('runtime_coordination.db') ? Promise.reject(new Error('EIO: db')) : base.exists(path)) }
    const project = await collectProject(io, 'project-a', ROOT, BIN, NOW)

    expect(project.hasStore && project.tracks).toEqual({ isOk: false, reason: 'onbeschikbaar: EIO: db' })
  })
})

describe('cutting text', () => {
  test('cuts on display cells: wide characters count two and combining marks none', () => {
    expect(fit('日本語日本語', 7)).toBe('日本語…')
    expect(fit('日本語日本語', 8)).toBe('日本語…')
    expect(fit('日本語', 6)).toBe('日本語')
    expect(fit('éééé', 4)).toBe('éééé')
    expect(fit('ééééé', 4)).toBe('ééé…')
    expect(fit('😀😀😀', 5)).toBe('😀😀…')
    expect(fit('a日', 2)).toBe('a…')
  })

  test('counts the cells of the bar as one each', () => {
    expect(fit('▓▓▓▓▓░░░', 8)).toBe('▓▓▓▓▓░░░')
    expect(fit('▓▓▓▓▓░░░', 6)).toBe('▓▓▓▓▓…')
  })
})

describe('the short title', () => {
  test('is the title up to the first colon, or the first 40 cells, whichever is shorter', () => {
    expect(shortTitleOf('K2a-3b: replay van de spool')).toBe('K2a-3b')
    expect(shortTitleOf('Geen dubbele punt hier')).toBe('Geen dubbele punt hier')
    expect(shortTitleOf(`${'x'.repeat(60)}: staart`)).toBe(`${'x'.repeat(39)}…`)
    expect(shortTitleOf(`${'y'.repeat(60)}`)).toBe(`${'y'.repeat(39)}…`)
    expect(cellsOf(shortTitleOf('日本語'.repeat(20)))).toBeLessThanOrEqual(40)
    expect(shortTitleOf(': begint met een dubbele punt')).toBe(': begint met een dubbele punt')
  })
})

describe('the bar in two parts', () => {
  test('splits into filled and empty cells with the same rounding as the bar', () => {
    expect(barCellsOf({ items: [], total: 12, done: 3, isRecent: false })).toEqual({ filled: 2, empty: 6 })
    expect(barCellsOf({ items: [], total: 2, done: 2, isRecent: false })).toEqual({ filled: 8, empty: 0 })
  })
})

describe('a row of segments', () => {
  const line = { segments: [{ text: 'ab', tone: 'ok' as const }, { text: 'cdef', tone: 'dim' as const }, { text: 'gh', tone: 'warn' as const }] }

  test('keeps its segments when it fits, and cuts across segments with one ellipsis when it does not', () => {
    expect(fitRow(line, 8)).toBe(line)
    expect(fitRow(line, 5)).toEqual({ segments: [{ text: 'ab', tone: 'ok' }, { text: 'cd…', tone: 'dim' }] })
    expect(fitRow(line, 2)).toEqual({ segments: [{ text: 'a…', tone: 'ok' }] })
    expect(fitRow(line, 1)).toEqual({ segments: [{ text: '…', tone: 'ok' }] })
    expect(textOf(fitRow(line, 7))).toBe('abcdef…')
  })

  test('measures the width on the text, wide characters included', () => {
    const wide = { segments: [{ text: '日本', tone: 'ok' as const }, { text: '語語', tone: 'dim' as const }] }

    expect(cellsOf(textOf(fitRow(wide, 5)))).toBeLessThanOrEqual(5)
    expect(textOf(fitRow(wide, 5))).toBe('日本…')
  })
})

describe('what is going on in a track, and what comes next', () => {
  const trackOf = (project: ProjectModel, id: string) => (shownTracksOf(project) ?? []).find(one => one.track.id === id)

  test('names the ready and in_progress deliverables as bezig and the proposed ones as volgt, in the order the list gives them', async () => {
    const items = [
      { deliverable_ref: 'pr:a', track: 't', derived_status: 'proposed', title: 'P1: eerste voorgesteld' },
      { deliverable_ref: 'pr:b', track: 't', derived_status: 'ready', title: 'R1: klaar om te starten' },
      { deliverable_ref: 'pr:c', track: 't', derived_status: 'in_progress', title: 'I1: loopt' },
      { deliverable_ref: 'pr:d', track: 't', derived_status: 'proposed', title: 'P2: tweede voorgesteld' },
      { deliverable_ref: 'pr:e', track: 't', derived_status: 'done', title: 'D1: klaar' },
      { deliverable_ref: 'pr:f', track: 't', derived_status: 'failed', title: 'F1: mislukt' },
    ]
    const tracks = [{ track_id: 't', phase: 'queued', horizon: 'now', priority: 'P1', pr_ref: null }]
    const run = (argv: readonly string[]) => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : items) })
    const project = await collectProject(ioOf(healthyFiles('project-a'), [], run), 'project-a', ROOT, BIN, NOW)
    const one = trackOf(project, 't')

    expect(one?.busy).toEqual(['R1', 'I1'])
    expect(one?.upcoming).toEqual(['P1', 'P2'])
  })

  test('counts a deliverable as bezig when a builder runs with its id as dispatch id, and a builder on the track that no deliverable carries', async () => {
    const items = [
      { deliverable_ref: 'pr:d-live-1', track: 't', derived_status: 'proposed', title: 'L1: wordt gebouwd' },
      { deliverable_ref: 'pr:other', track: 't', derived_status: 'proposed', title: 'O1: wacht' },
    ]
    const tracks = [{ track_id: 't', phase: 'queued', horizon: 'now', priority: 'P1', pr_ref: null }, { track_id: 'bld', phase: 'queued', horizon: 'now', priority: 'P1', pr_ref: null }]
    const run = (argv: readonly string[]) => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : items) })
    const project = await collectProject(ioOf(healthyFiles('project-a'), [], run), 'project-a', ROOT, BIN, NOW)

    // d-live-1 is the builder of the fixture; its spec names the track `bld`, which has no deliverable carrying its id.
    expect(trackOf(project, 't')?.busy).toEqual(['L1'])
    expect(trackOf(project, 't')?.upcoming).toEqual(['O1'])
    expect(trackOf(project, 'bld')?.busy).toEqual(['Invented task'])
  })
})

describe('what waits on the operator', () => {
  const projectWith = async (tracks: unknown[], items: unknown[]) =>
    collectProject(ioOf(healthyFiles('project-a'), [], argv => ({ exitCode: 0, stdout: JSON.stringify(argv[1] === 'objective' ? tracks : items) })), 'project-a', ROOT, BIN, NOW)
  const track = (track_id: string, horizon: string | null, phase = 'queued') => ({ track_id, phase, horizon, priority: 'P2', pr_ref: null })
  const proposed = (trackId: string, n: number) => Array.from({ length: n }, (_, at) => ({ deliverable_ref: `pr:${trackId}-${at}`, track: trackId, derived_status: 'proposed', title: 'T' }))

  test('counts the proposed on now-tracks, the backlog on next and later, and nothing of a done track, a track without horizon or an unknown track', async () => {
    const project = await projectWith(
      [track('n', 'now'), track('x', 'next'), track('l', 'later'), track('o', 'next', 'done'), track('o2', 'now', 'done'), track('z', null)],
      [...proposed('n', 2), ...proposed('x', 3), ...proposed('l', 1), ...proposed('o', 4), ...proposed('o2', 5), ...proposed('z', 6), ...proposed('ghost', 7)],
    )

    expect(promotionOf(project)).toEqual({ isOk: true, value: { now: 2, backlog: 4 } })
  })

  test('is unavailable, with the reason, when the tracks or the deliverables cannot be read', async () => {
    const healthyProject = await healthy()
    const noTracks = { ...healthyProject, tracks: { isOk: false as const, reason: 'vnx exit 2' } } as ProjectModel
    const noDeliverables = { ...healthyProject, deliverables: { isOk: false as const, reason: 'vnx exit 3' } } as ProjectModel

    expect(promotionOf(noTracks)).toEqual({ isOk: false, reason: 'vnx exit 2' })
    expect(promotionOf(noDeliverables)).toEqual({ isOk: false, reason: 'vnx exit 3' })
    expect(promotionOf({ id: 'project-a', hasStore: false, reason: 'geen store' })).toEqual({ isOk: false, reason: 'geen store' })
  })

  test('counts the tracks without a horizon without the ones whose phase is done', async () => {
    const project = await projectWith([track('z', null), track('z-done', null, 'done'), track('dash-done', '-', 'done'), track('l', 'later')], [])

    expect(planOf(project)?.withoutHorizon).toBe(1)
  })

  test('plans the tracks on next and later without the done ones, and counts the tracks without a horizon', async () => {
    const project = await projectWith([track('x2', 'next'), track('x1', 'next'), track('l', 'later'), track('d', 'later', 'done'), track('n', 'now'), track('z', null), track('z2', '-')], [])
    const plan = planOf(project)

    expect(plan?.next.map(one => one.track.id)).toEqual(['x1', 'x2'])
    expect(plan?.later.map(one => one.track.id)).toEqual(['l'])
    expect(plan?.withoutHorizon).toBe(2)
    expect(planOf({ id: 'project-a', hasStore: false, reason: 'geen store' })).toBeNull()
  })
})
