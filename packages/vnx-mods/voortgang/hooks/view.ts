import type { Builder, Model, PaneView, Pr, ProjectModel, Row, Segment, Tone } from '../types'

import { barCellsOf, busyDeliverableIdsOf, cleanTitleOf, deliverablesNotice, formatAge, INDEX_LIVE_CAP, isOutdated, planOf, promotionOf, shownTracksOf } from './collect'
import type { ShownTrack } from './collect'
import { cellsOf, fit } from './width'

export { cellsOf, fit }

export type PaneSize = { columns: number; headerColumns: number; rows: number; isRefreshing: boolean; failure?: string | null; view?: PaneView }

type Stored = Extract<ProjectModel, { hasStore: true }>

/** The column under which the second row of every track starts: after `  P1 `, whatever the priority is. */
const TRACK_INDENT = 5
/** The columns a title keeps at least, before it is cut for the track and the PR behind it. */
const MIN_TITLE_COLUMNS = 8

const seg = (text: string, tone: Tone = 'normal'): Segment => ({ text, tone })
const rowOf = (...segments: Segment[]): Row => ({ segments })
const row = (text: string, tone: Tone = 'normal'): Row => rowOf(seg(text, tone))

/** The text of a row, without its tones. */
export const textOf = (line: Row): string => line.segments.map(one => one.text).join('')

/** The row cut to `columns` display cells with an ellipsis; width is measured on the text, whatever the tones. */
export function fitRow(line: Row, columns: number): Row {
  const full = textOf(line)
  const fitted = fit(full, columns)

  if (fitted === full) {
    return line
  }

  let left = [...fitted].length - 1

  if (left <= 0) {
    return rowOf(seg('…', line.segments[0]?.tone))
  }

  const segments: Segment[] = []

  for (const one of line.segments) {
    const chars = [...one.text]

    if (chars.length >= left) {
      segments.push(seg(`${chars.slice(0, left).join('')}…`, one.tone))
      break
    }

    segments.push(one)
    left -= chars.length
  }

  return rowOf(...segments)
}

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

function builderRow(builder: Builder, columns: number): Row {
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

  return row(`${before}${title}${after}`, failures ? 'error' : 'normal')
}

/** An open PR: its number, what the PR queue knows of it (null when only the index lists it), and the dispatch behind it. */
type OpenPr = { number: number; detail: Pr | null; dispatchId: string | null }

/** Every open PR a source lists: the PR queue, then the ones only the index lists. */
function openPrsToShow(project: Stored): { prs: OpenPr[]; reason: string | null } {
  const { index, state } = project
  const fromIndex = index.isOk && index.value.liveWork.isOk ? index.value.liveWork.value.openPrs : []
  const dispatchOf = (number: number) => fromIndex.find(pr => pr.number === number)?.dispatchId ?? null
  const queue = state.isOk && state.value.prs.isOk ? state.value.prs.value : []
  const reason = !state.isOk ? state.reason : !state.value.prs.isOk ? state.value.prs.reason : null
  const prs: OpenPr[] = queue.map(detail => ({ number: detail.number, detail, dispatchId: dispatchOf(detail.number) }))

  for (const pr of fromIndex) {
    if (!prs.some(one => one.number === pr.number)) {
      prs.push({ number: pr.number, detail: null, dispatchId: pr.dispatchId })
    }
  }

  return { prs, reason }
}

const ciTone = (ci: string): Tone => (ci === 'pass' ? 'ok' : ci === 'fail' ? 'error' : 'normal')
const ciOf = (ci: string): string => (ci === 'pass' ? 'groen' : ci === 'fail' ? 'rood' : ci === 'pending' ? 'loopt' : ci)

/** `PR #N · ci loopt|rood|groen · stoel open: <seats>`, or `PR #N · klaar voor merge` when nothing is owed and CI is green. */
function prRow(pr: OpenPr, indent: string, reason: string | null): Row {
  const label = seg(`${indent}PR #${pr.number}`)
  const dot = seg(' · ', 'dim')

  if (pr.detail === null) {
    return reason === null ? rowOf(label, dot, seg('niet in de PR-wachtrij', 'warn')) : rowOf(label, dot, seg(`onbeschikbaar: ${reason}`, 'error'))
  }

  const { ci, owed } = pr.detail

  if (owed !== null && owed.length === 0 && ci === 'pass') {
    return rowOf(label, dot, seg('klaar voor merge', 'ok'))
  }

  const checks = [label, dot, seg(`ci ${ciOf(ci)}`, ciTone(ci))]

  return owed !== null && owed.length === 0 ? rowOf(...checks) : rowOf(...checks, dot, seg(`stoel open: ${seatsOf(owed)}`, 'warn'))
}

const isReady = (pr: OpenPr): boolean => pr.detail !== null && pr.detail.owed !== null && pr.detail.owed.length === 0 && pr.detail.ci === 'pass'
const isWaiting = (pr: OpenPr): boolean => pr.detail !== null && (pr.detail.owed === null || pr.detail.owed.length > 0)
const isRed = (pr: OpenPr): boolean => pr.detail !== null && pr.detail.ci === 'fail'

/** `N open PR's zonder bouwer · M klaar voor merge · K wacht op stoel · J ci rood`, each count left out at 0, then the reason when the PR queue cannot be read. */
function foldedRow(prs: OpenPr[], reason: string | null): Row {
  const counts: [number, string, Tone][] = [
    [prs.filter(isReady).length, 'klaar voor merge', 'ok'],
    [prs.filter(isWaiting).length, 'wacht op stoel', 'warn'],
    [prs.filter(isRed).length, 'ci rood', 'error'],
  ]
  const dot = seg(' · ', 'dim')
  const parts = counts.filter(([count]) => count > 0).flatMap(([count, text, tone]) => [dot, seg(`${count} ${text}`, tone)])

  const unavailable = reason === null ? [] : [dot, seg(`onbeschikbaar: ${reason}`, 'error')]

  return rowOf(seg(`  ${prs.length} open PR's zonder bouwer`, 'dim'), ...parts, ...unavailable)
}

/** The builders at work, each with the PR it belongs to; then the open PRs that belong to a deliverable `bezig:` names. The rest fold into one row. */
function nowSectionOf(project: Stored, columns: number): Section {
  const { prs, reason } = openPrsToShow(project)
  const owned = new Set<number>()
  // A builder list that cannot be read is its own row; the PRs of a readable queue are still drawn, all folded as no builder is known.
  const items: Row[][] = project.builders.isOk ? [] : [[row(`  onbeschikbaar: ${project.builders.reason}`, 'error')]]
  const builders = project.builders.isOk ? project.builders.value : []

  items.push(...builders.map(builder => {
    const prId = builder.who.isOk && builder.who.value.prId !== null ? (/\d+/.exec(builder.who.value.prId)?.[0] ?? null) : null
    const mine = prs.filter(pr => (prId !== null && pr.number === Number(prId)) || pr.dispatchId === builder.dispatchId)

    mine.forEach(pr => owned.add(pr.number))

    return [builderRow(builder, columns), ...mine.map(pr => prRow(pr, '    ', reason))]
  }))
  const deliverables = project.deliverables.isOk ? Object.values(project.deliverables.value.byTrack).flatMap(one => one.items) : []

  const busyIds = project.builders.isOk ? busyDeliverableIdsOf(project) : new Set<string>()
  const folded: OpenPr[] = []

  for (const pr of prs.filter(one => !owned.has(one.number))) {
    const deliverable = pr.dispatchId === null || !busyIds.has(pr.dispatchId) ? undefined : deliverables.find(one => one.id === pr.dispatchId)

    if (deliverable === undefined) {
      folded.push(pr)
      continue
    }

    const label = deliverable.title !== '' ? fit(deliverable.title, MIN_TITLE_COLUMNS * 4) : cleanTitleOf(deliverable.id)

    items.push([row(`  ${label}`), prRow(pr, '    ', reason)])
  }

  if (folded.length > 0) {
    items.push([foldedRow(folded, reason)])
  }

  // With no PR known at all no row can carry the reason of an unreadable PR queue, so it gets its own.
  if (reason !== null && prs.length === 0) {
    items.push([row(`  onbeschikbaar: ${reason}`, 'error')])
  }

  return sectionOf('Nu bezig', 'niets', items, project.buildersOmitted)
}

function waitSectionOf(project: Stored): Section {
  const { index } = project
  const outcomes = index.isOk ? index.value.outcomes : index
  const promotion = promotionOf(project)
  const parts: Segment[] = []

  if (promotion.isOk) {
    parts.push(seg(`${promotion.value.now} te promoveren (now)`, promotion.value.now > 0 ? 'warn' : 'normal'))
  } else {
    parts.push(seg(`te promoveren onbeschikbaar: ${promotion.reason}`, 'error'))
  }

  if (outcomes.isOk) {
    parts.push(seg(`${pluralOf(outcomes.value.total, 'open uitkomst', 'open uitkomsten')}`))
  } else {
    parts.push(seg(`uitkomsten onbeschikbaar: ${outcomes.reason}`, 'error'))
  }

  const backlog = promotion.isOk ? promotion.value.backlog : 0
  const isNothing = promotion.isOk && outcomes.isOk && promotion.value.now === 0 && outcomes.value.total === 0 && backlog === 0
  const [first = seg(''), ...others] = parts
  const main = rowOf(seg('  '), first, ...others.flatMap(other => [seg(' · '), other]))
  const lines = [main, ...(backlog > 0 ? [row(`  backlog: ${backlog} voorgesteld op next/later`, 'dim')] : [])]

  return sectionOf('Wacht op een besluit', 'niets', isNothing ? [] : [lines])
}

/** The bar in two tones: the filled cells `ok`, the empty cells `dim`. */
const barSegments = (one: NonNullable<ShownTrack['progress']>): Segment[] => {
  const { filled, empty } = barCellsOf(one)

  return [seg('▓'.repeat(filled), 'ok'), seg('░'.repeat(empty), 'dim')]
}

const BUSY_LABEL = 'bezig:  '
const NEXT_LABEL = 'volgt:  '

/** `volgt:` titles as many as fit the width, then `(+N)` for the rest. The count is never cut: the first title gives way to it. */
function upcomingRow(titles: string[], columns: number): Row {
  const prefix = ' '.repeat(TRACK_INDENT) + NEXT_LABEL
  const room = columns - cellsOf(prefix)
  const countOf = (rest: number) => (rest > 0 ? `  (+${rest})` : '')
  let shown = titles.length

  while (shown > 1 && cellsOf(titles.slice(0, shown).join(' · ')) + cellsOf(countOf(titles.length - shown)) > room) {
    shown -= 1
  }

  const rest = titles.length - shown
  const count = countOf(rest)
  const shownTitles = titles.slice(0, shown)
  const titleRoom = room - cellsOf(count)

  if (shown === 1) {
    const only = titleRoom >= 1 ? fit(titles[0] ?? '', titleRoom) : ''
    const dim = only === '' ? count.trim() : count

    return rowOf(seg(prefix), ...(only === '' ? [] : [seg(only, 'warn')]), ...(rest > 0 ? [seg(dim, 'dim')] : []))
  }

  const list = shownTitles.flatMap((title, at) => (at === 0 ? [seg(title, 'warn')] : [seg(' · ', 'dim'), seg(title, 'warn')]))

  return rowOf(seg(prefix), ...list, ...(rest > 0 ? [seg(count, 'dim')] : []))
}

function trackItem(one: ShownTrack, columns: number, isNow: boolean): Row[] {
  const head = `  ${one.track.priority} ${one.track.id}`
  const indent = ' '.repeat(TRACK_INDENT)
  const busy = isNow && one.busy.length > 0 ? [rowOf(seg(`${indent}${BUSY_LABEL}`), seg(one.busy.join(' · ')))] : []
  const upcoming = one.upcoming.length > 0 ? [upcomingRow(one.upcoming, columns)] : []

  if (one.unreadable !== null) {
    return [row(head), row(`${indent}leveringen onbeschikbaar: ${one.unreadable}`, 'error'), ...busy]
  }

  if (one.progress === null) {
    return [rowOf(seg(head), seg('  geen leveringen gepland', 'dim')), ...busy]
  }

  const isAllDone = one.progress.done === one.progress.total
  const tone: Tone = isAllDone ? 'ok' : 'normal'

  return [rowOf(seg(head, tone), seg('  '), ...barSegments(one.progress), seg(`  ${one.progress.done}/${one.progress.total}`, tone)), ...busy, ...upcoming]
}

function tracksSectionOf(project: Stored, columns: number): Section {
  const tracks = shownTracksOf(project)

  if (tracks === null) {
    return failedSection('Tracks', project.tracks.isOk ? '' : project.tracks.reason)
  }

  const notice = deliverablesNotice(project)

  return sectionOf('Tracks', 'geen', [...(notice === null ? [] : [[row(`  ${notice}`, 'error')]]), ...tracks.map(one => trackItem(one, columns, true))])
}

/** The plan sections: the tracks on `next`, the tracks on `later`, and one dim count of the tracks without a horizon. */
function planSectionsOf(project: Stored, columns: number): { sections: Section[]; footer: Row | null } {
  const plan = planOf(project)

  if (plan === null) {
    return { sections: [failedSection('Plan', project.tracks.isOk ? '' : project.tracks.reason)], footer: null }
  }

  const notice = deliverablesNotice(project)
  const noticeItems = notice === null ? [] : [[row(`  ${notice}`, 'error')]]
  const footer = plan.withoutHorizon > 0 ? row(`${pluralOf(plan.withoutHorizon, 'track', 'tracks')} zonder horizon`, 'dim') : null

  return {
    sections: [
      sectionOf('next', 'geen', [...noticeItems, ...plan.next.map(one => trackItem(one, columns, false))]),
      sectionOf('later', 'geen', plan.later.map(one => trackItem(one, columns, false))),
    ],
    footer,
  }
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
function cleanupRow(project: Stored): Row | null {
  if (!project.index.isOk || !project.index.value.liveWork.isOk) {
    return null
  }

  const { stale, unmeasured } = project.index.value.liveWork.value
  const parts = [stale > 0 ? pluralOf(stale, 'stale rij', 'stale rijen') : null, unmeasured > 0 ? `${unmeasured} ongemeten` : null].filter((part): part is string => part !== null)

  return parts.length === 0 ? null : row(`opruimen: ${parts.join(' · ')}`, 'dim')
}

/** The header of the pane: the project and the age of its index; `verouderd` once, when the index or the state is old. */
function headerOf(project: ProjectModel): { text: string; tone: Tone } {
  if (!project.hasStore) {
    return { text: `${project.id} · ${project.reason}`, tone: 'error' }
  }

  const age = project.index.isOk ? formatAge(project.index.value.ageMinutes) : 'onbeschikbaar'
  const isOld = isOutdated(project)

  return { text: `${project.id} · index ${age}${isOld ? ' · verouderd' : ''}`, tone: isOld ? 'warn' : 'head' }
}

/**
 * The sections in the order that takes the room. The now view: Nu bezig, Wacht op een besluit, Tracks, opruimen.
 * The plan view: next, later, the count without horizon. Each section gets what is left of `budget` rows.
 */
function projectRows(project: Stored, budget: number, columns: number, view: PaneView): Row[] {
  const plan = view === 'plan' ? planSectionsOf(project, columns) : null
  const sections = plan === null ? [nowSectionOf(project, columns), waitSectionOf(project), tracksSectionOf(project, columns)] : plan.sections
  const rows: Row[] = []

  for (const section of sections) {
    rows.push(...placeSection(section, budget - rows.length))
  }

  const last = plan === null ? cleanupRow(project) : plan.footer

  return last !== null && budget - rows.length >= 1 ? [...rows, last] : rows
}

/**
 * The pane, drawn from the model alone: the header (project and index age), then the sections of that one
 * project, every row cut to the pane's width and every section cut to the rows that are left.
 */
export function paneRows(model: Model | null, size: PaneSize): { header: string; headerTone: Tone; lines: Row[] } {
  const busy = size.isRefreshing ? ' · verversen…' : ''
  const failed = size.failure === undefined || size.failure === null ? [] : [row(`verversen mislukt: ${size.failure}`, 'error')]
  const cut = (lines: Row[]) => lines.map(line => fitRow(line, size.columns))

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
  const lines = project.hasStore ? projectRows(project, budget, size.columns, size.view ?? 'now') : []

  return { header: fit(`${header.text}${busy}${size.view === 'plan' ? ' · plan' : ''}`, size.headerColumns), headerTone: header.tone, lines: cut([...failed, ...lines]) }
}
