import type { PluginOptions, ProcessRunInit, ProcessRunResult } from 'claude-code'

import type {
  Builder,
  DeliverableView,
  IndexView,
  LiveWork,
  Model,
  Outcomes,
  Part,
  Pr,
  PrDispatch,
  ProjectModel,
  Running,
  StateView,
  TrackDeliverables,
  TrackInfo,
  TrackView,
} from '../types'

/** The four things the collector needs from the engine; `$` in a session, an object in memory in a test. */
export type Io = {
  /** The names of the entries of a directory. */
  list: (path: string) => Promise<string[]>
  exists: (path: string) => Promise<boolean>
  read: (path: string) => Promise<string>
  run: (argv: readonly string[], init: ProcessRunInit) => Promise<ProcessRunResult>
}

export type Config = { dataRoot: string; vnxBin: string; refreshSeconds: number }

export const STALE_MINUTES = 60
export const RUN_TIMEOUT_MS = 10_000
export const MIN_REFRESH_SECONDS = 15
export const INDEX_FILE = 't0_index.json'
export const STATE_FILE = 't0_state.json'
export const DB_FILE = 'runtime_coordination.db'
export const SPEC_FILE = 'dispatch-spec.json'
export const INSTRUCTION_FILE = 'instruction.md'
export const PROJECT_ID_FILE = '.vnx-project-id'
/** A deliverable counts as recent activity of its track when its `last_activity` is at most this old. */
export const RECENT_DAYS = 7
/** The cells of a progress bar. */
export const BAR_CELLS = 8
/** How much of `instruction.md` is searched for its first `# ` line, in characters. */
export const INSTRUCTION_BUDGET = 4096

const DEFAULT_DATA_ROOT = '~/.vnx-data'
const DEFAULT_VNX_BIN = '~/.vnx-system/current/bin/vnx'
const DEFAULT_REFRESH_SECONDS = 120
const HOME_UNKNOWN = 'HOME onbekend, ~ niet op te lossen'
const PROJECT_ID = /^[a-z][a-z0-9-]{1,31}$/
const DISPATCH_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const MESSAGE_CHARS = 80

type Json = Record<string, unknown>

const ok = <T>(value: T): Part<T> => ({ isOk: true, value })
const fail = <T>(reason: string): Part<T> => ({ isOk: false, reason })
const andThen = <A, B>(part: Part<A>, next: (value: A) => Part<B>): Part<B> => (part.isOk ? next(part.value) : fail(part.reason))
const isRecord = (value: unknown): value is Json => typeof value === 'object' && value !== null && !Array.isArray(value)
const isCount = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const textOr = (value: unknown, fallback: string): string => (typeof value === 'string' && value !== '' ? value : fallback)
const stringsOf = (value: unknown): string[] | null =>
  Array.isArray(value) ? value.filter((one): one is string => typeof one === 'string') : null
/** A dictionary whose keys come from data: no prototype, so `__proto__` or `constructor` is a key like any other. */
const dictionaryOf = <T>(entries: Iterable<readonly [string, T]> = []): Record<string, T> => {
  const dictionary = Object.create(null) as Record<string, T>

  for (const [key, value] of entries) {
    dictionary[key] = value
  }

  return dictionary
}

/** The reason a rejected `exists`, `read`, `list` or `run` is shown with. */
const unavailableOf = (error: unknown): `onbeschikbaar: ${string}` => `onbeschikbaar: ${messageOf(error)}`

export function messageOf(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error)
  const line = text.split('\n')[0] ?? ''

  return line.length > MESSAGE_CHARS ? `${line.slice(0, MESSAGE_CHARS - 1)}…` : line
}

export function configOf(options: PluginOptions): Config {
  const pathOf = (value: unknown, fallback: string) => (typeof value === 'string' && value.trim() !== '' ? value.trim() : fallback)
  const seconds = options.refresh_seconds

  return {
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

/** A dispatch id names one directory under `dispatches/pending`: no separator, no `.` or `..`. */
export function isDispatchId(id: string): boolean {
  return DISPATCH_ID.test(id)
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

/** The most entries the index producer keeps in `live` (`_INDEX_LIVE_WORK_LIVE_CAP` in `scripts/build_t0_state.py`). */
export const INDEX_LIVE_CAP = 8

/**
 * The builders in the index. The producer writes `live` with the live and the starting entries together, capped,
 * and no `starting` list; a `starting` list is read too when it is there. A list that is absent stands only for a
 * count of zero.
 */
function runningOf(block: Json, counts: Json): Part<Running[]> {
  const running: Running[] = []

  for (const name of ['live', 'starting'] as const) {
    const list = block[name]

    if (list === undefined) {
      if (name === 'live' && (counts.live as number) + (counts.starting as number) > 0) {
        return fail('live ontbreekt')
      }

      continue
    }

    if (!Array.isArray(list)) {
      return fail(`${name} ontbreekt`)
    }

    for (const entry of list) {
      if (!isRecord(entry) || typeof entry.dispatch_id !== 'string' || entry.dispatch_id === '') {
        return fail(`${name}: bouwer zonder dispatch_id`)
      }

      running.push({ dispatchId: entry.dispatch_id, ageSeconds: isCount(entry.age_seconds) ? entry.age_seconds : null })
    }
  }

  return ok(running)
}

/** `live_work.open_prs`: which dispatch opened which PR. An entry that is not a number with a dispatch id says nothing and is left out. */
function livePrsOf(value: unknown): PrDispatch[] {
  if (!Array.isArray(value)) {
    return []
  }

  return value.flatMap(one =>
    isRecord(one) && isCount(one.number) && typeof one.dispatch_id === 'string' && one.dispatch_id !== '' ? [{ number: one.number, dispatchId: one.dispatch_id }] : [],
  )
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
    openPrs: livePrsOf(block.value.open_prs),
    running: runningOf(block.value, counts),
  })
}

function outcomesOf(value: unknown): Part<Outcomes> {
  const block = blockOf(value, 'open_outcomes')

  if (!block.isOk) {
    return fail(block.reason)
  }

  const { total } = block.value

  if (!isCount(total)) {
    return fail('total ontbreekt')
  }

  return ok({ total })
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

/** `pr_ref` of a track is one string of PR numbers (`#2139,#2142`); the numbers, in order. */
export function prRefsOf(value: unknown): number[] {
  return typeof value === 'string' ? [...value.matchAll(/\d+/g)].map(one => Number(one[0])) : []
}

export function trackViewOf(json: unknown): Part<TrackView> {
  if (!Array.isArray(json)) {
    return fail('vnx gaf geen lijst')
  }

  const malformed = json.findIndex(one => !isRecord(one))

  if (malformed >= 0) {
    return fail(`vnx gaf geen object op plaats ${malformed + 1}`)
  }

  const tracks: TrackInfo[] = (json as Json[]).map(track => ({
    id: textOr(track.track_id, '?'),
    priority: textOr(track.priority, '-'),
    horizon: textOr(track.horizon, '-'),
    prRefs: prRefsOf(track.pr_ref),
  }))

  return ok({ tracks })
}

export function deliverableViewOf(json: unknown, now: number): Part<DeliverableView> {
  if (!Array.isArray(json)) {
    return fail('vnx gaf geen lijst')
  }

  const view: DeliverableView = { byTrack: dictionaryOf(), proposed: 0 }
  const malformed = json.findIndex(one => !isRecord(one))

  if (malformed >= 0) {
    return fail(`vnx gaf geen object op plaats ${malformed + 1}`)
  }

  for (const deliverable of json as Json[]) {
    const track = textOr(deliverable.track, '?')
    const status = textOr(deliverable.derived_status, 'onbekend')
    const age = ageMinutesOf(deliverable.last_activity, now)
    const counts: TrackDeliverables = view.byTrack[track] ?? { total: 0, done: 0, isRecent: false }

    counts.total += 1
    counts.done += status === 'done' ? 1 : 0
    counts.isRecent = counts.isRecent || (age !== null && age <= RECENT_DAYS * 24 * 60)
    view.byTrack[track] = counts

    if (status === 'proposed') {
      view.proposed += 1
    }
  }

  return ok(view)
}

async function readText(io: Io, path: string, name: string): Promise<Part<string>> {
  try {
    if (!(await io.exists(path))) {
      return fail(`${name} ontbreekt`)
    }
  } catch (error) {
    return fail(unavailableOf(error))
  }

  try {
    return ok(await io.read(path))
  } catch (error) {
    return fail(`${name} ${unavailableOf(error)}`)
  }
}

const readJson = async (io: Io, path: string, name: string): Promise<Part<unknown>> => andThen(await readText(io, path, name), parseJson)

/** Whether a file exists, or why that could not be told. */
async function existsPart(io: Io, path: string): Promise<Part<boolean>> {
  try {
    return ok(await io.exists(path))
  } catch (error) {
    return fail(unavailableOf(error))
  }
}

/**
 * The first line that starts with `# ` in the first `INSTRUCTION_BUDGET` characters,
 * without the `# `. The engine reads a file whole, so the rest of the text is dropped
 * here and nothing of it is kept: no other line is looked at or shown.
 */
export function titleOf(text: string): Part<string> {
  const isCut = text.length > INSTRUCTION_BUDGET
  const lines = (isCut ? text.slice(0, INSTRUCTION_BUDGET) : text).split('\n')

  if (isCut) {
    // The last line ends at the budget, not at its own end: it may be cut in half.
    lines.pop()
  }

  for (const line of lines) {
    if (line.startsWith('# ')) {
      const title = line.slice(2).replace(/[\u0000-\u001f\u007f]/g, ' ').trim()

      return title === '' ? fail('lege titelregel') : ok(title)
    }
  }

  return fail(isCut ? `geen titelregel in de eerste ${INSTRUCTION_BUDGET} tekens` : 'geen titelregel')
}

/** A spec field that is a non-empty string, or null when the spec sets none. */
const optionalTextOf = (value: unknown): string | null => (typeof value === 'string' && value.trim() !== '' ? value.trim() : null)

/** `model`, `track_id` and `pr_id` of a dispatch spec; the last two are null when the spec sets none. */
export function whoOf(json: unknown): Part<{ model: string; trackId: string | null; prId: string | null }> {
  if (!isRecord(json)) {
    return fail('spec is geen JSON-object')
  }

  const { model, track_id: trackId, pr_id: prId } = json

  if (typeof model !== 'string' || model === '') {
    return fail('model ontbreekt')
  }

  return ok({ model, trackId: optionalTextOf(trackId), prId: isCount(prId) ? String(prId) : optionalTextOf(prId) })
}

/** The minutes of an `age_seconds`, whole. */
export function minutesOf(ageSeconds: number | null): Part<number> {
  return ageSeconds === null ? fail('age_seconds ontbreekt') : ok(Math.floor(Math.max(0, ageSeconds) / 60))
}

/** One builder: its spec, the first title line of its instruction, and its age; each part on its own. */
async function builderOf(io: Io, dir: string, entry: Running): Promise<Builder> {
  const minutes = minutesOf(entry.ageSeconds)

  if (!isDispatchId(entry.dispatchId)) {
    return { who: fail('ongeldige dispatch-id'), task: fail('ongeldige dispatch-id'), minutes }
  }

  const base = `${dir}/${entry.dispatchId}`
  const who = andThen(await readJson(io, `${base}/${SPEC_FILE}`, SPEC_FILE), whoOf)
  const task = andThen(await readText(io, `${base}/${INSTRUCTION_FILE}`, INSTRUCTION_FILE), titleOf)

  return { who, task, minutes }
}

const dispatchesOf = (root: string, id: string) => `${root.replace(/\/+$/, '')}/${id}/dispatches/pending`

/** The builders of a project, in the order of the index: `live` first, then `starting`. */
async function buildersOf(io: Io, root: string, id: string, index: Part<IndexView>): Promise<Part<Builder[]>> {
  if (!index.isOk) {
    return fail(index.reason)
  }

  const { liveWork } = index.value

  if (!liveWork.isOk) {
    return fail(liveWork.reason)
  }

  const { running } = liveWork.value

  if (!running.isOk) {
    return fail(running.reason)
  }

  const dir = dispatchesOf(root, id)

  return ok(await Promise.all(running.value.map(entry => builderOf(io, dir, entry))))
}

/**
 * The track that the dispatch of each open PR names in its spec. A spec that is gone (the dispatch left `pending/`)
 * or has no `track_id` links nothing: the PR still shows under the tracks whose `pr_ref` lists it.
 */
async function prTracksOf(io: Io, root: string, id: string, index: Part<IndexView>): Promise<Record<number, string>> {
  if (!index.isOk || !index.value.liveWork.isOk) {
    return {}
  }

  const dir = dispatchesOf(root, id)
  const linked: Record<number, string> = {}

  await Promise.all(
    index.value.liveWork.value.openPrs.map(async ({ number, dispatchId }) => {
      if (!isDispatchId(dispatchId)) {
        return
      }

      const who = andThen(await readJson(io, `${dir}/${dispatchId}/${SPEC_FILE}`, SPEC_FILE), whoOf)

      if (who.isOk && who.value.trackId !== null) {
        linked[number] = who.value.trackId
      }
    }),
  )

  return linked
}

/** Builders the counts name that the index lists no entry for: its list is capped, the counts are not. */
function omittedOf(index: Part<IndexView>, builders: Part<Builder[]>): number {
  if (!index.isOk || !index.value.liveWork.isOk || !builders.isOk) {
    return 0
  }

  const { live, starting } = index.value.liveWork.value

  return Math.max(0, live + starting - builders.value.length)
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
 * The project: the store directory, its two files and the two vnx lists, each
 * part read on its own so one that fails leaves the others standing.
 */
export async function collectProject(io: Io, id: string, root: string, bin: string, now: number): Promise<ProjectModel> {
  if (!isProjectId(id)) {
    return { id, hasStore: false, reason: 'ongeldige project-id' }
  }

  const store = storeOf(root, id)

  try {
    if (!(await io.exists(store))) {
      return { id, hasStore: false, reason: 'geen store' }
    }
  } catch (error) {
    return { id, hasStore: false, reason: unavailableOf(error) }
  }

  const index = andThen(await readJson(io, `${store}/${INDEX_FILE}`, INDEX_FILE), json => indexViewOf(json, now))
  const state = andThen(await readJson(io, `${store}/${STATE_FILE}`, STATE_FILE), json => stateViewOf(json, now))
  const builders = await buildersOf(io, root, id, index)
  const buildersOmitted = omittedOf(index, builders)
  const prTracks = await prTracksOf(io, root, id, index)

  // `vnx objective list` and `vnx deliverable list` create the database when the store has none; the mod writes nothing.
  const hasDb = await existsPart(io, `${store}/${DB_FILE}`)

  if (!hasDb.isOk || !hasDb.value) {
    const reason = hasDb.isOk ? `${DB_FILE} ontbreekt` : hasDb.reason

    return { id, hasStore: true, index, state, tracks: fail(reason), deliverables: fail(reason), builders, buildersOmitted, prTracks }
  }

  const tracks = andThen(await runJson(io, objectiveArgv(bin, id, store), store), trackViewOf)
  const deliverables = andThen(await runJson(io, deliverableArgv(bin, id, store), store), json => deliverableViewOf(json, now))

  return { id, hasStore: true, index, state, tracks, deliverables, builders, buildersOmitted, prTracks }
}

/** The directories from `cwd` up to `/`, nearest first. */
export function ancestorsOf(cwd: string): string[] {
  const parts = cwd.split('/').filter(part => part !== '')
  const dirs: string[] = []

  for (let depth = parts.length; depth > 0; depth -= 1) {
    dirs.push(`/${parts.slice(0, depth).join('/')}`)
  }

  return [...dirs, '/']
}

/**
 * The project id of a session: walk up from `cwd` to the first directory that holds `.vnx-project-id`; its first
 * line, with all whitespace removed, is the id (the rule `bin/vnx` follows). The nearest file decides, as there. Never another project.
 */
export async function resolveProject(io: Io, cwd: string): Promise<Part<string>> {
  for (const dir of ancestorsOf(cwd)) {
    const file = `${dir === '/' ? '' : dir}/${PROJECT_ID_FILE}`
    const found = await existsPart(io, file)

    if (!found.isOk) {
      return fail(found.reason)
    }

    if (!found.value) {
      continue
    }

    const text = await readText(io, file, PROJECT_ID_FILE)

    if (!text.isOk) {
      return fail(text.reason)
    }

    const id = (text.value.split('\n')[0] ?? '').replace(/\s+/g, '')

    return isProjectId(id) ? ok(id) : fail(`geen geldig project-id in ${file}`)
  }

  return fail(`geen VNX-project in deze map (geen ${PROJECT_ID_FILE} boven ${cwd})`)
}

export async function collectModel(io: Io, config: Config, home: string | undefined, cwd: string | null, now: number): Promise<Model> {
  if (cwd === null) {
    return { collectedAt: now, noProject: 'geen VNX-project in deze map (werkmap onbekend)', project: null }
  }

  const id = await resolveProject(io, cwd)

  if (!id.isOk) {
    return { collectedAt: now, noProject: id.reason, project: null }
  }

  const root = expandHome(config.dataRoot, home)
  const bin = expandHome(config.vnxBin, home)

  if (root === null || bin === null) {
    return { collectedAt: now, noProject: HOME_UNKNOWN, project: null }
  }

  return { collectedAt: now, noProject: null, project: await collectProject(io, id.value, root, bin, now) }
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
    !deliverables.isOk ||
    !project.builders.isOk ||
    project.builders.value.some(builder => !builder.who.isOk || !builder.task.isOk || !builder.minutes.isOk)
  )
}

export function isOutdated(project: ProjectModel): boolean {
  if (!project.hasStore) {
    return false
  }

  const { index, state } = project

  return (index.isOk && isStale(index.value.ageMinutes)) || (state.isOk && isStale(state.value.ageMinutes))
}

const countOf = (value: number | null): string => (value === null ? '?' : String(value))

/** The number of builders: the counts of the live work, not the capped list. Null when it could not be read. */
export function builderCountOf(project: ProjectModel): number | null {
  return project.hasStore && project.builders.isOk ? project.builders.value.length + project.buildersOmitted : null
}

/** The open PRs of the PR queue; null when it could not be read. */
export function openPrsOf(project: ProjectModel): Pr[] | null {
  return project.hasStore && project.state.isOk && project.state.value.prs.isOk ? project.state.value.prs.value : null
}

/**
 * The numbers of the open PRs: those that `pr_queue` of the state lists, and those that `live_work` of the index lists.
 * A PR is open when either readable source lists it. Null when neither could be read, with the reasons.
 */
export function openNumbersOf(project: ProjectModel): { numbers: Set<number> | null; reason: string } {
  if (!project.hasStore) {
    return { numbers: null, reason: project.reason }
  }

  const reasons: string[] = []
  const numbers = new Set<number>()
  let isRead = false

  if (project.state.isOk && project.state.value.prs.isOk) {
    isRead = true
    project.state.value.prs.value.forEach(pr => numbers.add(pr.number))
  } else {
    reasons.push(!project.state.isOk ? project.state.reason : project.state.value.prs.isOk ? '' : project.state.value.prs.reason)
  }

  if (project.index.isOk && project.index.value.liveWork.isOk) {
    isRead = true
    project.index.value.liveWork.value.openPrs.forEach(pr => numbers.add(pr.number))
  } else {
    reasons.push(!project.index.isOk ? project.index.reason : project.index.value.liveWork.isOk ? '' : project.index.value.liveWork.reason)
  }

  return { numbers: isRead ? numbers : null, reason: reasons.join('; ') }
}

/** One track the pane shows: the track, its measured progress, and what is going on around it. */
export type ShownTrack = {
  track: TrackInfo
  /** Null when the track has no deliverables, or when the deliverables could not be read (`unreadable`). */
  progress: TrackDeliverables | null
  /** Why the deliverables could not be read; null when they could. */
  unreadable: string | null
  builders: number
  /** The PRs of `pr_ref`, `isOpen` when a readable source lists them; null when no source could be read. */
  prs: { number: number; isOpen: boolean | null }[]
  /** Why the state of the PRs is unknown; null when a source could be read. */
  prUnknown: string | null
  hasActivity: boolean
}

const PRIORITY_ORDER = ['P1', 'P2', 'P3']

const rankOf = (priority: string): number => {
  const at = PRIORITY_ORDER.indexOf(priority)

  return at < 0 ? PRIORITY_ORDER.length : at
}

/**
 * The tracks the pane shows: every `now` track, and every track with activity (a builder whose spec names it, an
 * open PR that its `pr_ref` lists or whose dispatch spec names it, a deliverable with recent `last_activity`).
 * Tracks with activity first, then by priority, then by id. Null when the tracks could not be read.
 */
export function shownTracksOf(project: ProjectModel): ShownTrack[] | null {
  if (!project.hasStore || !project.tracks.isOk) {
    return null
  }

  const deliverables = project.deliverables.isOk ? project.deliverables.value.byTrack : null
  const open = openNumbersOf(project)
  const openNumbers = open.numbers
  const builderTracks = project.builders.isOk ? project.builders.value.flatMap(builder => (builder.who.isOk && builder.who.value.trackId !== null ? [builder.who.value.trackId] : [])) : []
  const shown: ShownTrack[] = []

  for (const track of project.tracks.value.tracks) {
    const progress = deliverables?.[track.id] ?? null
    const builders = builderTracks.filter(id => id === track.id).length
    // An open PR whose dispatch spec names the track belongs to it even when `pr_ref` does not list it yet.
    const named = Object.entries(project.prTracks).flatMap(([number, id]) => (id === track.id && openNumbers?.has(Number(number)) ? [Number(number)] : []))
    const numbers = [...new Set([...track.prRefs, ...named])]
    const prs = numbers.map(number => ({ number, isOpen: openNumbers === null ? null : openNumbers.has(number) }))
    const hasActivity = builders > 0 || prs.some(pr => pr.isOpen === true) || (progress?.isRecent ?? false)
    // A failed read never drops a track: with unknown PR state, a track that has `pr_ref` entries stays.
    const isUnknown = openNumbers === null && prs.length > 0

    if (track.horizon === 'now' || hasActivity || isUnknown) {
      shown.push({ track, progress, unreadable: project.deliverables.isOk ? null : project.deliverables.reason, builders, prs, prUnknown: openNumbers === null ? open.reason : null, hasActivity })
    }
  }

  return shown.sort((a, b) => {
    if (a.hasActivity !== b.hasActivity) {
      return a.hasActivity ? -1 : 1
    }

    if (rankOf(a.track.priority) !== rankOf(b.track.priority)) {
      return rankOf(a.track.priority) - rankOf(b.track.priority)
    }

    return a.track.id < b.track.id ? -1 : a.track.id > b.track.id ? 1 : 0
  })
}

/** `X van Y klaar` for a track with deliverables, `geen leveringen gepland` for one without. */
export function progressText(one: ShownTrack): string {
  if (one.unreadable !== null) {
    return `leveringen onbeschikbaar: ${one.unreadable}`
  }

  return one.progress === null ? 'geen leveringen gepland' : `${one.progress.done} van ${one.progress.total} klaar`
}

/** The bar of `BAR_CELLS` cells: `▓` for done, `░` for the rest, rounded down. */
export function barOf(progress: TrackDeliverables): string {
  const filled = Math.floor((BAR_CELLS * progress.done) / progress.total)

  return '▓'.repeat(filled) + '░'.repeat(BAR_CELLS - filled)
}

/** The one line under the prompt; empty when the session is in no VNX project. */
export function statusLine(model: Model): string {
  const { project } = model

  if (project === null) {
    return ''
  }

  const proposed = project.hasStore && project.deliverables.isOk ? project.deliverables.value.proposed : null
  const outcomes = project.hasStore && project.index.isOk && project.index.value.outcomes.isOk ? project.index.value.outcomes.value.total : null
  const prs = openPrsOf(project)
  const parts = [project.id, `${countOf(builderCountOf(project))} bouwers`, `${countOf(prs === null ? null : prs.length)} PR`, `${countOf(proposed)} te promoveren`, `${countOf(outcomes)} uitkomsten`]

  if (isOutdated(project)) {
    parts.push('verouderd')
  }

  if (isUnreadable(project)) {
    parts.push('onleesbaar')
  }

  return parts.join(' · ')
}

/** The summary of `/voortgang` where no surface draws the pane: the status line, then one line per shown track. */
export function summaryLines(model: Model): string[] {
  if (model.project === null) {
    return [model.noProject ?? '']
  }

  const tracks = shownTracksOf(model.project)

  if (tracks === null) {
    const reason = model.project.hasStore && !model.project.tracks.isOk ? model.project.tracks.reason : model.project.hasStore ? '' : model.project.reason

    return [statusLine(model), `tracks onbeschikbaar: ${reason}`]
  }

  return [statusLine(model), ...tracks.map(one => `${one.track.priority} ${one.track.id} · ${progressText(one)}`)]
}

/**
 * What `/voortgang` answers: one short line when the pane is placed; where it
 * is not (no surface draws it, as in a headless run) the summary lines, so the
 * person still sees the progress.
 */
export function commandText(model: Model, unplacedReason: string | null): string {
  if (unplacedReason === null) {
    return model.project === null ? `Voortgang: ${model.noProject ?? ''}` : `Voortgang geopend: ${statusLine(model)}`
  }

  return summaryLines(model).join('\n')
}
