import type { PluginOptions, ProcessRunInit, ProcessRunResult } from 'claude-code'

import type {
  DeliverableView,
  IndexView,
  LiveWork,
  Model,
  NowTrack,
  Outcomes,
  Part,
  Pr,
  ProjectModel,
  StateView,
  TrackView,
} from '../types'

/** The three things the collector needs from the engine; `$` in a session, an object in memory in a test. */
export type Io = {
  exists: (path: string) => Promise<boolean>
  read: (path: string) => Promise<string>
  run: (argv: readonly string[], init: ProcessRunInit) => Promise<ProcessRunResult>
}

export type Config = { projects: string[]; dataRoot: string; vnxBin: string; refreshSeconds: number }

export const STALE_MINUTES = 60
export const RUN_TIMEOUT_MS = 10_000
export const MIN_REFRESH_SECONDS = 15
export const INDEX_FILE = 't0_index.json'
export const STATE_FILE = 't0_state.json'

const DEFAULT_PROJECTS = 'vnx-dev,mission-control,seocrawler-v2,sales-copilot'
const DEFAULT_DATA_ROOT = '~/.vnx-data'
const DEFAULT_VNX_BIN = '~/.vnx-system/current/bin/vnx'
const DEFAULT_REFRESH_SECONDS = 120
const HOME_UNKNOWN = 'HOME onbekend, ~ niet op te lossen'
const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const MESSAGE_CHARS = 80

type Json = Record<string, unknown>

const ok = <T>(value: T): Part<T> => ({ isOk: true, value })
const fail = <T>(reason: string): Part<T> => ({ isOk: false, reason })
const isRecord = (value: unknown): value is Json => typeof value === 'object' && value !== null && !Array.isArray(value)
const isCount = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const textOr = (value: unknown, fallback: string): string => (typeof value === 'string' && value !== '' ? value : fallback)
const stringsOf = (value: unknown): string[] | null =>
  Array.isArray(value) ? value.filter((one): one is string => typeof one === 'string') : null
const idsOf = (value: unknown): string[] =>
  Array.isArray(value) ? value.flatMap(one => (isRecord(one) && typeof one.dispatch_id === 'string' ? [one.dispatch_id] : [])) : []

function messageOf(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error)
  const line = text.split('\n')[0] ?? ''

  return line.length > MESSAGE_CHARS ? `${line.slice(0, MESSAGE_CHARS - 1)}…` : line
}

export function projectsOf(text: string): string[] {
  const ids = text.split(',').map(one => one.trim()).filter(one => one !== '')

  return [...new Set(ids)]
}

export function configOf(options: PluginOptions): Config {
  const pathOf = (value: unknown, fallback: string) => (typeof value === 'string' && value.trim() !== '' ? value.trim() : fallback)
  const seconds = options.refresh_seconds

  return {
    projects: projectsOf(typeof options.projects === 'string' ? options.projects : DEFAULT_PROJECTS),
    dataRoot: pathOf(options.data_root, DEFAULT_DATA_ROOT),
    vnxBin: pathOf(options.vnx_bin, DEFAULT_VNX_BIN),
    refreshSeconds: isCount(seconds) ? Math.max(MIN_REFRESH_SECONDS, seconds) : DEFAULT_REFRESH_SECONDS,
  }
}

/** The path with a leading `~` resolved from HOME; null when it needs HOME and there is none. */
export function expandHome(path: string, home: string | undefined): string | null {
  if (path !== '~' && !path.startsWith('~/')) {
    return path
  }

  return home === undefined ? null : `${home.replace(/\/+$/, '')}${path.slice(1)}`
}

/** A project id names one directory under the data root: no separator, no `.` or `..`. */
export function isProjectId(id: string): boolean {
  return PROJECT_ID.test(id)
}

export function storeOf(root: string, id: string): string {
  return `${root.replace(/\/+$/, '')}/${id}/state`
}

export function objectiveArgv(bin: string, id: string, store: string): string[] {
  return [bin, 'objective', 'list', '--json', '--project-id', id, '--state-dir', store]
}

export function deliverableArgv(bin: string, id: string, store: string): string[] {
  return [bin, 'deliverable', 'list', '--json', '--project-id', id, '--state-dir', store]
}

export function parseJson(text: string): Part<unknown> {
  try {
    return ok(JSON.parse(text) as unknown)
  } catch {
    return fail('ongeldige JSON')
  }
}

/** Whole minutes from an ISO timestamp to `now`; null when there is no readable timestamp. */
export function ageMinutesOf(stamp: unknown, now: number): number | null {
  if (typeof stamp !== 'string') {
    return null
  }

  // build_t0_state writes microseconds; a date parser need not take more than milliseconds.
  const at = Date.parse(stamp.replace(/(\.\d{3})\d+/, '$1'))

  return Number.isNaN(at) ? null : Math.max(0, Math.floor((now - at) / 60_000))
}

/** An index of unknown age cannot show it is fresh, so it counts as stale. */
export function isStale(ageMinutes: number | null): boolean {
  return ageMinutes === null || ageMinutes > STALE_MINUTES
}

export function formatAge(ageMinutes: number | null): string {
  if (ageMinutes === null) {
    return 'leeftijd onbekend'
  }

  if (ageMinutes < 60) {
    return `${ageMinutes} min`
  }

  if (ageMinutes < 48 * 60) {
    return `${Math.floor(ageMinutes / 60)} u`
  }

  return `${Math.floor(ageMinutes / (24 * 60))} d`
}

/** A block of the index or the state that says `available: true`, or why not. */
function blockOf(value: unknown, name: string): Part<Json> {
  if (!isRecord(value)) {
    return fail(`blok ${name} ontbreekt`)
  }

  if (value.available !== true) {
    return fail(textOr(value.reason, `available: ${String(value.available)}`))
  }

  return ok(value)
}

function liveWorkOf(value: unknown): Part<LiveWork> {
  const block = blockOf(value, 'live_work')

  if (!block.isOk) {
    return fail(block.reason)
  }

  const counts = block.value.counts

  if (!isRecord(counts) || ![counts.live, counts.starting, counts.stale, counts.unmeasured].every(isCount)) {
    return fail('counts onvolledig')
  }

  return ok({
    live: counts.live as number,
    starting: counts.starting as number,
    stale: counts.stale as number,
    unmeasured: counts.unmeasured as number,
    liveIds: idsOf(block.value.live),
  })
}

function outcomesOf(value: unknown): Part<Outcomes> {
  const block = blockOf(value, 'open_outcomes')

  if (!block.isOk) {
    return fail(block.reason)
  }

  const { total, by_outcome: byOutcome } = block.value

  if (!isCount(total)) {
    return fail('total ontbreekt')
  }

  return ok({
    total,
    byOutcome: isRecord(byOutcome) ? Object.fromEntries(Object.entries(byOutcome).filter(([, count]) => isCount(count))) as Record<string, number> : {},
    ids: idsOf(block.value.items),
  })
}

export function indexViewOf(json: unknown, now: number): Part<IndexView> {
  if (!isRecord(json)) {
    return fail('geen JSON-object')
  }

  return ok({
    ageMinutes: ageMinutesOf(json.timestamp, now),
    liveWork: liveWorkOf(json.live_work),
    outcomes: outcomesOf(json.open_outcomes),
  })
}

function prsOf(value: unknown): Part<Pr[]> {
  const block = blockOf(value, 'pr_queue')

  if (!block.isOk) {
    return fail(block.reason)
  }

  const open = block.value.open_prs

  if (!Array.isArray(open)) {
    return fail('open_prs ontbreekt')
  }

  const records = open.filter(isRecord)

  if (records.length !== open.length || !records.every(pr => isCount(pr.number))) {
    return fail('PR zonder nummer')
  }

  return ok(
    records.map(pr => ({
      number: pr.number as number,
      title: textOr(pr.title, ''),
      ci: textOr(pr.ci_status, '?'),
      merge: textOr(pr.merge_state, '?'),
      passed: stringsOf(pr.gates_passed),
      owed: stringsOf(pr.blocked_on),
    })),
  )
}

export function stateViewOf(json: unknown, now: number): Part<StateView> {
  if (!isRecord(json)) {
    return fail('geen JSON-object')
  }

  return ok({ ageMinutes: ageMinutesOf(json.generated_at, now), prs: prsOf(json.pr_queue) })
}

const byPriority = (a: NowTrack, b: NowTrack) => {
  if (a.priority !== b.priority) {
    return a.priority < b.priority ? -1 : 1
  }

  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
}

export function trackViewOf(json: unknown): Part<TrackView> {
  if (!Array.isArray(json)) {
    return fail('vnx gaf geen lijst')
  }

  const view: TrackView = { now: [], next: 0, later: 0, unplanned: 0 }

  for (const track of json.filter(isRecord)) {
    if (track.horizon === 'now') {
      const phase = textOr(track.phase, '?')
      const derived = track.derived_status

      view.now.push({
        id: textOr(track.track_id, '?'),
        priority: textOr(track.priority, '-'),
        phase,
        derivedStatus: typeof derived === 'string' && derived !== '' && derived !== phase ? derived : null,
      })
    } else if (track.horizon === 'next') {
      view.next += 1
    } else if (track.horizon === 'later') {
      view.later += 1
    } else {
      view.unplanned += 1
    }
  }

  view.now.sort(byPriority)

  return ok(view)
}

export function deliverableViewOf(json: unknown): Part<DeliverableView> {
  if (!Array.isArray(json)) {
    return fail('vnx gaf geen lijst')
  }

  const view: DeliverableView = { byTrack: {}, proposed: 0 }

  for (const deliverable of json.filter(isRecord)) {
    const track = textOr(deliverable.track, '?')
    const status = textOr(deliverable.derived_status, 'onbekend')
    const counts = view.byTrack[track] ?? {}

    counts[status] = (counts[status] ?? 0) + 1
    view.byTrack[track] = counts

    if (status === 'proposed') {
      view.proposed += 1
    }
  }

  return ok(view)
}

const andThen = <A, B>(part: Part<A>, next: (value: A) => Part<B>): Part<B> => (part.isOk ? next(part.value) : fail(part.reason))

async function readJson(io: Io, store: string, name: string): Promise<Part<unknown>> {
  const path = `${store}/${name}`

  if (!(await io.exists(path))) {
    return fail(`${name} ontbreekt`)
  }

  let text: string

  try {
    text = await io.read(path)
  } catch (error) {
    return fail(`${name} onleesbaar: ${messageOf(error)}`)
  }

  return parseJson(text)
}

async function runJson(io: Io, argv: string[], store: string): Promise<Part<unknown>> {
  let result: ProcessRunResult

  try {
    result = await io.run(argv, { cwd: store, timeoutMs: RUN_TIMEOUT_MS })
  } catch (error) {
    const message = messageOf(error)

    return fail(/timed? ?-?out|ETIMEDOUT/i.test(message) ? 'vnx time-out' : `vnx startfout: ${message}`)
  }

  if (result.exitCode !== 0) {
    return fail(`vnx exit ${result.exitCode}`)
  }

  const parsed = parseJson(result.stdout)

  return parsed.isOk ? parsed : fail('vnx gaf ongeldige JSON')
}

/**
 * One project: the store directory, its two files and the two vnx lists, each
 * part read on its own so one that fails leaves the others standing.
 */
export async function collectProject(io: Io, id: string, root: string, bin: string, now: number): Promise<ProjectModel> {
  if (!isProjectId(id)) {
    return { id, hasStore: false, reason: 'ongeldige project-id' }
  }

  const store = storeOf(root, id)

  if (!(await io.exists(store))) {
    return { id, hasStore: false, reason: 'geen store' }
  }

  const index = andThen(await readJson(io, store, INDEX_FILE), json => indexViewOf(json, now))
  const state = andThen(await readJson(io, store, STATE_FILE), json => stateViewOf(json, now))
  const tracks = andThen(await runJson(io, objectiveArgv(bin, id, store), store), trackViewOf)
  const deliverables = andThen(await runJson(io, deliverableArgv(bin, id, store), store), deliverableViewOf)

  return { id, hasStore: true, index, state, tracks, deliverables }
}

export async function collectModel(io: Io, config: Config, home: string | undefined, now: number): Promise<Model> {
  const root = expandHome(config.dataRoot, home)
  const bin = expandHome(config.vnxBin, home)

  if (root === null || bin === null) {
    return { collectedAt: now, rootError: HOME_UNKNOWN, projects: [] }
  }

  const projects = await Promise.all(config.projects.map(id => collectProject(io, id, root, bin, now)))

  return { collectedAt: now, rootError: null, projects }
}

export function isUnreadable(project: ProjectModel): boolean {
  if (!project.hasStore) {
    return true
  }

  const { index, state, tracks, deliverables } = project

  return (
    !index.isOk ||
    !index.value.liveWork.isOk ||
    !index.value.outcomes.isOk ||
    !state.isOk ||
    !state.value.prs.isOk ||
    !tracks.isOk ||
    !deliverables.isOk
  )
}

export function isOutdated(project: ProjectModel): boolean {
  if (!project.hasStore) {
    return false
  }

  const { index, state } = project

  return (index.isOk && isStale(index.value.ageMinutes)) || (state.isOk && isStale(state.value.ageMinutes))
}

const liveOf = (project: ProjectModel): number | null =>
  project.hasStore && project.index.isOk && project.index.value.liveWork.isOk ? project.index.value.liveWork.value.live : null
const openOf = (project: ProjectModel): number | null =>
  project.hasStore && project.index.isOk && project.index.value.outcomes.isOk ? project.index.value.outcomes.value.total : null
const prCountOf = (project: ProjectModel): number | null =>
  project.hasStore && project.state.isOk && project.state.value.prs.isOk ? project.state.value.prs.value.length : null

/** The sum over the projects that could be read; `?` when none could. */
function sumOf(model: Model, pick: (project: ProjectModel) => number | null): string {
  const counts = model.projects.map(pick).filter((count): count is number => count !== null)

  return counts.length === 0 ? '?' : String(counts.reduce((sum, count) => sum + count, 0))
}

/** The one line under the prompt: totals, then how many projects could not be read or are outdated. */
export function statusLine(model: Model): string {
  if (model.rootError !== null) {
    return `voortgang · onbeschikbaar: ${model.rootError}`
  }

  if (model.projects.length === 0) {
    return 'voortgang · geen projecten'
  }

  const unreadable = model.projects.filter(isUnreadable).length
  const outdated = model.projects.filter(isOutdated).length
  const parts = ['voortgang', `${sumOf(model, liveOf)} live`, `${sumOf(model, prCountOf)} PR`, `${sumOf(model, openOf)} open`]

  if (unreadable > 0) {
    parts.push(`${unreadable} onleesbaar`)
  }

  if (outdated > 0) {
    parts.push(`${outdated} verouderd`)
  }

  return parts.join(' · ')
}

function summaryOf(project: ProjectModel): string {
  if (!project.hasStore) {
    return `${project.id}: ${project.reason}`
  }

  const { index, state, tracks, deliverables } = project
  const parts: string[] = []

  if (!index.isOk) {
    parts.push('index onbeschikbaar')
  } else {
    const { liveWork } = index.value

    parts.push(liveWork.isOk ? `${liveWork.value.live} live` : 'werk onbeschikbaar')
  }

  parts.push(state.isOk && state.value.prs.isOk ? `${state.value.prs.value.length} PR` : "PR's onbeschikbaar")

  if (index.isOk) {
    const { outcomes } = index.value

    parts.push(outcomes.isOk ? `${outcomes.value.total} open` : 'uitkomsten onbeschikbaar')
  }

  if (tracks.isOk) {
    const ids = tracks.value.now.map(track => track.id)

    parts.push(`now ${ids.length === 0 ? 'geen' : ids.join(', ')}`, `next ${tracks.value.next}`, `later ${tracks.value.later}`)
  } else {
    parts.push('tracks onbeschikbaar')
  }

  parts.push(deliverables.isOk ? `${deliverables.value.proposed} te promoveren` : 'leveringen onbeschikbaar')

  if (isOutdated(project)) {
    parts.push('verouderd')
  }

  return `${project.id}: ${parts.join(' · ')}`
}

/** One line per project: track ids and counts, no content. */
export function summaryLines(model: Model): string[] {
  if (model.rootError !== null) {
    return [`onbeschikbaar: ${model.rootError}`]
  }

  return model.projects.map(summaryOf)
}

/**
 * What `/voortgang` answers: one short line when the pane is placed; where it
 * is not (no surface draws it, as in a headless run) the summary lines, so the
 * person still sees the progress.
 */
export function commandText(model: Model, unplacedReason: string | null): string {
  if (unplacedReason === null) {
    return `Voortgang geopend: ${statusLine(model).replace(/^voortgang · /, '')}`
  }

  return [`Voortgang (paneel niet getoond: ${unplacedReason})`, ...summaryLines(model)].join('\n')
}
