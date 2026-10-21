import type { On, SessionStartInput } from 'claude-code'

export const PLUGIN = 'voortgang'
export const HOME = '/home/op'
export const ROOT = `${HOME}/.vnx-data`
export const BIN = `${HOME}/.vnx-system/current/bin/vnx`
export const NOW = Date.parse('2026-10-20T16:00:00.000Z')

/** The working directory of a session, two levels below the directory that holds `.vnx-project-id`. */
export const CWD = '/work/project-a/src/deep'
export const PROJECT_DIR = '/work/project-a'
export const projectIdFileOf = (dir: string) => `${dir}/.vnx-project-id`

export const storeOf = (project: string) => `${ROOT}/${project}/state`
export const indexOf = (project: string) => `${storeOf(project)}/t0_index.json`
export const stateOf = (project: string) => `${storeOf(project)}/t0_state.json`
export const dbOf = (project: string) => `${storeOf(project)}/runtime_coordination.db`
export const dispatchOf = (project: string, dispatch: string) => `${ROOT}/${project}/dispatches/pending/${dispatch}`
export const specOf = (project: string, dispatch: string) => `${dispatchOf(project, dispatch)}/dispatch-spec.json`
export const instructionOf = (project: string, dispatch: string) => `${dispatchOf(project, dispatch)}/instruction.md`

/** A timestamp `minutes` before NOW, spelled as build_t0_state writes it: microseconds and `+00:00`. */
export const stampMinutesAgo = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString().replace('Z', '000+00:00')

/** Text that stands for what the mod must never show: a body, a reason, an item's content. */
export const CONTENT = 'KLANTINHOUD'

export const LIVE_WORK = {
  available: true,
  counts: { live: 1, starting: 0, stale: 4, unmeasured: 0 },
  live: [{ dispatch_id: 'd-live-1', state: 'running', age_seconds: 725, pr: null }],
  stale: [{ dispatch_id: 'd-stale-1', state: 'stale', age_seconds: 9000, lock: 'x' }],
  unmeasured: [],
  open_prs: [{ number: 2063, dispatch_id: 'd-pr-1' }],
}

/**
 * `live_work` as `_live_work_index_summary` writes it: `live` holds the live and the starting entries together, there
 * is no `starting` list, the list is capped at eight and `counts` keeps the totals.
 */
export const producerLiveWork = (live: number, starting: number, listed = Math.min(live + starting, 8)) => ({
  available: true,
  counts: { live, starting, stale: 0, unmeasured: 0 },
  live: Array.from({ length: listed }, () => ({ dispatch_id: 'd-live-1', state: starting > 0 ? 'starting' : 'running', age_seconds: 725, pr: null })),
  stale: [],
  unmeasured: [],
  open_prs: [],
})

export const OUTCOMES = {
  available: true,
  total: 2,
  by_outcome: { reject: 0, investigate: 2, unknown: 0 },
  by_kind: {},
  more: 0,
  items: [
    { dispatch_id: 'd-out-1', outcome: 'investigate', reason: CONTENT },
    { dispatch_id: 'd-out-2', outcome: 'investigate', reason: CONTENT },
  ],
}

export const PR_QUEUE = {
  schema: 'pr_queue/1',
  available: true,
  reason: null,
  open_prs: [
    {
      number: 2063,
      title: 'feat: voortgang',
      branch: 'dispatch/x',
      state: 'OPEN',
      ci_status: 'pass',
      merge_state: 'blocked',
      gates_passed: ['codex_gate'],
      blocked_on: ['kimi_gate'],
    },
  ],
}

export function indexJson(
  options: { ageMinutes?: number; liveWork?: unknown; outcomes?: unknown } = {},
): string {
  return JSON.stringify({
    schema: 't0_index/1',
    timestamp: stampMinutesAgo(options.ageMinutes ?? 3),
    git_branch: 'main',
    live_work: options.liveWork ?? LIVE_WORK,
    recent_receipts: [{ body: CONTENT }],
    open_outcomes: options.outcomes ?? OUTCOMES,
  })
}

export function stateJson(options: { ageMinutes?: number; prQueue?: unknown } = {}): string {
  return JSON.stringify({
    schema_version: 2,
    generated_at: stampMinutesAgo(options.ageMinutes ?? 3),
    open_items: [{ id: 'OI-1', body: CONTENT }],
    pr_queue: options.prQueue ?? PR_QUEUE,
  })
}

const track = (track_id: string, horizon: string | null, priority: string | null = 'P2', pr_ref: string | null = null) => ({
  track_id,
  title: CONTENT,
  phase: 'queued',
  derived_status: 'queued',
  horizon,
  priority,
  pr_ref,
})

/** now: ci-profile, docs. Activity: bld (a builder), kosten (the spec of PR 2063's dispatch), viaref (pr_ref lists 2063), fresh (a recent deliverable). */
export const TRACKS = [
  track('ci-profile', 'now', 'P1', '#2001,#2002'),
  track('docs', 'now', 'P2'),
  track('fresh', 'later', 'P3'),
  track('viaref', 'next', 'P2', '#2063'),
  track('kosten', 'next', 'P1'),
  track('bld', 'later', 'P2'),
  track('old-d', 'later', 'P2'),
  track('n1', 'next'),
  track('l1', 'later'),
  track('u1', null, null),
]

const deliverable = (trackId: string, derived_status: string, daysAgo = 30) => ({
  deliverable_ref: `${trackId}-${derived_status}-${daysAgo}`,
  track: trackId,
  derived_status,
  last_activity: stampMinutesAgo(daysAgo * 24 * 60),
  title: CONTENT,
})

export const DELIVERABLES = [
  deliverable('ci-profile', 'proposed'),
  deliverable('ci-profile', 'proposed'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'failed'),
  deliverable('docs', 'ready'),
  deliverable('fresh', 'done', 2),
  deliverable('old-d', 'done', 8),
  deliverable('l1', 'proposed'),
]

export const SPEC = { role: 'backend-developer', model: 'sonnet', provider: 'claude', track_id: 'bld', pr_id: '#2150', instruction_file: CONTENT }
/** The spec of the dispatch behind PR 2063: it names the track `kosten`. */
export const PR_SPEC = { role: 'backend-developer', model: 'opus', track_id: 'kosten', pr_id: null }

/** An instruction whose first `# ` line is the task; the lines around it must never reach the pane. */
export const instructionText = (title: string) => `Preface line ${CONTENT}\n## Not a title\n# ${title}\n\nBody ${CONTENT}\n# Second title ${CONTENT}\n`

/** The files of a healthy store: the two state files, the database, and the spec and instruction of its one builder. */
export const healthyFiles = (project: string): Record<string, string> => ({
  [projectIdFileOf(PROJECT_DIR)]: `${project}\n`,
  [specOf(project, 'd-pr-1')]: JSON.stringify(PR_SPEC),
  [indexOf(project)]: indexJson(),
  [stateOf(project)]: stateJson(),
  [dbOf(project)]: '',
  [specOf(project, 'd-live-1')]: JSON.stringify(SPEC),
  [instructionOf(project, 'd-live-1')]: instructionText('Invented task: add the block'),
})

export type RunAnswer = { exitCode: number; stdout: string } | { deny: string }

/** What `vnx` answers for a healthy store: the tracks, or the deliverables. */
export const healthyRun = (argv: readonly string[]): RunAnswer => {
  if (argv[1] === 'objective') {
    return { exitCode: 0, stdout: JSON.stringify(TRACKS) }
  }

  if (argv[1] === 'deliverable') {
    return { exitCode: 0, stdout: JSON.stringify(DELIVERABLES) }
  }

  return { deny: `unexpected argv ${argv.join(' ')}` }
}

export type World = {
  reads: string[]
  exists: string[]
  runs: { argv: string[]; cwd: string | undefined; timeoutMs: number | undefined }[]
  statuses: (string | undefined)[]
  opened: { id: string; title: string | undefined }[]
  commands: string[]
  /** Every call the mod must never make: a write, a listing, a stat, the store. */
  forbidden: string[]
}

export type WorldSetup = {
  files: Record<string, string>
  /** Store directories that exist without the files in them. */
  dirs?: string[]
  run?: (argv: readonly string[]) => RunAnswer
  env?: Record<string, string>
  /** The reason `$.ui.open` gives when it does not place the pane; placed when absent. */
  unplaced?: string
  /** The reason reading the environment is denied, which makes a refresh reject; reads fine when absent. */
  envDeny?: string
}

/** The engine beneath the mod, in memory: the file tree, `vnx`, the display, and a record of each call. */
export function worldOf(on: On, setup: WorldSetup): World {
  const world: World = { reads: [], exists: [], runs: [], statuses: [], opened: [], commands: [], forbidden: [] }
  const env = setup.env ?? { HOME }
  const run = setup.run ?? healthyRun
  const isDir = (path: string) =>
    (setup.dirs ?? []).includes(path) || Object.keys(setup.files).some(file => file.startsWith(`${path}/`))

  on('fs.exists', ($, e) => {
    world.exists.push(e.path)

    return { value: e.path in setup.files || isDir(e.path) }
  })

  on('fs.read', ($, e) => {
    world.reads.push(e.path)
    const text = setup.files[e.path]

    return text === undefined ? { deny: `ENOENT: ${e.path}` } : { value: text }
  })

  on('fs.write', ($, e) => {
    world.forbidden.push(`write ${e.path}`)

    return { deny: 'the mod writes nothing' }
  })

  on('fs.list', ($, e) => {
    world.forbidden.push(`list ${e.path}`)

    return { deny: 'the mod lists nothing' }
  })

  on('fs.stat', ($, e) => {
    world.forbidden.push(`stat ${e.path}`)

    return { deny: 'the mod stats nothing' }
  })

  on('store.get', ($, e) => {
    world.forbidden.push(`store.get ${e.key}`)

    return { deny: 'the mod keeps nothing' }
  })

  on('store.set', ($, e) => {
    world.forbidden.push(`store.set ${e.key}`)

    return { deny: 'the mod keeps nothing' }
  })

  on('process.run', ($, e) => {
    world.runs.push({ argv: [...e.argv], cwd: e.init?.cwd, timeoutMs: e.init?.timeoutMs })
    const answer = run(e.argv)

    if ('deny' in answer) {
      return { deny: answer.deny }
    }

    return {
      value: { exitCode: answer.exitCode, stdout: answer.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
    }
  })

  on('env.get', ($, e) => (setup.envDeny === undefined ? { value: env[e.name] } : { deny: setup.envDeny }))

  on('ui.status', ($, e) => {
    world.statuses.push(e.text)

    return { value: undefined }
  })

  on('ui.open', ($, e) => {
    world.opened.push({ id: e.id, title: e.title })

    return setup.unplaced === undefined
      ? { value: { isPlaced: true as const } }
      : { value: { isPlaced: false as const, reason: setup.unplaced } }
  })

  on('command.register', ($, e) => {
    world.commands.push(e.name)

    return { value: { command: e.name } }
  })

  on('session.start', ($, e) => ({ cwd: e.cwd }))

  return world
}

export const SESSION: SessionStartInput = { surface: 'terminal', isInteractive: true, cwd: CWD }

export const command = (name: string, args = '') => ({
  command: name,
  args,
  origin: { kind: 'composer' as const },
  presentation: { isFullscreen: true, columns: 180 },
})

export const PANE_PROPS = {
  title: 'Voortgang',
  isFocused: false,
  bodyColumns: 72,
  placement: 'dock' as const,
  scroll: { offset: 0, bodyRows: 44 },
  view: {},
}

export const PANE = {
  plugin: PLUGIN,
  component: 'Pane' as const,
  requestId: 'voortgang',
  viewport: { columns: 180, rows: 48, isFullscreen: true },
  props: PANE_PROPS,
}
