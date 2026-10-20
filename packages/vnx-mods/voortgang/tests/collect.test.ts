import { describe, expect, test } from 'claude-code/testing'

import {
  ageMinutesOf,
  collectModel,
  collectProject,
  configOf,
  deliverableArgv,
  deliverableViewOf,
  expandHome,
  formatAge,
  indexViewOf,
  isOutdated,
  isProjectId,
  isStale,
  isUnreadable,
  objectiveArgv,
  parseJson,
  projectsOf,
  stateViewOf,
  statusLine,
  summaryLines,
  trackViewOf,
} from '../hooks/collect'
import type { Io } from '../hooks/collect'
import { fit, paneRows } from '../hooks/view'
import type { Model, ProjectModel } from '../types'

import {
  BIN,
  CONTENT,
  childNames,
  dbOf,
  DELIVERABLES,
  HOME,
  NOW,
  OUTCOMES,
  ROOT,
  TRACKS,
  healthyFiles,
  indexJson,
  indexOf,
  instructionOf,
  instructionText,
  specOf,
  stampMinutesAgo,
  stateJson,
  stateOf,
  storeOf,
} from './fixtures'

const json = (text: string): unknown => JSON.parse(text)

/** An in-memory `Io`: the two files of each store (a directory holding a file exists), and what `vnx` answers or throws. */
const ioOf = (
  files: Record<string, string>,
  dirs: string[],
  run: (argv: readonly string[]) => { exitCode: number; stdout: string } | Error,
  unlistable?: string,
): Io => ({
  list: async path => {
    if (unlistable !== undefined) {
      throw new Error(unlistable)
    }

    return childNames(path, files, dirs)
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
  argv[1] === 'objective'
    ? { exitCode: 0, stdout: JSON.stringify(TRACKS) }
    : { exitCode: 0, stdout: JSON.stringify(DELIVERABLES) }

describe('the options', () => {
  test('defaults to no exclusions, the data root and the vnx binary', () => {
    expect(configOf({})).toEqual({
      exclude: [],
      dataRoot: '~/.vnx-data',
      vnxBin: '~/.vnx-system/current/bin/vnx',
      refreshSeconds: 120,
    })
  })

  test('takes an exclude list with spaces and doubles, and an interval no shorter than 15 s', () => {
    expect(projectsOf(' a, b,,a ,c ')).toEqual(['a', 'b', 'c'])
    expect(configOf({ exclude: ' x, y ,x' }).exclude).toEqual(['x', 'y'])
    expect(configOf({ refresh_seconds: 2 }).refreshSeconds).toBe(15)
    expect(configOf({ refresh_seconds: 300 }).refreshSeconds).toBe(300)
  })

  test('resolves ~ from HOME, and refuses to guess without it', () => {
    expect(expandHome('~/.vnx-data', HOME)).toBe(ROOT)
    expect(expandHome('~', HOME)).toBe(HOME)
    expect(expandHome('/srv/vnx', undefined)).toBe('/srv/vnx')
    expect(expandHome('~/.vnx-data', undefined)).toBeNull()
  })

  test('accepts a project id only when it stays one directory under the data root', () => {
    expect(isProjectId('project-a')).toBe(true)
    expect(isProjectId('project-b.v2')).toBe(true)
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
})

describe('the defaults', () => {
  test('carry no project id of the operator', () => {
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
    expect(formatAge(3)).toBe('3 min')
    expect(formatAge(59)).toBe('59 min')
    expect(formatAge(120)).toBe('2 u')
    expect(formatAge(3 * 24 * 60)).toBe('3 d')
    expect(formatAge(null)).toBe('leeftijd onbekend')
  })
})

describe('t0_index.json', () => {
  test('reads the live work and the open outcomes of a healthy index', () => {
    const view = indexViewOf(json(indexJson()), NOW)

    expect(view).toEqual({
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
            liveIds: ['d-live-1'],
            running: { isOk: true, value: [{ dispatchId: 'd-live-1', ageSeconds: 725 }] },
          },
        },
        outcomes: {
          isOk: true,
          value: { total: 2, byOutcome: { reject: 0, investigate: 2, unknown: 0 }, ids: ['d-out-1', 'd-out-2'] },
        },
      },
    })
  })

  test('gives a block that says available: false its own reason, or says so', () => {
    const view = indexViewOf(
      json(indexJson({ liveWork: { available: false, reason: 'lease store unreadable' }, outcomes: { available: false } })),
      NOW,
    )

    expect(view.isOk && view.value.liveWork).toEqual({ isOk: false, reason: 'lease store unreadable' })
    expect(view.isOk && view.value.outcomes).toEqual({ isOk: false, reason: 'available: false' })
  })

  test('never reads a missing block or missing counts as zero', () => {
    const missing = indexViewOf({ timestamp: stampMinutesAgo(1) }, NOW)
    const partial = indexViewOf(
      json(indexJson({ liveWork: { available: true, counts: { live: 1 } }, outcomes: { ...OUTCOMES, total: 'x' } })),
      NOW,
    )

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
        prs: {
          isOk: true,
          value: [{ number: 2063, title: 'feat: voortgang', ci: 'pass', merge: 'blocked', passed: ['codex_gate'], owed: ['kimi_gate'] }],
        },
      },
    })
  })

  test('says why the PR queue is unavailable, and keeps a missing seat list unknown', () => {
    const offline = stateViewOf(json(stateJson({ prQueue: { available: false, reason: 'gh offline' } })), NOW)
    const noSeats = stateViewOf(json(stateJson({ prQueue: { available: true, open_prs: [{ number: 7, title: 't' }] } })), NOW)

    expect(offline.isOk && offline.value.prs).toEqual({ isOk: false, reason: 'gh offline' })
    expect(noSeats.isOk && noSeats.value.prs).toEqual({
      isOk: true,
      value: [{ number: 7, title: 't', ci: '?', merge: '?', passed: null, owed: null }],
    })
  })
})

describe('vnx objective list and deliverable list', () => {
  test('shows the now-tracks in detail and counts the other horizons', () => {
    expect(trackViewOf(TRACKS)).toEqual({
      isOk: true,
      value: {
        now: [
          { id: 'ci-profile', priority: 'P1', phase: 'queued', derivedStatus: 'in_progress' },
          { id: 'docs', priority: 'P2', phase: 'active', derivedStatus: null },
        ],
        next: 2,
        later: 3,
        unplanned: 1,
      },
    })
    expect(trackViewOf({ tracks: [] })).toEqual({ isOk: false, reason: 'vnx gaf geen lijst' })
  })

  test('counts the deliverables per track by derived_status, and every proposed one', () => {
    expect(deliverableViewOf(DELIVERABLES)).toEqual({
      isOk: true,
      value: {
        byTrack: { 'ci-profile': { proposed: 2, done: 3, failed: 1 }, docs: { ready: 1 }, l1: { proposed: 1 } },
        proposed: 3,
      },
    })
    expect(deliverableViewOf('x')).toEqual({ isOk: false, reason: 'vnx gaf geen lijst' })
  })

  test('counts a track or status named like an Object.prototype member as any other', () => {
    const result = deliverableViewOf([
      { track: '__proto__', derived_status: 'proposed' },
      { track: '__proto__', derived_status: 'proposed' },
      { track: 'constructor', derived_status: 'done' },
      { track: 'toString', derived_status: 'constructor' },
      { track: 'docs', derived_status: '__proto__' },
    ])

    expect(result.isOk).toBe(true)

    if (result.isOk) {
      const { byTrack, proposed } = result.value

      expect(proposed).toBe(2)
      expect(byTrack['__proto__']?.proposed).toBe(2)
      expect(byTrack['constructor']?.['done']).toBe(1)
      expect(byTrack['toString']?.['constructor']).toBe(1)
      expect(byTrack.docs?.['__proto__']).toBe(1)
      expect(Object.keys(byTrack).sort()).toEqual(['__proto__', 'constructor', 'docs', 'toString'])
      expect(({} as Record<string, unknown>).proposed).toBeUndefined()
    }
  })

  test('keeps an outcome named __proto__ among the open outcomes', () => {
    const block = { available: true, total: 3, by_outcome: JSON.parse('{"__proto__": 2, "constructor": 1}'), items: [] }
    const result = indexViewOf({ timestamp: stampMinutesAgo(1), live_work: null, open_outcomes: block }, NOW)

    expect(result.isOk && result.value.outcomes.isOk && Object.keys(result.value.outcomes.value.byOutcome).sort()).toEqual(['__proto__', 'constructor'])
  })
})

describe('one project', () => {
  test('reads both files and runs both commands for a healthy store', async () => {
    const project = await collectProject(ioOf(healthyFiles('project-a'), [], healthyAnswer), 'project-a', ROOT, BIN, NOW)

    expect(project.hasStore).toBe(true)
    expect(project.hasStore && project.index.isOk && project.index.value.liveWork.isOk).toBe(true)
    expect(project.hasStore && project.tracks.isOk && project.tracks.value.now.map(one => one.id)).toEqual(['ci-profile', 'docs'])
    expect(project.hasStore && project.deliverables.isOk && project.deliverables.value.proposed).toBe(3)
    expect(isUnreadable(project)).toBe(false)
    expect(isOutdated(project)).toBe(false)
  })

  test('says geen store when the store directory is missing, and runs nothing for it', async () => {
    const asked: string[] = []
    const io = ioOf({}, [], argv => {
      asked.push(argv.join(' '))

      return healthyAnswer(argv)
    })

    expect(await collectProject(io, 'project-c', ROOT, BIN, NOW)).toEqual({ id: 'project-c', hasStore: false, reason: 'geen store' })
    expect(asked).toEqual([])
  })

  test('turns a rejecting exists, read or list into onbeschikbaar for that part only', async () => {
    const base = ioOf(healthyFiles('project-a'), [], healthyAnswer)
    const noStore = await collectProject({ ...base, exists: async () => Promise.reject(new Error('EIO: store')) }, 'project-a', ROOT, BIN, NOW)

    expect(noStore).toEqual({ id: 'project-a', hasStore: false, reason: 'onbeschikbaar: EIO: store' })

    const noIndex = await collectProject(
      { ...base, exists: async path => (path.endsWith('t0_index.json') ? Promise.reject(new Error('EIO: index')) : base.exists(path)) },
      'project-a',
      ROOT,
      BIN,
      NOW,
    )

    expect(noIndex.hasStore && !noIndex.index.isOk && noIndex.index.reason).toBe('onbeschikbaar: EIO: index')
    expect(noIndex.hasStore && noIndex.state.isOk && noIndex.tracks.isOk && noIndex.deliverables.isOk).toBe(true)
  })

  test('refreshes the healthy projects when one project rejects, and when a store check rejects during discovery', async () => {
    const files = { ...healthyFiles('project-a'), ...healthyFiles('project-b'), ...healthyFiles('project-c') }
    const base = ioOf(files, [], healthyAnswer)
    const io: Io = {
      ...base,
      exists: async path => (path.includes('/project-b/') ? Promise.reject(new Error('EIO: project-b')) : base.exists(path)),
    }
    const model = await collectModel(io, configOf({}), HOME, NOW)

    expect(model.rootError).toBeNull()
    expect(model.projects.map(project => project.id)).toEqual(['project-a', 'project-b', 'project-c'])
    expect(model.projects[1]).toEqual({ id: 'project-b', hasStore: false, reason: 'onbeschikbaar: EIO: project-b' })
    expect([model.projects[0], model.projects[2]].every(project => project !== undefined && !isUnreadable(project))).toBe(true)
    expect(statusLine(model)).toBe('voortgang · 2 live · 2 PR · 4 open · 2 bouwers · 1 onleesbaar')
  })

  test('refuses a project id that would leave the data root', async () => {
    expect(await collectProject(ioOf({}, [], healthyAnswer), '../etc', ROOT, BIN, NOW)).toEqual({
      id: '../etc',
      hasStore: false,
      reason: 'ongeldige project-id',
    })
  })

  test('says which file is missing or not JSON', async () => {
    const io = ioOf({ [indexOf('project-a')]: '{"broken' }, [storeOf('project-a')], healthyAnswer)
    const project = await collectProject(io, 'project-a', ROOT, BIN, NOW)

    expect(project.hasStore && project.index).toEqual({ isOk: false, reason: 'ongeldige JSON' })
    expect(project.hasStore && project.state).toEqual({ isOk: false, reason: 't0_state.json ontbreekt' })
    expect(isUnreadable(project)).toBe(true)
  })

  test('names the exit code, a time-out, a start failure, invalid JSON and a wrong shape from vnx', async () => {
    const cases: [(argv: readonly string[]) => { exitCode: number; stdout: string } | Error, string][] = [
      [() => ({ exitCode: 1, stdout: '' }), 'vnx exit 1'],
      [() => new Error('process timed out after 10000 ms'), 'vnx time-out'],
      [() => new Error('spawn ENOENT'), 'vnx startfout: spawn ENOENT'],
      [() => ({ exitCode: 0, stdout: 'Traceback' }), 'vnx gaf ongeldige JSON'],
      [() => ({ exitCode: 0, stdout: '{}' }), 'vnx gaf geen lijst'],
    ]

    for (const [run, reason] of cases) {
      const project = await collectProject(ioOf(healthyFiles('project-a'), [], run), 'project-a', ROOT, BIN, NOW)

      expect(project.hasStore && project.tracks, reason).toEqual({ isOk: false, reason })
      expect(project.hasStore && project.deliverables, reason).toEqual({ isOk: false, reason })
      expect(project.hasStore && project.index.isOk, reason).toBe(true)
      expect(project.hasStore && project.state.isOk, reason).toBe(true)
    }
  })

  test('calls a project with a two-hour-old index outdated', async () => {
    const files = { [indexOf('project-a')]: indexJson({ ageMinutes: 120 }), [stateOf('project-a')]: stateJson(), [dbOf('project-a')]: '' }
    const project = await collectProject(ioOf(files, [], healthyAnswer), 'project-a', ROOT, BIN, NOW)

    expect(isOutdated(project)).toBe(true)
    expect(isUnreadable(project)).toBe(false)
  })
})

describe('store discovery', () => {
  const discover = (files: Record<string, string>, dirs: string[], options: Record<string, string | number | boolean> = {}, unlistable?: string) =>
    collectModel(ioOf(files, dirs, healthyAnswer, unlistable), configOf(options), HOME, NOW)

  test('finds exactly the child directories with a store, in id order', async () => {
    const files = { ...healthyFiles('project-b'), [stateOf('project-a')]: stateJson() }
    const model = await discover(files, [`${ROOT}/project-z`, `${ROOT}/project-z/logs`, `${ROOT}/project-y/state`])

    expect(model.rootError).toBeNull()
    expect(model.projects.map(project => project.id)).toEqual(['project-a', 'project-b'])
  })

  test('leaves out the excluded ids', async () => {
    const files = { ...healthyFiles('project-a'), ...healthyFiles('project-b'), ...healthyFiles('project-c') }
    const model = await discover(files, [], { exclude: 'project-b, project-x' })

    expect(model.projects.map(project => project.id)).toEqual(['project-a', 'project-c'])
  })

  test('says onbeschikbaar for the whole model when the data root cannot be listed, never an empty list', async () => {
    const model = await discover(healthyFiles('project-a'), [], {}, 'EACCES: permission denied')

    expect(model.rootError).toBe('EACCES: permission denied')
    expect(model.projects).toEqual([])
    expect(statusLine(model)).toBe('voortgang · onbeschikbaar: EACCES: permission denied')
    expect(summaryLines(model)).toEqual(['onbeschikbaar: EACCES: permission denied'])
  })

  test('shows geen projecten, not an error, for a data root that holds no store', async () => {
    const model = await discover({}, [`${ROOT}/scratch`])

    expect(model.rootError).toBeNull()
    expect(model.projects).toEqual([])
  })

  test('never lets a listed name that is not one directory name reach a path', async () => {
    const asked: string[] = []
    const io: Io = {
      list: async () => ['..', '.hidden', 'a b/c', 'project-a'],
      exists: async path => {
        asked.push(path)

        return true
      },
      read: async () => '',
      run: async () => ({ exitCode: 0, stdout: '[]', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }),
    }
    const model = await collectModel(io, configOf({}), HOME, NOW)

    expect(model.projects.map(project => project.id)).toEqual(['project-a'])
    expect(asked.every(path => path.startsWith(`${ROOT}/project-a/`))).toBe(true)
  })
})

const healthy = async (id: string): Promise<ProjectModel> =>
  collectProject(ioOf(healthyFiles(id), [], healthyAnswer), id, ROOT, BIN, NOW)

describe('the status line', () => {
  test('names one unreadable project as a count', async () => {
    const model: Model = {
      collectedAt: NOW,
      rootError: null,
      projects: [await healthy('project-a'), { id: 'project-c', hasStore: false, reason: 'geen store' }],
    }

    expect(statusLine(model)).toBe('voortgang · 1 live · 1 PR · 2 open · 1 bouwers · 1 onleesbaar')
  })

  test('adds the outdated projects, and writes ? where no project could be read', async () => {
    const stale = await collectProject(
      ioOf({ [indexOf('project-a')]: indexJson({ ageMinutes: 120 }), [stateOf('project-a')]: stateJson(), [dbOf('project-a')]: '' }, [], healthyAnswer),
      'project-a',
      ROOT,
      BIN,
      NOW,
    )

    expect(statusLine({ collectedAt: NOW, rootError: null, projects: [stale] })).toBe('voortgang · 1 live · 1 PR · 2 open · 1 bouwers · 1 verouderd')
    expect(statusLine({ collectedAt: NOW, rootError: null, projects: [{ id: 'project-c', hasStore: false, reason: 'geen store' }] })).toBe(
      'voortgang · ? live · ? PR · ? open · ? bouwers · 1 onleesbaar',
    )
    expect(statusLine({ collectedAt: NOW, rootError: 'HOME onbekend', projects: [] })).toBe('voortgang · onbeschikbaar: HOME onbekend')
    expect(statusLine({ collectedAt: NOW, rootError: null, projects: [] })).toBe('voortgang · geen projecten')
  })

  test('reads no file at all when HOME is unknown and a path starts with ~', async () => {
    const asked: string[] = []
    const io: Io = {
      list: async path => {
        asked.push(path)

        return ['project-a']
      },
      exists: async path => {
        asked.push(path)

        return true
      },
      read: async path => {
        asked.push(path)

        return ''
      },
      run: async argv => {
        asked.push(argv.join(' '))

        return { exitCode: 0, stdout: '[]', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
      },
    }
    const model = await collectModel(io, configOf({}), undefined, NOW)

    expect(model.rootError).toBe('HOME onbekend, ~ niet op te lossen')
    expect(model.projects).toEqual([])
    expect(asked).toEqual([])
  })
})

describe('the summary and the pane rows', () => {
  test('sums each project up in one line of ids and counts', async () => {
    const model: Model = {
      collectedAt: NOW,
      rootError: null,
      projects: [await healthy('project-a'), { id: 'project-c', hasStore: false, reason: 'geen store' }],
    }

    expect(summaryLines(model)).toEqual([
      'project-a: 1 live · 1 PR · 2 open · now ci-profile, docs · next 2 · later 3 · 1 bouwers · 3 te promoveren',
      'project-c: geen store',
    ])
  })

  test('cuts a text to the width with an ellipsis', () => {
    expect(fit('abcdef', 6)).toBe('abcdef')
    expect(fit('abcdefg', 6)).toBe('abcde…')
    expect(fit('abc', 1)).toBe('…')
  })

  test('fits every row to the pane, shows no content, and cuts a long list with +N meer', async () => {
    const many = Array.from({ length: 12 }, (_, n) => ({ track_id: `t${n}`, priority: 'P1', phase: 'queued', derived_status: 'queued', horizon: 'now' }))
    const io = ioOf(healthyFiles('project-a'), [], argv =>
      argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(many) } : { exitCode: 0, stdout: '[]' },
    )
    const model = await collectModel(io, configOf({}), HOME, NOW)
    const view = paneRows(model, { columns: 30, headerColumns: 17, rows: 12, isRefreshing: false })
    const texts = view.lines.map(line => line.text)

    expect(texts.every(text => Array.from(text).length <= 30)).toBe(true)
    expect(Array.from(view.header).length).toBeLessThanOrEqual(17)
    expect(texts.some(text => /^ {2}\+\d+ meer$/.test(text))).toBe(true)
    expect(texts.join('\n')).not.toContain(CONTENT)
  })
})

describe('malformed entries in a vnx list', () => {
  test('make the track part onbeschikbaar instead of dropping the entry', () => {
    expect(trackViewOf([null])).toEqual({ isOk: false, reason: 'vnx gaf geen object op plaats 1' })
    expect(trackViewOf([{ track_id: 'a', horizon: 'now' }, 'x'])).toEqual({ isOk: false, reason: 'vnx gaf geen object op plaats 2' })
    expect(trackViewOf([[]]).isOk).toBe(false)
    expect(trackViewOf([]).isOk).toBe(true)
  })

  test('make the deliverable part onbeschikbaar instead of dropping the entry', () => {
    expect(deliverableViewOf([null])).toEqual({ isOk: false, reason: 'vnx gaf geen object op plaats 1' })
    expect(deliverableViewOf([{ track: 'a', derived_status: 'done' }, 3])).toEqual({ isOk: false, reason: 'vnx gaf geen object op plaats 2' })
  })

  test('show up in a project as onbeschikbaar for that part only', async () => {
    const run = (argv: readonly string[]) => (argv[1] === 'objective' ? { exitCode: 0, stdout: '[null]' } : healthyAnswer(argv))
    const project = await collectProject(ioOf(healthyFiles('project-a'), [], run), 'project-a', ROOT, BIN, NOW)

    expect(project.hasStore && project.tracks).toEqual({ isOk: false, reason: 'vnx gaf geen object op plaats 1' })
    expect(project.hasStore && project.deliverables.isOk).toBe(true)
    expect(isUnreadable(project)).toBe(true)
  })
})

describe('the builders of a project', () => {
  const builders = async (liveWork: unknown, extra: Record<string, string> = {}, asked: string[] = []) => {
    const files = { ...healthyFiles('project-a'), [indexOf('project-a')]: indexJson({ liveWork }), ...extra }
    const base = ioOf(files, [], healthyAnswer)
    const io: Io = {
      ...base,
      read: async path => {
        asked.push(path)

        return base.read(path)
      },
    }
    const project = await collectProject(io, 'project-a', ROOT, BIN, NOW)

    return project.hasStore ? project.builders : null
  }
  const running = (counts: Record<string, number>, lists: Record<string, unknown[]>) => ({ available: true, counts: { live: 0, starting: 0, stale: 0, unmeasured: 0, ...counts }, ...lists })

  test('reads role and model, the first # line and the minutes of a live dispatch', async () => {
    const result = await builders(running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 725 }] }))

    expect(result).toEqual({
      isOk: true,
      value: [{ who: { isOk: true, value: { role: 'backend-developer', model: 'sonnet' } }, task: { isOk: true, value: 'Invented task: add the block' }, minutes: { isOk: true, value: 12 } }],
    })
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
      value: [
        {
          who: { isOk: false, reason: 'model ontbreekt' },
          task: { isOk: false, reason: 'geen titelregel' },
          minutes: { isOk: false, reason: 'age_seconds ontbreekt' },
        },
      ],
    })
  })

  test('stops at the first # line and at the byte budget, and says which', async () => {
    const early = await builders(running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 0 }] }), {
      [instructionOf('project-a', 'd-live-1')]: `intro\n# First\n# Second\n${'x'.repeat(9000)}`,
    })
    const late = await builders(running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 0 }] }), {
      [instructionOf('project-a', 'd-live-1')]: `${'x'.repeat(5000)}\n# Too late`,
    })
    const cut = await builders(running({ live: 1 }, { live: [{ dispatch_id: 'd-live-1', age_seconds: 0 }] }), {
      [instructionOf('project-a', 'd-live-1')]: `${'x'.repeat(4090)}\n# Cut in half ${'y'.repeat(100)}`,
    })

    expect(early?.isOk && early.value[0]?.task).toEqual({ isOk: true, value: 'First' })
    expect(late?.isOk && late.value[0]?.task).toEqual({ isOk: false, reason: 'geen titelregel in de eerste 4096 tekens' })
    expect(cut?.isOk && cut.value[0]?.task.isOk).toBe(false)
  })

  test('is onbeschikbaar when the live work is, and when an entry has no dispatch_id or a list is missing', async () => {
    expect(await builders({ available: false, reason: 'lease store unreadable' })).toEqual({ isOk: false, reason: 'lease store unreadable' })
    expect(await builders(running({ live: 1 }, { live: [null] }))).toEqual({ isOk: false, reason: 'live: bouwer zonder dispatch_id' })
    expect(await builders(running({ live: 1 }, {}))).toEqual({ isOk: false, reason: 'live ontbreekt' })
    expect(await builders(running({ starting: 1 }, { live: [] }))).toEqual({ isOk: false, reason: 'starting ontbreekt' })
    expect(await builders(running({}, {}))).toEqual({ isOk: true, value: [] })
  })

  test('refuses a dispatch id that is not one directory name, and reads nothing for it', async () => {
    const asked: string[] = []
    const result = await builders(running({ live: 1 }, { live: [{ dispatch_id: '../../state', age_seconds: 60 }] }), {}, asked)

    expect(result?.isOk && result.value[0]).toEqual({
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

describe('the pane header and rows', () => {
  const size = { columns: 40, headerColumns: 27, rows: 30, isRefreshing: false }

  test('says the count is unknown when discovery failed', () => {
    const view = paneRows({ collectedAt: NOW, rootError: 'EACCES', projects: [] }, size)

    expect(view.header).toBe('Voortgang · aantal onbekend')
    expect(paneRows({ collectedAt: NOW, rootError: null, projects: [] }, { ...size, columns: 100 }).lines[0]?.text).toContain('exclude, data_root')
  })

  test('cuts on display cells: wide characters count two and combining marks none', () => {
    expect(fit('日本語日本語', 7)).toBe('日本語…')
    expect(fit('日本語日本語', 8)).toBe('日本語…')
    expect(fit('日本語', 6)).toBe('日本語')
    expect(fit('e\u0301e\u0301e\u0301e\u0301', 4)).toBe('e\u0301e\u0301e\u0301e\u0301')
    expect(fit('e\u0301e\u0301e\u0301e\u0301e\u0301', 4)).toBe('e\u0301e\u0301e\u0301…')
    expect(fit('😀😀😀', 5)).toBe('😀😀…')
    expect(fit('a日', 2)).toBe('a…')
  })
})
