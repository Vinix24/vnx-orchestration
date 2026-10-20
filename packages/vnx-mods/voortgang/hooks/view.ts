import type { DeliverableView, Model, NowTrack, Part, Pr, ProjectModel, Row } from '../types'

import { formatAge, isStale } from './collect'

export type PaneSize = { columns: number; headerColumns: number; rows: number; isRefreshing: boolean }

/** The rows a project always takes beside its lists: its name, work, outcomes, PR and track heads, the promote count. */
const FIXED_ROWS = 8
const MIN_LIST = 3
const STATUS_ORDER = ['proposed', 'ready', 'failed', 'done']

const row = (text: string, tone: Row['tone'] = 'normal'): Row => ({ text, tone })
const unavailable = (label: string, reason: string): Row => row(`  ${label}: onbeschikbaar: ${reason}`, 'error')

/** The text cut to `columns` cells with an ellipsis, by code point so no character is split. */
export function fit(text: string, columns: number): string {
  const chars = Array.from(text)

  if (chars.length <= columns) {
    return text
  }

  return columns <= 1 ? '…' : `${chars.slice(0, columns - 1).join('')}…`
}

/** One row of ids: as many as fit beside the label, the rest counted. */
function idsRow(label: string, ids: string[], columns: number): Row {
  const head = `  ${label}: `

  for (let shown = ids.length; shown > 0; shown -= 1) {
    const rest = ids.length - shown
    const text = `${head}${ids.slice(0, shown).join(', ')}${rest > 0 ? ` +${rest} meer` : ''}`

    if (Array.from(text).length <= columns) {
      return row(text, 'dim')
    }
  }

  return row(`${head}+${ids.length} meer`, 'dim')
}

/** The first `room` items drawn by `draw`, and a `+N meer` row for the rest. */
function listRows<T>(items: T[], room: number, draw: (item: T) => Row[]): Row[] {
  const shown = items.slice(0, room).flatMap(draw)

  return items.length > room ? [...shown, row(`  +${items.length - room} meer`, 'dim')] : shown
}

const seatsOf = (seats: string[] | null) => (seats === null ? 'onbekend' : seats.length === 0 ? 'geen' : seats.join(', '))

const prRows = (pr: Pr): Row[] => [
  row(`  #${pr.number} · ci ${pr.ci} · ${pr.merge}${pr.title === '' ? '' : ` · ${pr.title}`}`),
  row(`    geslaagd: ${seatsOf(pr.passed)} · open: ${seatsOf(pr.owed)}`, 'dim'),
]

function countsText(counts: Record<string, number>): string {
  const rest = Object.keys(counts)
    .filter(status => !STATUS_ORDER.includes(status))
    .sort()

  return [...STATUS_ORDER, ...rest]
    .filter(status => (counts[status] ?? 0) > 0)
    .map(status => `${counts[status]} ${status}`)
    .join(' · ')
}

function trackRow(track: NowTrack, deliverables: Part<DeliverableView>): Row {
  const status = `${track.phase}${track.derivedStatus === null ? '' : ` → ${track.derivedStatus}`}`
  const counts = deliverables.isOk ? deliverables.value.byTrack[track.id] : undefined
  const delivered = !deliverables.isOk ? '' : counts === undefined ? ' · geen leveringen' : ` · ${countsText(counts)}`

  return row(`  ${track.priority} ${track.id} · ${status}${delivered}`)
}

function projectRows(project: ProjectModel, room: number, columns: number): Row[] {
  if (!project.hasStore) {
    return [row(`${project.id} · ${project.reason}`, 'error')]
  }

  const { index, state, tracks, deliverables } = project
  const rows: Row[] = []

  if (index.isOk) {
    const { ageMinutes, liveWork, outcomes } = index.value
    const isOld = isStale(ageMinutes)
    const oldTag = isOld ? ' · verouderd' : ''

    rows.push(row(`${project.id} · index ${formatAge(ageMinutes)}`, 'head'))

    if (liveWork.isOk) {
      const work = liveWork.value

      rows.push(
        row(`  werk: ${work.live} live · ${work.starting} start · ${work.stale} stale · ${work.unmeasured} ongemeten${oldTag}`, isOld ? 'warn' : 'normal'),
      )

      if (work.liveIds.length > 0) {
        rows.push(idsRow('live', work.liveIds, columns))
      }
    } else {
      rows.push(unavailable('werk', liveWork.reason))
    }

    if (outcomes.isOk) {
      const open = outcomes.value
      const kinds = Object.entries(open.byOutcome)
        .filter(([, count]) => count > 0)
        .map(([outcome, count]) => `${outcome} ${count}`)

      rows.push(row(`  uitkomsten: ${open.total} open${kinds.length > 0 ? ` (${kinds.join(', ')})` : ''}${oldTag}`, isOld ? 'warn' : 'normal'))

      if (open.ids.length > 0) {
        rows.push(idsRow('besluit nodig', open.ids, columns))
      }
    } else {
      rows.push(unavailable('uitkomsten', outcomes.reason))
    }
  } else {
    rows.push(row(project.id, 'head'), unavailable('index', index.reason))
  }

  if (!state.isOk) {
    rows.push(unavailable("PR's", state.reason))
  } else if (!state.value.prs.isOk) {
    rows.push(unavailable("PR's", state.value.prs.reason))
  } else {
    const isOld = isStale(state.value.ageMinutes)
    const prs = state.value.prs.value

    rows.push(
      row(`  PR's: ${prs.length} open · state ${formatAge(state.value.ageMinutes)}${isOld ? ' · verouderd' : ''}`, isOld ? 'warn' : 'normal'),
      ...listRows(prs, room, prRows),
    )
  }

  if (tracks.isOk) {
    const view = tracks.value
    const unplanned = view.unplanned > 0 ? ` · ${view.unplanned} zonder horizon` : ''

    rows.push(
      row(`  tracks: ${view.now.length} now · ${view.next} next · ${view.later} later${unplanned}`),
      ...listRows(view.now, room, track => [trackRow(track, deliverables)]),
    )
  } else {
    rows.push(unavailable('tracks', tracks.reason))
  }

  if (!deliverables.isOk) {
    rows.push(unavailable('leveringen', deliverables.reason))
  } else if (tracks.isOk) {
    const onNow = tracks.value.now.reduce((sum, track) => sum + (deliverables.value.byTrack[track.id]?.proposed ?? 0), 0)

    rows.push(row(`  te promoveren: ${onNow} now · ${deliverables.value.proposed} totaal`))
  } else {
    rows.push(row(`  te promoveren: ${deliverables.value.proposed} totaal`))
  }

  return rows
}

/**
 * The pane, drawn from the model alone: a header, then each project's rows,
 * every row cut to the pane's width and each list cut to the room the pane's
 * rows leave per project.
 */
export function paneRows(model: Model | null, size: PaneSize): { header: string; lines: Row[] } {
  const busy = size.isRefreshing ? ' · verversen…' : ''

  if (model === null) {
    return { header: fit(`Voortgang${busy}`, size.headerColumns), lines: [row('nog niet ververst', 'dim')] }
  }

  const count = model.projects.length
  const header = fit(`Voortgang · ${count} ${count === 1 ? 'project' : 'projecten'}${busy}`, size.headerColumns)
  let lines: Row[]

  if (model.rootError !== null) {
    lines = [row(`onbeschikbaar: ${model.rootError}`, 'error')]
  } else if (count === 0) {
    lines = [row('geen projecten ingesteld (optie projects)', 'dim')]
  } else {
    const room = Math.max(MIN_LIST, Math.floor(size.rows / count) - FIXED_ROWS)

    lines = model.projects.flatMap(project => projectRows(project, room, size.columns))
  }

  return { header, lines: lines.map(line => ({ ...line, text: fit(line.text, size.columns) })) }
}
