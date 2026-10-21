import type { Builder, Model, Pr, ProjectModel, Row } from '../types'

import { barOf, deliverablesNotice, formatAge, INDEX_LIVE_CAP, isOutdated, progressText, shownTracksOf } from './collect'
import type { ShownTrack } from './collect'
import { cellsOf, fit } from './width'

export { cellsOf, fit }

export type PaneSize = { columns: number; headerColumns: number; rows: number; isRefreshing: boolean; failure?: string | null }

/** Open PRs of a track that are closed are named up to this many; more are counted. */
const CLOSED_PRS_NAMED = 3
/** The column under which the second row of every track starts: after `  P1 `, whatever the priority is. */
const TRACK_INDENT = 5
/** The columns a title keeps at least, before it is cut for the track and the PR behind it. */
const MIN_TITLE_COLUMNS = 8

const row = (text: string, tone: Row['tone'] = 'normal'): Row => ({ text, tone })

const seatsOf = (seats: string[] | null) => (seats === null ? 'onbekend' : seats.length === 0 ? 'geen' : seats.join(', '))
const pluralOf = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`

/** A section of the pane: a title row, then items of one or more rows each. */
type Section = {
  title: string
  /** What the title row says after a colon when the section has no item. */
  empty: string
  items: Row[][]
  /** Items the data names that could not be drawn at all (the index lists at most `INDEX_LIVE_CAP` builders). */
  omitted: number
}

const sectionOf = (title: string, empty: string, items: Row[][], omitted = 0): Section => ({ title, empty, items, omitted })
const failedSection = (title: string, reason: string): Section => sectionOf(title, '', [[row(`  onbeschikbaar: ${reason}`, 'error')]])

/** `PR #2150` from the `pr_id` of a spec, which is a number or a string such as `#2150`. */
const prLabelOf = (prId: string): string => (/^#?\d+$/.test(prId) ? `PR #${prId.replace('#', '')}` : `PR ${prId}`)

function builderRow(builder: Builder, columns: number): Row[] {
  const failures = [builder.who, builder.task, builder.minutes].some(part => !part.isOk)
  const unavailableOf = (reason: string) => `onbeschikbaar: ${reason}`
  const model = builder.who.isOk ? (builder.who.value.model ?? 'model ?') : unavailableOf(builder.who.reason)
  const minutes = builder.minutes.isOk ? `${builder.minutes.value} min` : unavailableOf(builder.minutes.reason)
  const links = builder.who.isOk
    ? [builder.who.value.trackId, builder.who.value.prId === null ? null : prLabelOf(builder.who.value.prId)].filter((one): one is string => one !== null)
    : []
  const before = `  ${model} · ${minutes} · `
  const after = links.length === 0 ? '' : ` · ${links.join(' · ')}`
  const task = builder.task.isOk ? builder.task.value : unavailableOf(builder.task.reason)
  // The title gives way to the track and the PR behind it: those say what the builder belongs to.
  const room = columns - cellsOf(before) - cellsOf(after)
  const title = room >= MIN_TITLE_COLUMNS ? fit(task, room) : task

  return [row(`${before}${title}${after}`, failures ? 'error' : 'normal')]
}

const nowSectionOf = (project: Extract<ProjectModel, { hasStore: true }>, columns: number): Section => {
  if (!project.builders.isOk) {
    return failedSection('Nu bezig', project.builders.reason)
  }

  return sectionOf('Nu bezig', 'niets', project.builders.value.map(builder => builderRow(builder, columns)), project.buildersOmitted)
}

function waitSectionOf(project: Extract<ProjectModel, { hasStore: true }>): Section {
  const { index, deliverables } = project
  const outcomes = index.isOk ? index.value.outcomes : index
  const parts: string[] = []
  let isFailed = false

  if (deliverables.isOk) {
    parts.push(`${pluralOf(deliverables.value.proposed, 'levering', 'leveringen')} te promoveren`)
  } else {
    parts.push(`te promoveren onbeschikbaar: ${deliverables.reason}`)
    isFailed = true
  }

  if (outcomes.isOk) {
    parts.push(`${pluralOf(outcomes.value.total, 'open uitkomst', 'open uitkomsten')}`)
  } else {
    parts.push(`uitkomsten onbeschikbaar: ${outcomes.reason}`)
    isFailed = true
  }

  const isNothing = deliverables.isOk && outcomes.isOk && deliverables.value.proposed === 0 && outcomes.value.total === 0

  return sectionOf('Wacht op een besluit', 'niets', isNothing ? [] : [[row(`  ${parts.join(' · ')}`, isFailed ? 'error' : 'normal')]])
}

/** `PR's: #2141 #2148 dicht, #2150 open`: the closed ones named while there are few, the open ones always. */
function prsOfTrack(one: ShownTrack): string | null {
  const closed = one.prs.filter(pr => pr.isOpen === false)
  const open = one.prs.filter(pr => pr.isOpen === true)
  const unknown = one.prs.filter(pr => pr.isOpen === null)
  const parts = [
    closed.length === 0 ? null : closed.length <= CLOSED_PRS_NAMED ? `${closed.map(pr => `#${pr.number}`).join(' ')} dicht` : `${closed.length} dicht`,
    open.length === 0 ? null : `${open.map(pr => `#${pr.number}`).join(' ')} open`,
    unknown.length === 0 ? null : unknown.map(pr => `#${pr.number} ?`).join(' '),
  ].filter((part): part is string => part !== null)

  return parts.length === 0 ? null : `PR's: ${parts.join(', ')}`
}

function trackItem(one: ShownTrack): Row[] {
  const head = `  ${one.track.priority} ${one.track.id}`
  const isPrUnknown = one.prUnknown !== null && one.prs.some(pr => pr.isOpen === null)
  const around = [prsOfTrack(one), isPrUnknown ? `PR-status onbekend: ${one.prUnknown}` : null, one.builders === 0 ? null : pluralOf(one.builders, 'bouwer', 'bouwers')].filter((part): part is string => part !== null)
  const indent = ' '.repeat(TRACK_INDENT)

  if (one.unreadable !== null) {
    return [row(head), row(`${indent}${[progressText(one), ...around].join(' · ')}`, 'error')]
  }

  if (one.progress === null) {
    return [row(`${head}${one.builders > 0 ? ' · bezig' : ''}`), row(`${indent}${[progressText(one), ...around].join(' · ')}`, 'dim')]
  }

  const first = row(`${head} · ${progressText(one)} ${barOf(one.progress)}`)

  return around.length === 0 ? [first] : [first, row(`${indent}${around.join(' · ')}`, 'dim')]
}

function tracksSectionOf(project: Extract<ProjectModel, { hasStore: true }>): Section {
  const tracks = shownTracksOf(project)

  if (tracks === null) {
    return failedSection('Tracks', project.tracks.isOk ? '' : project.tracks.reason)
  }

  const notice = deliverablesNotice(project)

  return sectionOf('Tracks', 'geen', [...(notice === null ? [] : [[row(`  ${notice}`, 'error')]]), ...tracks.map(trackItem)])
}

const ciOf = (ci: string): string => (ci === 'pass' ? 'groen' : ci === 'fail' ? 'rood' : ci === 'pending' ? 'loopt' : ci)

const prRow = (pr: Pr): Row[] => [row(`  #${pr.number} · ci ${ciOf(pr.ci)} · geslaagd: ${seatsOf(pr.passed)} · open: ${seatsOf(pr.owed)}`)]

function prsSectionOf(project: Extract<ProjectModel, { hasStore: true }>): Section {
  const { state } = project

  if (!state.isOk) {
    return failedSection("PR's", state.reason)
  }

  if (!state.value.prs.isOk) {
    return failedSection("PR's", state.value.prs.reason)
  }

  return sectionOf("PR's", 'geen', state.value.prs.value.map(prRow))
}

/** The rows of a section in `free` rows: whole, or as many whole items as fit plus one row for the rest. Nothing when not even one item fits. */
function placeSection(section: Section, free: number): Row[] {
  const { title, items } = section

  if (items.length === 0) {
    const text = section.omitted > 0 ? `${section.omitted} niet getoond (index noemt ze niet)` : section.empty

    return free >= 1 ? [row(`${title}: ${text}`, 'head')] : []
  }

  const cost = (list: Row[][]) => list.reduce((sum, item) => sum + item.length, 0)
  const rest = (hidden: number) => row(`  +${hidden} niet getoond${hidden === section.omitted ? ` (index toont er max. ${INDEX_LIVE_CAP})` : ''}`, 'dim')

  if (1 + cost(items) + (section.omitted > 0 ? 1 : 0) <= free) {
    return [row(title, 'head'), ...items.flat(), ...(section.omitted > 0 ? [rest(section.omitted)] : [])]
  }

  let shown = items.length

  while (shown > 0 && 1 + cost(items.slice(0, shown)) + 1 > free) {
    shown -= 1
  }

  return shown === 0 ? [] : [row(title, 'head'), ...items.slice(0, shown).flat(), rest(items.length - shown + section.omitted)]
}

/** The one dim row at the bottom: what the live work counts as `stale` or `unmeasured`. Nothing when both are zero. */
function cleanupRow(project: Extract<ProjectModel, { hasStore: true }>): Row | null {
  if (!project.index.isOk || !project.index.value.liveWork.isOk) {
    return null
  }

  const { stale, unmeasured } = project.index.value.liveWork.value
  const parts = [stale > 0 ? pluralOf(stale, 'stale rij', 'stale rijen') : null, unmeasured > 0 ? `${unmeasured} ongemeten` : null].filter((part): part is string => part !== null)

  return parts.length === 0 ? null : row(`opruimen: ${parts.join(' · ')}`, 'dim')
}

/** The header of the pane: the project and the age of its index; `verouderd` once, when the index or the state is old. */
function headerOf(project: ProjectModel): { text: string; tone: Row['tone'] } {
  if (!project.hasStore) {
    return { text: `${project.id} · ${project.reason}`, tone: 'error' }
  }

  const age = project.index.isOk ? formatAge(project.index.value.ageMinutes) : 'onbeschikbaar'
  const isOld = isOutdated(project)

  return { text: `${project.id} · index ${age}${isOld ? ' · verouderd' : ''}`, tone: isOld ? 'warn' : 'head' }
}

/**
 * The sections in the order that takes the room: Nu bezig, Wacht op een besluit, Tracks, PR's, opruimen.
 * Each gets what is left of `budget` rows.
 */
function projectRows(project: Extract<ProjectModel, { hasStore: true }>, budget: number, columns: number): Row[] {
  const sections = [nowSectionOf(project, columns), waitSectionOf(project), tracksSectionOf(project), prsSectionOf(project)]
  const rows: Row[] = []

  for (const section of sections) {
    rows.push(...placeSection(section, budget - rows.length))
  }

  const cleanup = cleanupRow(project)

  return cleanup !== null && budget - rows.length >= 1 ? [...rows, cleanup] : rows
}

/**
 * The pane, drawn from the model alone: the header (project and index age), then the sections of that one
 * project, every row cut to the pane's width and every section cut to the rows that are left.
 */
export function paneRows(model: Model | null, size: PaneSize): { header: string; headerTone: Row['tone']; lines: Row[] } {
  const busy = size.isRefreshing ? ' · verversen…' : ''
  const failed = size.failure === undefined || size.failure === null ? [] : [row(`verversen mislukt: ${size.failure}`, 'error')]
  const cut = (lines: Row[]) => lines.map(line => ({ ...line, text: fit(line.text, size.columns) }))

  if (model === null) {
    return { header: fit(`Voortgang${busy}`, size.headerColumns), headerTone: 'head', lines: cut([...failed, row('nog niet ververst', 'dim')]) }
  }

  const { project } = model

  if (project === null) {
    return { header: fit(`Voortgang${busy}`, size.headerColumns), headerTone: 'head', lines: cut([...failed, row(model.noProject ?? '', 'dim')]) }
  }

  const header = headerOf(project)
  // The header row and the failure row come out of the pane's rows before the sections share what is left.
  const budget = Math.max(0, size.rows - 1 - failed.length)
  const lines = project.hasStore ? projectRows(project, budget, size.columns) : []

  return { header: fit(`${header.text}${busy}`, size.headerColumns), headerTone: header.tone, lines: cut([...failed, ...lines]) }
}
