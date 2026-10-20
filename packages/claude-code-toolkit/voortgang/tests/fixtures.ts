import type { On, SessionStartInput } from 'claude-code'

export const PLUGIN = 'voortgang'
export const HOME = '/home/op'
export const ROOT = `${HOME}/.vnx-data`
export const BIN = `${HOME}/.vnx-system/current/bin/vnx`
export const NOW = Date.parse('2026-10-20T16:00:00.000Z')

export const storeOf = (project: string) => `${ROOT}/${project}/state`
export const indexOf = (project: string) => `${storeOf(project)}/t0_index.json`
export const stateOf = (project: string) => `${storeOf(project)}/t0_state.json`

/** A timestamp `minutes` before NOW, spelled as build_t0_state writes it: microseconds and `+00:00`. */
export const stampMinutesAgo = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString().replace('Z', '000+00:00')

/** Text that stands for what the mod must never show: a body, a reason, an item's content. */
export const CONTENT = 'KLANTINHOUD'

export const LIVE_WORK = {
  available: true,
  counts: { live: 1, starting: 0, stale: 4, unmeasured: 0 },
  live: [{ dispatch_id: 'd-live-1', state: 'running', age_seconds: 30, pr: null }],
  stale: [{ dispatch_id: 'd-stale-1', state: 'stale', age_seconds: 9000, lock: 'x' }],
  unmeasured: [],
  open_prs: [],
}

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

const track = (track_id: string, horizon: string | null, priority = 'P2', phase = 'queued', derived_status = phase) => ({
  track_id,
  title: CONTENT,
  phase,
  derived_status,
  horizon,
  priority,
})

export const TRACKS = [
  track('ci-profile', 'now', 'P1', 'queued', 'in_progress'),
  track('docs', 'now', 'P2', 'active', 'active'),
  track('n1', 'next'),
  track('n2', 'next'),
  track('l1', 'later'),
  track('l2', 'later'),
  track('l3', 'later'),
  track('u1', null),
]

const deliverable = (trackId: string, derived_status: string) => ({
  deliverable_ref: `${trackId}-${derived_status}`,
  track: trackId,
  derived_status,
  title: CONTENT,
})

export const DELIVERABLES = [
  deliverable('ci-profile', 'proposed'),
  deliverable('ci-profile', 'proposed'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'done'),
  deliverable('ci-profile', 'failed'),
  deliverable('docs', 'ready'),
  deliverable('l1', 'proposed'),
]

/** The two files of a healthy store. */
export const healthyFiles = (project: string): Record<string, string> => ({
  [indexOf(project)]: indexJson(),
  [stateOf(project)]: stateJson(),
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

  on('env.get', ($, e) => ({ value: env[e.name] }))

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

export const SESSION: SessionStartInput = { surface: 'terminal', isInteractive: true, cwd: '/work' }

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
