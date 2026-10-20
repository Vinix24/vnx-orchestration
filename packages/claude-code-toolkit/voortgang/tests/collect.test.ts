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
  DELIVERABLES,
  HOME,
  NOW,
  OUTCOMES,
  ROOT,
  TRACKS,
  healthyFiles,
  indexJson,
  indexOf,
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
): Io => ({
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
  test('defaults to the four projects, the data root and the vnx binary', () => {
    expect(configOf({})).toEqual({
      projects: ['vnx-dev', 'mission-control', 'seocrawler-v2', 'sales-copilot'],
      dataRoot: '~/.vnx-data',
      vnxBin: '~/.vnx-system/current/bin/vnx',
      refreshSeconds: 120,
    })
  })

  test('takes a project list with spaces and doubles, and an interval no shorter than 15 s', () => {
    expect(projectsOf(' a, b,,a ,c ')).toEqual(['a', 'b', 'c'])
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
    expect(isProjectId('vnx-dev')).toBe(true)
    expect(isProjectId('seocrawler-v2')).toBe(true)
    expect(isProjectId('..')).toBe(false)
    expect(isProjectId('../etc')).toBe(false)
    expect(isProjectId('a/b')).toBe(false)
    expect(isProjectId('')).toBe(false)
  })

  test('builds the two vnx argv shapes with --state-dir', () => {
    const store = storeOf('alpha')

    expect(objectiveArgv(BIN, 'alpha', store)).toEqual([BIN, 'objective', 'list', '--json', '--project-id', 'alpha', '--state-dir', store])
    expect(deliverableArgv(BIN, 'alpha', store)).toEqual([BIN, 'deliverable', 'list', '--json', '--project-id', 'alpha', '--state-dir', store])
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
        liveWork: { isOk: true, value: { live: 1, starting: 0, stale: 4, unmeasured: 0, liveIds: ['d-live-1'] } },
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
})

describe('one project', () => {
  test('reads both files and runs both commands for a healthy store', async () => {
    const project = await collectProject(ioOf(healthyFiles('alpha'), [], healthyAnswer), 'alpha', ROOT, BIN, NOW)

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

    expect(await collectProject(io, 'ghost', ROOT, BIN, NOW)).toEqual({ id: 'ghost', hasStore: false, reason: 'geen store' })
    expect(asked).toEqual([])
  })

  test('refuses a project id that would leave the data root', async () => {
    expect(await collectProject(ioOf({}, [], healthyAnswer), '../etc', ROOT, BIN, NOW)).toEqual({
      id: '../etc',
      hasStore: false,
      reason: 'ongeldige project-id',
    })
  })

  test('says which file is missing or not JSON', async () => {
    const io = ioOf({ [indexOf('alpha')]: '{"broken' }, [storeOf('alpha')], healthyAnswer)
    const project = await collectProject(io, 'alpha', ROOT, BIN, NOW)

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
      const project = await collectProject(ioOf(healthyFiles('alpha'), [], run), 'alpha', ROOT, BIN, NOW)

      expect(project.hasStore && project.tracks, reason).toEqual({ isOk: false, reason })
      expect(project.hasStore && project.deliverables, reason).toEqual({ isOk: false, reason })
      expect(project.hasStore && project.index.isOk, reason).toBe(true)
      expect(project.hasStore && project.state.isOk, reason).toBe(true)
    }
  })

  test('calls a project with a two-hour-old index outdated', async () => {
    const files = { [indexOf('alpha')]: indexJson({ ageMinutes: 120 }), [stateOf('alpha')]: stateJson() }
    const project = await collectProject(ioOf(files, [], healthyAnswer), 'alpha', ROOT, BIN, NOW)

    expect(isOutdated(project)).toBe(true)
    expect(isUnreadable(project)).toBe(false)
  })
})

const healthy = async (id: string): Promise<ProjectModel> =>
  collectProject(ioOf(healthyFiles(id), [], healthyAnswer), id, ROOT, BIN, NOW)

describe('the status line', () => {
  test('names one unreadable project as a count', async () => {
    const model: Model = {
      collectedAt: NOW,
      rootError: null,
      projects: [await healthy('alpha'), { id: 'ghost', hasStore: false, reason: 'geen store' }],
    }

    expect(statusLine(model)).toBe('voortgang · 1 live · 1 PR · 2 open · 1 onleesbaar')
  })

  test('adds the outdated projects, and writes ? where no project could be read', async () => {
    const stale = await collectProject(
      ioOf({ [indexOf('alpha')]: indexJson({ ageMinutes: 120 }), [stateOf('alpha')]: stateJson() }, [], healthyAnswer),
      'alpha',
      ROOT,
      BIN,
      NOW,
    )

    expect(statusLine({ collectedAt: NOW, rootError: null, projects: [stale] })).toBe('voortgang · 1 live · 1 PR · 2 open · 1 verouderd')
    expect(statusLine({ collectedAt: NOW, rootError: null, projects: [{ id: 'ghost', hasStore: false, reason: 'geen store' }] })).toBe(
      'voortgang · ? live · ? PR · ? open · 1 onleesbaar',
    )
    expect(statusLine({ collectedAt: NOW, rootError: 'HOME onbekend', projects: [] })).toBe('voortgang · onbeschikbaar: HOME onbekend')
    expect(statusLine({ collectedAt: NOW, rootError: null, projects: [] })).toBe('voortgang · geen projecten')
  })

  test('reads no file at all when HOME is unknown and a path starts with ~', async () => {
    const asked: string[] = []
    const io: Io = {
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
    const model = await collectModel(io, configOf({ projects: 'alpha' }), undefined, NOW)

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
      projects: [await healthy('alpha'), { id: 'ghost', hasStore: false, reason: 'geen store' }],
    }

    expect(summaryLines(model)).toEqual([
      'alpha: 1 live · 1 PR · 2 open · now ci-profile, docs · next 2 · later 3 · 3 te promoveren',
      'ghost: geen store',
    ])
  })

  test('cuts a text to the width with an ellipsis', () => {
    expect(fit('abcdef', 6)).toBe('abcdef')
    expect(fit('abcdefg', 6)).toBe('abcde…')
    expect(fit('abc', 1)).toBe('…')
  })

  test('fits every row to the pane, shows no content, and cuts a long list with +N meer', async () => {
    const many = Array.from({ length: 12 }, (_, n) => ({ track_id: `t${n}`, priority: 'P1', phase: 'queued', derived_status: 'queued', horizon: 'now' }))
    const io = ioOf(healthyFiles('alpha'), [], argv =>
      argv[1] === 'objective' ? { exitCode: 0, stdout: JSON.stringify(many) } : { exitCode: 0, stdout: '[]' },
    )
    const model = await collectModel(io, configOf({ projects: 'alpha' }), HOME, NOW)
    const view = paneRows(model, { columns: 30, headerColumns: 17, rows: 12, isRefreshing: false })
    const texts = view.lines.map(line => line.text)

    expect(texts.every(text => Array.from(text).length <= 30)).toBe(true)
    expect(Array.from(view.header).length).toBeLessThanOrEqual(17)
    expect(texts.some(text => /^ {2}\+\d+ meer$/.test(text))).toBe(true)
    expect(texts.join('\n')).not.toContain(CONTENT)
  })
})
